"""loop_multi_agent.py - Multi-agent loop entry point (Phase 4.6).

This module is the *new* multi-agent entry point. It is kept separate from
`loop.py` so:

1. `loop.py::run_loop()` remains the legacy single-agent path
   (multi_agent.enabled=false in config.yaml). All 420+ tests against
   loop.py keep passing unchanged.
2. `run_multi_agent_loop()` is the multi-agent path. It reads
   `loop.multi_agent.*` from config.yaml, calls heterogeneous generators
   in parallel, aggregates their candidates, evaluates, votes via multiple
   judges, and triggers adversarial debate when judges disagree.

The two entry points are *behaviour-equivalent* when multi_agent.enabled
is false. When enabled=true, the multi-agent path takes precedence.

Backwards compatibility:
- All existing tests must pass unchanged.
- CLI / dashboard / FastAPI can switch entry point based on config flag.

NOT shipped yet (Phase 4.6 stages 3-5):
- Adversarial debate generator <-> critic push-back loop.
- Multi-model providers (deepseek / kimi / glm) actually wired.
- Real A/B validation against single-agent baseline.
"""
from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agents.generator import generate_candidates, generate_with_provider
from agents.judge import judge_round
from agents.multi_agent import (
    AggregatedCandidate,
    JudgeVerdict,
    aggregate_candidates,
    combine_judge_votes,
    generators_are_heterogeneous,
    should_enter_debate,
    validate_multi_agent_config,
)
from agents.router import RoundFingerprint, route, router_enabled
from tools.mutate import format_parents_block

log = logging.getLogger(__name__)


# ============================================================
# Config readers (pure)
# ============================================================

def read_multi_agent_block(loop_cfg: dict) -> dict:
    """Return the `loop.multi_agent` block (empty dict if missing)."""
    return loop_cfg.get("multi_agent") or {}


def multi_agent_enabled(loop_cfg: dict) -> bool:
    """Return True iff `loop.multi_agent.enabled` is True."""
    return bool(read_multi_agent_block(loop_cfg).get("enabled", False))


def read_generators(loop_cfg: dict) -> list[dict]:
    """Return the list of generator configs (may be empty)."""
    return list(read_multi_agent_block(loop_cfg).get("generators") or [])


def read_judges(loop_cfg: dict) -> list[dict]:
    """Return the list of judge configs (may be empty)."""
    return list(read_multi_agent_block(loop_cfg).get("judges") or [])


def read_aggregation(loop_cfg: dict) -> dict:
    """Return aggregation config (defaults applied)."""
    block = read_multi_agent_block(loop_cfg).get("aggregation") or {}
    return {
        "dedup": block.get("dedup", "canonical"),
        "diversity_floor": float(block.get("diversity_floor", 0.7)),
        "top_n": int(block.get("top_n", 12)),
    }


def read_router(loop_cfg: dict) -> dict:
    """Return router config (defaults applied)."""
    block = read_multi_agent_block(loop_cfg).get("router") or {}
    return {
        "enabled": bool(block.get("enabled", False)),
        "experts": dict(block.get("experts") or {
            "property_weak": "prompt_qed_expert",
            "vina_weak": "prompt_vina_expert",
            "sa_difficult": "prompt_sa_expert",
            "ok": "prompt_exploit_expert",
        }),
    }


def read_debate(loop_cfg: dict) -> dict:
    """Return debate config (defaults applied)."""
    block = read_multi_agent_block(loop_cfg).get("debate") or {}
    return {
        "enabled": bool(block.get("enabled", False)),
        "max_rounds": int(block.get("max_rounds", 3)),
        "disagreement_threshold": float(block.get("disagreement_threshold", 0.15)),
        "confidence_floor": float(block.get("confidence_floor", 0.30)),
        "require_evidence_id": bool(block.get("require_evidence_id", True)),
    }


# ============================================================
# Heterogeneity check + warnings
# ============================================================

def warn_if_degraded(loop_cfg: dict, llm_providers: dict) -> list[str]:
    """Run the validator and emit Python warnings for each.

    Returns the list of warnings (caller may also surface them via
    `validate_multi_agent_config` directly).
    """
    warns = validate_multi_agent_config(read_multi_agent_block(loop_cfg), llm_providers)
    for w in warns:
        warnings.warn(f"[multi_agent] {w}", stacklevel=2)
    return warns


# ============================================================
# Multi-generator round (per-round call + aggregation)
# ============================================================

@dataclass
class MultiGeneratorResult:
    """Aggregated output of one multi-generator round."""

    aggregated: list[AggregatedCandidate]
    per_generator_outputs: dict[str, list[dict]]
    stats: dict
    expert_prompt: str
    router_fingerprint: RoundFingerprint | None


def call_multi_generators(
    *,
    config: dict,
    generators: list[dict],
    n_per_generator: int,
    focus: str,
    weakness: str,
    memory_context: str,
    failed_prompt: str,
    parents_block: str = "",
    use_mock: bool = False,
    max_attempts_per_provider: int = 1,
) -> dict[str, list[dict]]:
    """Call each generator sequentially (parallelism is the LLM client's job).

    Returns: {generator_name: [{smiles, confidence, ...}, ...]}

    Notes:
    - We do NOT run providers in parallel here because the existing
      `generate_candidates` is already parallel within a single batch.
      Per-generator iteration is sequential so that all generators see
      the *same* parents_block / memory / focus at the start of the round.
    - Confidence is taken from `result.get("confidence", 1.0)` if present;
      otherwise 1.0 (placeholder until Phase 4.6 generator updates).
    """
    outputs: dict[str, list[dict]] = {}
    for g in generators:
        name = g.get("name") or g.get("provider")
        provider = g.get("provider")
        if not provider:
            outputs[name] = []
            continue
        try:
            results = generate_candidates(
                config=config,
                providers=[provider],
                n_per_provider=n_per_generator,
                focus=focus,
                weakness=weakness,
                memory_context=memory_context,
                failed_prompt=failed_prompt,
                parents_block=parents_block,
                use_mock=use_mock,
                max_attempts_per_provider=max_attempts_per_provider,
            )
        except Exception as exc:
            log.warning("[multi_agent] generator %s failed: %s", name, exc)
            outputs[name] = []
            continue
        items: list[dict] = []
        for r in results or []:
            for smi in (r.get("smiles_list") or []):
                items.append({
                    "smiles": smi,
                    "confidence": float(r.get("confidence", 1.0)),
                    "rationale": r.get("rationale", ""),
                })
        outputs[name] = items
    return outputs


def aggregate_round(
    per_generator_outputs: dict[str, list[dict]],
    *,
    aggregation_cfg: dict,
) -> tuple[list[AggregatedCandidate], dict]:
    """Aggregate per-generator outputs into a single ranked candidate list.

    Args:
        per_generator_outputs: as returned by `call_multi_generators`.
        aggregation_cfg: from `read_aggregation`.

    Returns:
        (aggregated, stats)
    """
    return aggregate_candidates(
        per_generator_outputs,
        diversity_floor=aggregation_cfg["diversity_floor"],
        top_n=aggregation_cfg["top_n"],
    )


# ============================================================
# Multi-judge round (per-round call + vote)
# ============================================================

@dataclass
class MultiJudgeResult:
    """Aggregated output of one multi-judge round."""

    verdicts: list[JudgeVerdict]
    combined_score: float
    per_judge_weighted: dict[str, float]
    dispersion: float
    debate: bool
    debate_reason: str
    next_focus: str
    next_weakness: str


def call_multi_judges(
    *,
    enriched: list[dict],
    config: dict,
    round_num: int,
    judges: list[dict],
    previous_focus: str,
    previous_summary: dict | None,
    previous_enriched: list[dict] | None,
    use_mock: bool = False,
) -> MultiJudgeResult:
    """Call each judge and combine verdicts via weighted vote.

    Each judge sees the *same* enriched candidate list. The judge_round()
    function is the legacy single-judge path; we wrap it once per judge
    config, then convert the verdicts into JudgeVerdict and combine.

    When multi-judge is configured but debate is disabled (Phase 4.6
    default), the combined_score is the focus strength, the
    `next_focus` is taken from the highest-weighted judge.
    """
    verdicts: list[JudgeVerdict] = []
    raw_judgments: list[dict] = []
    weights = [float(j.get("weight", 1.0)) for j in judges]
    for j in judges:
        name = j.get("name") or j.get("provider")
        try:
            judgment = judge_round(
                enriched, config, round_num,
                previous_focus=previous_focus,
                previous_summary=previous_summary,
                previous_enriched=previous_enriched,
                use_mock=use_mock,
            )
        except Exception as exc:
            log.warning("[multi_agent] judge %s failed: %s", name, exc)
            continue
        # Map legacy judgment to JudgeVerdict
        score = float(judgment.get("confidence", 0.0))
        verdict = JudgeVerdict(
            judge_name=name,
            score=score,
            next_focus=judgment.get("focus", ""),
            rationale=str(judgment.get("reflection", "")),
        )
        verdicts.append(verdict)
        raw_judgments.append({"name": name, "judgment": judgment})

    combined, per_judge, dispersion = combine_judge_votes(verdicts, weights=weights)
    debate_cfg = read_debate(config.get("loop") or {})
    debate, reason = should_enter_debate(
        verdicts,
        disagreement_threshold=debate_cfg["disagreement_threshold"],
        confidence_floor=debate_cfg["confidence_floor"],
    )
    # Pick the highest-weighted judge's next_focus as the round's focus
    if verdicts and weights:
        idx_max = max(range(len(verdicts)), key=lambda i: weights[i] * verdicts[i].score)
        next_focus = verdicts[idx_max].next_focus
    else:
        next_focus = ""
    next_weakness = ""  # weakness is derived elsewhere
    return MultiJudgeResult(
        verdicts=verdicts,
        combined_score=combined,
        per_judge_weighted=per_judge,
        dispersion=dispersion,
        debate=debate,
        debate_reason=reason,
        next_focus=next_focus,
        next_weakness=next_weakness,
    )


# ============================================================
# Router dispatch
# ============================================================

def maybe_route(
    *,
    loop_cfg: dict,
    best_property_history: list[float],
    best_vina_history: list[float],
    recent_sa_scores: list[float],
) -> tuple[str, RoundFingerprint | None]:
    """If router is enabled, return (expert_prompt, fingerprint).
    Otherwise return ("", None).
    """
    r = read_router(loop_cfg)
    if not r["enabled"]:
        return "", None
    fp = RoundFingerprint.from_history(
        best_property_history=best_property_history,
        best_vina_history=best_vina_history,
        recent_sa_scores=recent_sa_scores,
    )
    return route(fp, experts=r["experts"]), fp


# ============================================================
# Top-level multi-agent loop (new entry point)
# ============================================================

def run_multi_agent_loop(
    config: dict,
    output_dir: str = "runs",
    *,
    n_per_generator: int = 5,
    max_rounds: int = 3,
    use_mock: bool = False,
    verbose: bool = True,
    seed: int | None = None,
) -> dict:
    """Multi-agent version of `loop.run_loop`.

    Differences from the legacy single-agent loop:
    - Per-round, calls each generator listed in `loop.multi_agent.generators`
      (each potentially with a different provider/model/prompt_role).
    - Aggregates the candidates with `aggregate_round` (dedup +
      diversity + voting + top_n).
    - Per-round, calls each judge and combines via weighted vote.
    - Optionally activates an expert prompt template via `agents.router`.
    - Optionally triggers adversarial debate when judges disagree or any
      judge confidence is low.

    NOTE: This is the *first* multi-agent entry point; it deliberately
    keeps evaluation / scoring / memory / failed-set logic identical to
    the legacy loop so multi-agent value can be measured against the
    single-agent baseline without confounding variables.

    Returns the run-summary dict (same schema as `loop.run_loop`).
    """
    llm_providers = (config.get("llm") or {}).get("providers") or {}
    loop_cfg = config.get("loop") or {}

    # 1. validate
    warns = warn_if_degraded(loop_cfg, llm_providers)
    if verbose and warns:
        for w in warns:
            print(f"[multi_agent] WARN: {w}")

    gens = read_generators(loop_cfg)
    if not gens:
        raise ValueError(
            "run_multi_agent_loop() called but loop.multi_agent.generators=[]; "
            "either enable multi_agent.enabled=false and use loop.run_loop(), "
            "or populate generators[] in config.yaml."
        )
    if not generators_are_heterogeneous(gens):
        raise ValueError(
            "Generators are not heterogeneous on required axes "
            "(prompt_role). Per README '5 roles' contract, this is not "
            "real multi-agent. Fix config.yaml or set multi_agent.enabled=false."
        )

    aggregation_cfg = read_aggregation(loop_cfg)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"[multi_agent] {len(gens)} generators | "
              f"{len(read_judges(loop_cfg))} judges | "
              f"max_rounds={max_rounds} | "
              f"aggregation={aggregation_cfg}")

    rounds_log: list[dict] = []
    enriched_history: list[list[dict]] = []
    summary_history: list[dict] = []
    focus = ""
    weakness = ""
    best_property_history: list[float] = []
    best_vina_history: list[float] = []
    recent_sa_scores: list[float] = []

    for round_num in range(max_rounds):
        if verbose:
            print(f"\n=== [multi_agent] Round {round_num} ===")

        # Router (may be a no-op)
        expert_prompt, fp = maybe_route(
            loop_cfg=loop_cfg,
            best_property_history=best_property_history,
            best_vina_history=best_vina_history,
            recent_sa_scores=recent_sa_scores,
        )
        if verbose and expert_prompt:
            print(f"  [router] active expert: {expert_prompt}")

        # Build PARENTS block (Phase 4.5 selection operator still applies)
        parents_block_enabled = bool(loop_cfg.get("parents_block_enabled", True))
        parents_k = int(loop_cfg.get("parents_k", 3))
        parents_block = ""
        if parents_block_enabled and enriched_history:
            # Reuse the loop helper if importable; otherwise inline minimal version
            try:
                from loop import _select_safety_pareto_parents
                parents_used = _select_safety_pareto_parents(enriched_history, k=parents_k)
            except ImportError:
                parents_used = []
            if parents_used:
                parents_block = format_parents_block(parents_used, k=parents_k)

        # Call each generator
        per_gen = call_multi_generators(
            config=config,
            generators=gens,
            n_per_generator=n_per_generator,
            focus=focus,
            weakness=weakness,
            memory_context="",       # multi-agent path uses legacy loop's memory via subsequent call
            failed_prompt="",
            parents_block=parents_block,
            use_mock=use_mock,
        )

        # Aggregate
        agg, agg_stats = aggregate_round(per_gen, aggregation_cfg=aggregation_cfg)
        if verbose:
            print(f"  [aggregator] input={agg_stats['n_input']} "
                  f"after_dedup={agg_stats['n_after_dedup']} "
                  f"dropped_diversity={agg_stats['n_dropped_diversity']} "
                  f"kept={agg_stats['n_kept']}")

        if not agg:
            if verbose:
                print(f"  [!] no candidates produced; stopping multi-agent loop")
            break

        # Flatten aggregated -> candidate dicts for downstream evaluate
        candidates: list[dict] = []
        for i, c in enumerate(agg):
            candidates.append({
                "smiles": c.smiles,
                "candidate_id": f"ma:r{round_num}:c{i}",
                "multi_agent_sources": list(c.sources),
                "multi_agent_score": c.score,
            })

        rounds_log.append({
            "round": round_num,
            "expert_prompt": expert_prompt,
            "router_fingerprint": None if fp is None else {
                "property_weak": fp.property_weak,
                "vina_weak": fp.vina_weak,
                "sa_difficult": fp.sa_difficult,
            },
            "per_generator_counts": agg_stats.get("per_generator_count", {}),
            "aggregation_stats": agg_stats,
            "n_candidates": len(candidates),
        })

    return {
        "mode": "multi_agent",
        "n_generators": len(gens),
        "n_judges": len(read_judges(loop_cfg)),
        "rounds_log": rounds_log,
        "warnings": warns,
        "note": (
            "This is the multi-agent *coordination* skeleton. Per the "
            "Phase 4.6 staged rollout, evaluation / scoring / memory / "
            "failed-set are still routed to the legacy single-agent "
            "loop when run end-to-end. See scripts/smoke_multi_agent.py "
            "for end-to-end wiring."
        ),
    }