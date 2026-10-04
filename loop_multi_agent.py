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

import copy
import json
import logging
import re
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
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
# Phase 4.1+ integrations brought into the multi-agent path so this entry
# point stops bypassing the project's core safety nets.
from agents.working_memory import WorkingMemory
from agents.failed_set import FailedLigandSet
from agents.loop_controller import LoopController, LoopConfig, LoopState
from agents.evaluator import summarize_round
from tools.mutate import format_parents_block
# Manifest + caches mirror loop.py so multi-agent runs are audit-equivalent
# to legacy single-agent runs (same schema_version=2 manifest, same
# protocol_id derived from target + scoring, same evaluation/docking caches).
from tools.evaluation_cache import EvaluationCache
from tools.docking_cache import DockingCache
from tools.provenance import digest, evaluation_protocol, docking_protocol, file_hash

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
    per_generator_focus: dict[str, str] | None = None,
    per_generator_weakness: dict[str, str] | None = None,
) -> dict[str, list[dict]]:
    """Call each generator sequentially (parallelism is the LLM client's job).

    Returns: {generator_name: [{smiles, confidence, ...}, ...]}

    Phase 4.6 stage 7: per_generator_focus / per_generator_weakness let each
    generator see a *different* prompt_role framing even when they share
    the same base focus / weakness from the prior round's judge. This is
    the wiring that turns multi-agent into a real "5 roles" contract:
    A1_qed sees QED focus, A2_vina sees Vina focus, A3_synth sees
    synthesis focus. Without this dict, all generators see the same
    focus string and behave identically (still heterogeneous on
    model/prompt_role, but the role is not visible to the model).

    Confidence is taken from `result.get("confidence", 1.0)` if present;
    otherwise 1.0 (placeholder).
    """
    outputs: dict[str, list[dict]] = {}
    per_generator_focus = per_generator_focus or {}
    per_generator_weakness = per_generator_weakness or {}
    for g in generators:
        name = g.get("name") or g.get("provider")
        provider = g.get("provider")
        if not provider:
            outputs[name] = []
            continue
        gen_focus = per_generator_focus.get(name, focus)
        gen_weakness = per_generator_weakness.get(name, weakness)
        try:
            results = generate_candidates(
                config=config,
                providers=[provider],
                n_per_provider=n_per_generator,
                focus=gen_focus,
                weakness=gen_weakness,
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
    best_safe_vina_history: list[float] | None = None,
    progress_signal: str = "vina",
) -> tuple[str, RoundFingerprint | None]:
    """If router is enabled, return (expert_prompt, fingerprint).
    Otherwise return ("", None).

    Phase 4.7: when ``progress_signal == "safe_vina"``, the fingerprint
    looks at the safety-gated Vina series instead of the all-candidate
    Vina series, so the router no longer watches unsafe progress when
    the project has opted into the safety-gated signal.
    """
    r = read_router(loop_cfg)
    if not r["enabled"]:
        return "", None
    fp = RoundFingerprint.from_history(
        best_property_history=best_property_history,
        best_vina_history=best_vina_history,
        recent_sa_scores=recent_sa_scores,
        best_safe_vina_history=best_safe_vina_history,
        progress_signal=progress_signal,
    )
    return route(fp, experts=r["experts"]), fp


# ============================================================
# Per-generator focus helper (Phase 4.6 stage 7 wiring)
# ============================================================

from agents.prompts import load as load_prompt, render as render_prompt
from agents.marketplace import select_top_k_by_vote as marketplace_select_top_k


def build_per_generator_focus(
    generators: list[dict],
    base_focus: str,
    base_weakness: str,
    expert_prompt: str,
    debate_critique: str = "",
) -> tuple[dict[str, str], dict[str, str]]:
    """Compose a per-generator focus / weakness overlay.

    For each generator, render its prompt_role template with the
    available context. The result is a different `focus` string per
    generator even when the upstream judge returned a single string.

    Args:
        generators: list of generator configs (must each carry a
            prompt_role or default).
        base_focus: focus string from the previous round's judge (may
            be empty on round 0).
        base_weakness: weakness string from the previous round's judge.
        expert_prompt: name of the expert template activated by the
            router (may be empty).
        debate_critique: when adversarial debate is in progress, the
            critic's most recent push-back to fold into the focus.

    Returns:
        (per_generator_focus, per_generator_weakness)
    """
    per_focus: dict[str, str] = {}
    per_weakness: dict[str, str] = {}
    for g in generators:
        name = g.get("name") or g.get("provider")
        role = g.get("prompt_role") or "default"
        # Use the expert template if router activated one, otherwise
        # the role-specific template.
        template_name = expert_prompt if expert_prompt else role
        try:
            rendered = render_prompt(
                template_name,
                {
                    "n": "1",
                    "focus": base_focus,
                    "weakness": base_weakness,
                    "memory": "",
                    "parents_block": "",
                    "failed_prompt": "",
                },
            )
        except Exception:
            rendered = base_focus
        per_focus[name] = f"{rendered}\n{debate_critique}".strip()
        per_weakness[name] = base_weakness
    return per_focus, per_weakness


# ============================================================
# Adversarial debate glue (Phase 4.6 stage 7)
# ============================================================

from agents.debate import (
    DebateTurn,
    extract_evidence_ids,
    run_debate as debate_run,
    should_terminate_debate,
    validate_critic_turn,
)


def _select_safety_pareto_parents_via_marketplace(
    enriched_history: list[list[dict]],
    parents_k: int,
) -> tuple[list[dict], dict]:
    """Phase 4.6 stage 13: pick PARENTS block via marketplace N-of-N voting.

    Two-stage selection:
    1. Take top ``max(2k, 6)`` Pareto-front safety-pass candidates
       from the legacy ``_select_safety_pareto_parents`` (oversampled
       so N-of-N voting has room to choose).
    2. Rank the candidate pool by ECFP4-Tanimoto N-of-N voting
       (``agents.marketplace.select_top_k_by_vote``): structurally
       similar candidates reinforce each other; dissimilar ones
       cancel out. The top ``parents_k`` ranked entries become the
       PARENTS block.

    Returns:
        (parents, stats)
        parents: up to parents_k enriched candidate dicts.
        stats: dict with n_candidate_pool / n_unparseable / n_selected /
            score_range for audit.
    """
    if parents_k < 1:
        raise ValueError("parents_k must be >= 1")
    try:
        from loop import _select_safety_pareto_parents
    except ImportError:
        return [], {}
    pool = _select_safety_pareto_parents(
        enriched_history, k=max(parents_k * 2, 6),
    )
    if not pool:
        return [], {}
    market_in = [
        {"smiles": c.get("smiles"), "score": c.get("composite_score", 0.0)}
        for c in pool if c.get("smiles")
    ]
    if not market_in:
        return [], {}
    top, market_stats = marketplace_select_top_k(market_in, k=parents_k)
    smiles_to_full = {c["smiles"]: c for c in pool if c.get("smiles")}
    parents = [smiles_to_full[v.smiles] for v in top
               if v.smiles in smiles_to_full]
    stats = {
        "n_candidate_pool": market_stats.get("n_valid", len(market_in)),
        "n_unparseable": market_stats.get("n_unparseable", 0),
        "n_selected": len(parents),
        "score_range": list(market_stats.get("score_range", ())),
    }
    return parents, stats


def maybe_debate(
    *,
    judge_verdicts,
    debate_cfg: dict,
    initial_generator_text: str,
    round_num: int,
    verbose: bool = False,
) -> tuple[bool, str, str]:
    """If debate is enabled AND disagreement / low-confidence triggers,
    run a synthetic single-round debate.

    In live multi-agent runs the "critic" text is the highest-confidence
    judge's critique. The "generator" response is the LLM-generated
    SMILES list from this round.

    Returns:
        (should_debate, reason, critic_text)
    """
    if not debate_cfg.get("enabled", False):
        return False, "debate_disabled", ""
    should, reason = should_enter_debate(
        judge_verdicts,
        disagreement_threshold=debate_cfg["disagreement_threshold"],
        confidence_floor=debate_cfg["confidence_floor"],
    )
    if not should:
        return False, reason, ""
    # Pick the highest-confidence judge's rationale as the critic push-back.
    if judge_verdicts:
        best = max(judge_verdicts, key=lambda v: v.score)
        critic_text = f"{best.rationale}".strip()
    else:
        critic_text = ""
    if verbose:
        print(f"  [debate] round {round_num} trigger={reason} "
              f"critic={best.judge_name if judge_verdicts else '(none)'}")
    return True, reason, critic_text


def debate_recall_generators(
    *,
    config: dict,
    generators: list[dict],
    n_per_generator: int,
    base_focus: str,
    base_weakness: str,
    critic_text: str,
    memory_context: str,
    parents_block: str,
    use_mock: bool,
    max_rounds: int,
) -> dict[str, list[dict]]:
    """Re-call generators with the critic's push-back injected into the prompt.

    Phase 4.6 stage 10 (debate generator re-call): when should_enter_debate
    triggers, this function calls each generator ONE more time in the
    same round, with a new focus that includes `critic_text`. The
    returned candidates are appended to the round's pool before the
    final diversity ranking.

    Failure mode: if the underlying call_multi_generators raises (e.g.
    a model 5xx), this returns {} so the main loop can still record
    the round without crashing. Callers detect the empty dict via
    `if debate_per_gen:` and skip re-aggregation.
    """
    debate_focus = (
        f"REVISION REQUEST FROM CRITIC:\n{critic_text}\n\n"
        f"Previous focus was: {base_focus}"
    ).strip()
    try:
        return call_multi_generators(
            config=config,
            generators=generators,
            n_per_generator=n_per_generator,
            focus=debate_focus,
            weakness=base_weakness,
            memory_context=memory_context,
            failed_prompt="",
            parents_block=parents_block,
            use_mock=use_mock,
            max_attempts_per_provider=max_rounds,
        )
    except Exception as exc:
        log.warning("[multi_agent] debate re-call failed: %s", exc)
        return {}


# ============================================================
# Evaluation glue (Phase 4.6 stage 7 - end-to-end wiring)
# ============================================================

def evaluate_aggregated_candidates(
    *,
    candidates: list[dict],
    config: dict,
) -> tuple[list[dict], dict]:
    """Evaluate aggregated multi-agent candidates using agents.evaluator.

    This is the multi-agent equivalent of `loop.evaluate_candidates_with_cache`.
    We intentionally do NOT wire evaluation caches (memory/evaluation_cache)
    in this stage so multi-agent value can be measured against the
    single-agent baseline without confounding variables.

    Args:
        candidates: aggregated candidates from run_multi_agent_loop's
            aggregator (each carries SMILES + multi_agent_sources +
            multi_agent_score).
        config: full project config.

    Returns:
        (enriched, summary_dict)
        enriched: candidates annotated with property / ADMET / safety.
        summary_dict: best_property / best_vina / best_safe_vina / etc.
    """
    try:
        from agents.evaluator import evaluate_candidates, summarize_round
    except ImportError as exc:
        log.warning("[multi_agent] evaluator import failed: %s", exc)
        return candidates, {}
    scoring = (config.get("scoring") or {})
    target = (config.get("target") or {})
    # Legacy loop uses dock_enabled env var to decide whether to call Vina.
    import os as _os
    dock_enabled = str(_os.environ.get("AIDD_DOCK_ENABLED", "0")).lower() in (
        "1", "true", "yes",
    )
    try:
        enriched = evaluate_candidates(
            candidates=candidates,
            scoring_config=scoring,
            target_config=target,
            dock_enabled=dock_enabled,
        )
    except Exception as exc:
        log.warning("[multi_agent] evaluate_candidates failed: %s", exc)
        return candidates, {}
    summary = summarize_round(enriched)
    return enriched, summary


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

    # ---- Phase 4.7: parity with loop.py's safety nets ----
    scoring_cfg = config.get("scoring") or {}
    target_cfg = config.get("target") or {}

    progress_signal = str(loop_cfg.get("progress_signal", "safe_vina"))
    if progress_signal not in ("vina", "safe_vina"):
        raise ValueError(
            "loop.progress_signal must be 'vina' or 'safe_vina' "
            f"(got {progress_signal!r})"
        )
    token_budget = int(loop_cfg.get("token_budget", 50000))

    memory_enabled = bool(loop_cfg.get("memory_enabled", True))
    failed_set_enabled = bool(loop_cfg.get("failed_set_enabled", memory_enabled))
    memory_namespace = str(loop_cfg.get("memory_namespace", "default"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", memory_namespace):
        raise ValueError(
            "loop.memory_namespace may contain only letters, digits, ., _, and -"
        )
    evaluation_cache_enabled = bool(
        loop_cfg.get("evaluation_cache_enabled", True)
    )
    docking_cache_enabled = bool(loop_cfg.get("docking_cache_enabled", True))
    require_all_generators = bool(loop_cfg.get("require_all_generators", False))
    generation_max_attempts = int(loop_cfg.get("generation_max_attempts", 1))
    judge_max_attempts = int(loop_cfg.get("judge_max_attempts", 1))
    if generation_max_attempts < 1 or judge_max_attempts < 1:
        raise ValueError("loop generation/judge max attempts must be positive")

    if not target_cfg.get("name"):
            # Tests and offline smoke runs sometimes omit target.name. Default it
            # to a placeholder so setup continues; loop.py raises the same error
            # at startup, but multi-agent has historically been more permissive.
            log.warning(
                "config.target.name is missing; defaulting to 'UNKNOWN'. "
                "Persistent memory / docking caches will still be created."
            )
            target_cfg = {**target_cfg, "name": "UNKNOWN"}
    import os as _os
    dock_enabled = str(_os.environ.get("AIDD_DOCK_ENABLED", "0")).lower() in (
        "1", "true", "yes",
    )

    # ---- protocol_id (mirror loop.py) ----
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        # evaluation_protocol requires target.receptor_pdbqt and other dock fields.
    # When called with an offline / minimal config (e.g. happy-path mocks
    # without target), fall back to a synthetic protocol_id so setup continues.
    try:
            protocol = evaluation_protocol(target_cfg, scoring_cfg, dock_enabled)
            protocol_id = digest(protocol)
            docking_protocol_id = digest(docking_protocol(target_cfg, scoring_cfg))
    except (KeyError, FileNotFoundError) as exc:
            log.warning(
                "[multi_agent] evaluation_protocol() needs target fields missing "
                "from the config (got %s); using a synthetic protocol_id. "
                "Persistent caches will not be reused across this run.",
                exc,
            )
            protocol = {"synthetic": True, "target_name": target_cfg.get("name")}
            protocol_id = digest(protocol)
            docking_protocol_id = digest({"synthetic": True, "dock": True})

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    if any(out_path.glob("round_*.json")) or (out_path / "manifest.json").exists():
        raise FileExistsError(
            f"Output directory {out_path} already contains a run; "
            "use a new output directory."
        )

    if use_mock:
        memory_base = out_path / "_memory"
        evaluation_cache_root = out_path / "_evaluation_cache"
        docking_cache_root = out_path / "_docking_cache"
    else:
        memory_base = (
            Path(loop_cfg.get("memory_dir", "memory/v2"))
            / target_cfg["name"] / protocol_id / memory_namespace
        )
        evaluation_cache_root = Path(
            loop_cfg.get("evaluation_cache_dir", "memory/evaluation_cache")
        )
        docking_cache_root = Path(
            loop_cfg.get("docking_cache_dir", "memory/docking_cache")
        )

    evaluation_cache = EvaluationCache(
        evaluation_cache_root,
        protocol_id,
        enabled=evaluation_cache_enabled,
    )
    docking_cache = DockingCache(
        docking_cache_root,
        docking_protocol_id,
        enabled=docking_cache_enabled,
    )

    memory_strategy_path = memory_base / "strategy_history.json"
    memory_best_path = memory_base / "best_molecules.json"
    memory = WorkingMemory(
        max_recent=int(loop_cfg.get("memory_max_recent", 3)),
        strategy_persist_path=memory_strategy_path if memory_enabled else None,
        best_persist_path=memory_best_path if memory_enabled else None,
        target_name=target_cfg["name"],
    )
    failed_cfg = scoring_cfg.get("failed_set", {}) or {}
    failed_set = FailedLigandSet(
        path=(memory_base / "failed_ligands.json" if failed_set_enabled else
              out_path / "_disabled_failed_ligands.json"),
        threshold_composite=loop_cfg.get(
            "failed_threshold_composite",
            failed_cfg.get("composite_floor", 0.5),
        ),
        threshold_vina=loop_cfg.get(
            "failed_threshold_vina",
            failed_cfg.get("vina_floor", -2.5),
        ),
        max_size=int(loop_cfg.get(
            "failed_max_size",
            failed_cfg.get("max_size", 0),
        )),
        enable_embeddings=failed_set_enabled and bool(
            (failed_cfg.get("embeddings") or {}).get("enabled", False)
        ),
        embedding_threshold=float(
            (failed_cfg.get("embeddings") or {}).get("threshold", 0.85)
        ),
        embedding_model=str(
            (failed_cfg.get("embeddings") or {}).get("model", "all-MiniLM-L6-v2")
        ),
        embedding_device=str(
            (failed_cfg.get("embeddings") or {}).get("device", "auto")
        ),
    )

    loop_controller = LoopController(LoopConfig(
        max_rounds=max_rounds,
        token_budget=token_budget,
        judge_convergence_patience=int(loop_cfg.get(
            "early_stop_patience",
            loop_cfg.get("judge_convergence_patience", 2),
        )),
        progress_signal=progress_signal,
    ))
    state = LoopState()

    parents_block_enabled = bool(loop_cfg.get("parents_block_enabled", True))
    parents_k = int(loop_cfg.get("parents_k", 3))

    # ---- manifest.json (schema_version=2, same as loop.py) ----
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "is_mock": use_mock,
        "mode": "multi_agent",
        "protocol_id": protocol_id,
        "protocol": protocol,
        "docking_protocol_id": docking_protocol_id,
        "execution": {
            "max_rounds": max_rounds,
            "candidates_per_round_per_generator": n_per_generator,
            "providers": [g.get("provider") for g in gens],
            "judges": [j.get("name") for j in read_judges(loop_cfg)],
            "dock_enabled": dock_enabled,
            "memory_namespace": memory_namespace,
            "judge_enabled": bool(loop_cfg.get("judge_enabled", True)),
            "memory_enabled": memory_enabled,
            "failed_set_enabled": failed_set_enabled,
            "progress_signal": progress_signal,
            "progress_patience": loop_controller.config.judge_convergence_patience,
            "token_budget": token_budget,
            "require_all_generators": require_all_generators,
            "generation_max_attempts": generation_max_attempts,
            "judge_max_attempts": judge_max_attempts,
            "evaluation_cache_enabled": evaluation_cache.enabled,
            "evaluation_cache_dir": str(evaluation_cache.directory.resolve()),
            "docking_cache_enabled": docking_cache.enabled,
            "docking_cache_dir": str(docking_cache.directory.resolve()),
            "parents_block_enabled": parents_block_enabled,
            "parents_k": parents_k,
        },
        "llm": {
            name: {k: v for k, v in settings.items()
                   if k != "api_key_env" and "key" not in k.lower()}
            for name, settings in llm_providers.items()
        },
        "reference_registry_sha256": (
            file_hash("data/reference_compounds.json")
            if Path("data/reference_compounds.json").exists() else None
        ),
        "sa_fragment_model_sha256": (
            file_hash("tools/fpscores.pkl.gz")
            if Path("tools/fpscores.pkl.gz").exists() else None
        ),
        "code_hashes": {
            str(p): file_hash(p) for p in [
                Path("loop_multi_agent.py"),
                *Path("agents").glob("*.py"),
                *Path("tools").glob("*.py"),
            ]
        },
    }
    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if verbose:
        print(f"  [manifest] {out_path / 'manifest.json'}")
        print(f"  [caches] eval={evaluation_cache.enabled} "
              f"dock={docking_cache.enabled} protocol_id={protocol_id[:12]}")

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
    debate_critique = ""
    stop_reason = "max_rounds_reached"
    best_property_history: list[float] = []
    best_vina_history: list[float] = []
    best_safe_vina_history: list[float] = []
    recent_sa_scores: list[float] = []
    debate_cfg = read_debate(loop_cfg)

    # Phase 4.7: refresh memory + failed-set prompt injections after each round.
    memory_context_current = (
        memory.compress_for_generator() if memory_enabled
        else "Memory disabled for this experiment."
    )
    failed_prompt_current = (
        failed_set.format_for_prompt() if failed_set_enabled else ""
    )

    for round_num in range(max_rounds):
        # 0. LoopController: should we stop before starting this round?
        state.round = round_num
        stop, reason = loop_controller.should_stop(state)
        if stop:
            stop_reason = reason
            if verbose:
                print(f"\n[STOP pre-round {round_num}] "
                      f"{loop_controller.explain(reason)}")
            break

        if verbose:
            print(f"\n=== [multi_agent] Round {round_num} ===")
            if memory_enabled:
                first_line = memory_context_current.splitlines()[0]
                print(f"  [memory] {first_line}")
            if failed_set_enabled and failed_set.failed:
                print(f"  [failed_set] {len(failed_set.failed)} SMILES blocked")

        # 1. Router dispatch (may be a no-op).
        expert_prompt, fp = maybe_route(
            loop_cfg=loop_cfg,
            best_property_history=best_property_history,
            best_vina_history=best_vina_history,
            recent_sa_scores=recent_sa_scores,
                    best_safe_vina_history=best_safe_vina_history,
                    progress_signal=progress_signal,
                )
        if verbose and expert_prompt:
            print(f"  [router] active expert: {expert_prompt}")

        # 2. Build PARENTS block (Phase 4.5 selection operator + Phase 4.6
        # marketplace N-of-N voting).
        parents_block_enabled = bool(loop_cfg.get("parents_block_enabled", True))
        parents_k = int(loop_cfg.get("parents_k", 3))
        parents_block = ""
        parents_marketplace_stats: dict = {}
        if parents_block_enabled and enriched_history:
            parents_used, parents_marketplace_stats = (
                _select_safety_pareto_parents_via_marketplace(
                    enriched_history, parents_k=parents_k,
                )
            )
            if parents_used:
                parents_block = format_parents_block(
                    parents_used, k=parents_k,
                )

        # 3. Per-generator focus / weakness overlays (real "5 roles" wiring).
        per_focus, per_weakness = build_per_generator_focus(
            generators=gens,
            base_focus=focus,
            base_weakness=weakness,
            expert_prompt=expert_prompt,
            debate_critique=debate_critique,
        )

        # 4. Call each generator IN PARALLEL via ThreadPoolExecutor.
        # Phase 4.7: previously this was sequential. With 4 generators the
        # wall-clock difference is roughly (latency of slowest) vs (sum of
        # latencies). Each call to LLM takes 10-30s, so parallelism is a
        # 2-3x speedup on multi-agent wall time.
        def _call_one_generator(g: dict) -> tuple[str, list[dict]]:
            name = g.get("name") or g.get("provider") or "unknown"
            provider = g.get("provider")
            if not provider:
                return name, []
            gen_focus = per_focus.get(name, focus)
            gen_weakness = per_weakness.get(name, weakness)
            try:
                results = generate_candidates(
                    config=config,
                    providers=[provider],
                    n_per_provider=n_per_generator,
                    focus=gen_focus,
                    weakness=gen_weakness,
                    memory_context=memory_context_current,
                    failed_prompt=failed_prompt_current,
                    parents_block=parents_block,
                    use_mock=use_mock,
                    max_attempts_per_provider=generation_max_attempts,
                )
            except Exception as exc:
                log.warning("[multi_agent] generator %s failed: %s", name, exc)
                return name, []
            items: list[dict] = []
            for r in results or []:
                for smi in (r.get("smiles_list") or []):
                    items.append({
                        "smiles": smi,
                        "confidence": float(r.get("confidence", 1.0)),
                        "rationale": r.get("rationale", ""),
                    })
            return name, items

        per_gen: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(gens))) as pool:
            for name, items in pool.map(_call_one_generator, gens):
                per_gen[name] = items

        # 5. Aggregate (dedup + diversity + voting + top_n).
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

        # 6. Flatten aggregated -> candidate dicts.
        candidates: list[dict] = []
        for i, c in enumerate(agg):
            candidates.append({
                "smiles": c.smiles,
                "candidate_id": f"ma:r{round_num}:c{i}",
                "multi_agent_sources": list(c.sources),
                "multi_agent_score": c.score,
            })

        # 7. Evaluate (RDKit + ADMET + safety gate; Vina via cache).
        # Phase 4.7: wire EvaluationCache + DockingCache so multi-agent runs
        # reuse work the legacy loop has already done under the same protocol_id.
        try:
            from loop import evaluate_candidates_with_cache
        except ImportError:
            evaluate_candidates_with_cache = None
        cache_stats: dict = {}
        if evaluate_candidates_with_cache is not None:
            try:
                enriched, cache_stats = evaluate_candidates_with_cache(
                    candidates=candidates,
                    scoring_config=scoring_cfg,
                    target_config=target_cfg,
                    dock_enabled=dock_enabled,
                    artifact_dir=str(out_path / "artifacts"),
                    cache=evaluation_cache,
                    docking_cache=docking_cache,
                )
                summary = summarize_round(enriched)
            except Exception as exc:
                log.warning("[multi_agent] cache-aware evaluate failed: %s", exc)
                enriched, summary = evaluate_aggregated_candidates(
                    candidates=candidates, config=config,
                )
        else:
            enriched, summary = evaluate_aggregated_candidates(
                candidates=candidates, config=config,
            )
        if verbose and summary:
            print(f"  [evaluate] n_total={summary.get('n_total')} "
                  f"n_valid={summary.get('n_valid')} "
                  f"best_property={summary.get('best_property')} "
                  f"best_vina={summary.get('best_vina')} "
                  f"best_safe_vina={summary.get('best_safe_vina')} "
                  f"cache_hits={cache_stats.get('hits', 0)}/"
                  f"misses={cache_stats.get('misses', 0)}")

        # 7a. WorkingMemory + FailedLigandSet updates (mirror loop.py).
        if memory_enabled and summary:
            memory.add_round(enriched, focus_used=focus or "")
        if failed_set_enabled and summary and not use_mock:
            new_failures = []
            for c in enriched:
                if c.get("evaluation_status") != "complete":
                    continue
                safety_failed = c.get("safety_gate_pass") is False
                if safety_failed or failed_set.should_mark_failed(
                    c.get("composite_score", 0.0),
                    (c.get("dock") or {}).get("score"),
                ):
                    new_failures.append((
                        c.get("smiles", ""),
                        f"composite={c.get('composite_score')}, "
                        f"vina={(c.get('dock') or {}).get('score')}, "
                        f"safety_gate_pass={c.get('safety_gate_pass')}",
                    ))
            if new_failures:
                failed_set.add_failed_many(new_failures)
        # Refresh for the next round.
        memory_context_current = (
            memory.compress_for_generator() if memory_enabled
            else "Memory disabled for this experiment."
        )
        failed_prompt_current = (
            failed_set.format_for_prompt() if failed_set_enabled else ""
        )

        # 8. Multi-judge vote (independent judges).
        judges = read_judges(loop_cfg)
        if judges:
            judge_result = call_multi_judges(
                enriched=enriched,
                config=config,
                round_num=round_num,
                judges=judges,
                previous_focus=focus,
                previous_summary=summary_history[-1] if summary_history else None,
                previous_enriched=enriched_history[-1] if enriched_history else None,
                use_mock=use_mock,
            )
            focus = judge_result.next_focus
            weakness = ""  # weakness is recorded per-judge; top-weighted wins
            if verbose:
                print(f"  [judges] combined={judge_result.combined_score:.3f} "
                      f"dispersion={judge_result.dispersion:.3f} "
                      f"vote={[(v.judge_name, round(v.score,3)) for v in judge_result.verdicts]}")
                print(f"  [judges] next_focus: {focus[:120]}")
        else:
            judge_result = None
            if verbose:
                print(f"  [judges] none configured; focus unchanged")

        # 9. Adversarial debate trigger.
        debate_triggered, debate_reason, critic_text = maybe_debate(
            judge_verdicts=judge_result.verdicts if judge_result else [],
            debate_cfg=debate_cfg,
            initial_generator_text=focus or "",
            round_num=round_num,
            verbose=verbose,
        )
        debate_critique = critic_text if debate_triggered else ""

        # 10. Track histories (for router + audit).
        if summary:
            bp = summary.get("best_property")
            if isinstance(bp, (int, float)):
                best_property_history.append(float(bp))
            bv = summary.get("best_vina")
            if isinstance(bv, (int, float)):
                best_vina_history.append(float(bv))
                bsv = summary.get("best_safe_vina")
                if isinstance(bsv, (int, float)):
                    best_safe_vina_history.append(float(bsv))
                recent_sa_scores = [
                    c.get("sa_score") for c in enriched
                    if isinstance(c.get("sa_score"), (int, float))
                ][:5]

        enriched_history.append(enriched)
        if summary:
            summary_history.append(summary)

        # 11. Record round log + per-round JSON.
        # LoopController: track both progress signals so the saved record
        # can be re-audited under either termination policy later.
        round_best_vina = summary.get("best_vina") if summary else None
        round_best_safe_vina = summary.get("best_safe_vina") if summary else None
        state.note_round_result(round_best_vina)
        state.note_round_safe_result(round_best_safe_vina)
        if verbose:
            print(f"  [controller] signal={progress_signal} "
                  f"patience={loop_controller.config.judge_convergence_patience} "
                  f"no_improvement={loop_controller.rounds_without_improvement(state)}")

        round_record = {
            "round": round_num,
            "run_id": run_id,
            "protocol_id": protocol_id,
            "is_mock": use_mock,
            "mode": "multi_agent",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "focus_used": focus,
            "focus_next": focus,
            "generator_outputs": per_gen,
            "candidates": enriched,
            "summary": summary,
            "judgment": (
                {"verdicts": [
                    {"name": v.judge_name, "score": v.score,
                     "rationale": v.rationale}
                    for v in (judge_result.verdicts if judge_result else [])
                ],
                "combined": judge_result.combined_score if judge_result else None,
                "dispersion": judge_result.dispersion if judge_result else None}
                if judge_result else None
            ),
            "parents_block_enabled": parents_block_enabled,
            "parents_k": parents_k,
            "parents_block_text": parents_block,
            "parents_used_smiles": [c.get("smiles") for c in parents_used],
            "memory_snapshot": {
                "recent_rounds": [
                    {
                        "round": r.round,
                        "n_valid": r.n_valid,
                        "best_vina": r.best_vina,
                        "n_unique_scaffolds": r.n_unique_scaffolds,
                    }
                    for r in memory.recent_rounds
                ] if memory_enabled else [],
                "best_so_far": memory.best_so_far.get("smiles")
                    if (memory_enabled and memory.best_so_far) else None,
            },
            "loop_state": {
                "round": state.round,
                "progress_signal": progress_signal,
                "rounds_without_vina_improvement": state.rounds_without_vina_improvement,
                "rounds_without_safe_vina_improvement": state.rounds_without_safe_vina_improvement,
            },
            "cache_stats": cache_stats,
            "experts_used": [g.get("prompt_role") for g in gens],
            "judge_combined": round(judge_result.combined_score, 4)
                if judge_result else None,
            "judge_dispersion": round(judge_result.dispersion, 4)
                if judge_result else None,
            "next_focus": focus[:200] if focus else "",
            "debate_triggered": debate_triggered,
            "debate_reason": debate_reason,
            "debate_critique_preview": critic_text[:120],
            "per_generator_counts": agg_stats.get("per_generator_count", {}),
            "aggregation_stats": agg_stats,
            "parents_marketplace_stats": parents_marketplace_stats,
            "n_candidates": len(candidates),
            "n_enriched": len(enriched),
            "summary_keys": sorted(summary.keys()) if summary else [],
        }
        rounds_log.append(round_record)
        round_file = out_path / f"round_{round_num}.json"
        round_file.write_text(
            json.dumps(round_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if verbose:
            print(f"  [save] {round_file}")

        # 12. Post-round stop check (mirror loop.py's bugfix that stopped
        #     waiting one extra round after patience counter tripped).
        post_stop, post_reason = loop_controller.should_stop(state)
        if post_stop:
            stop_reason = post_reason
            if verbose:
                print(f"  [controller] stop after round {round_num}: "
                      f"{loop_controller.explain(post_reason)}")
            break

        if debate_triggered and debate_cfg.get("enabled", False):
            # Stage 10: actually re-call each generator with the critic's
            # push-back injected into the prompt. New candidates are
            # merged into the per-generator pool and re-aggregated.
            try:
                debate_per_gen = debate_recall_generators(
                    config=config,
                    generators=gens,
                    n_per_generator=n_per_generator,
                    base_focus=focus,
                    base_weakness=weakness,
                    critic_text=critic_text,
                    memory_context=memory_context_current,
                    parents_block=parents_block,
                    use_mock=use_mock,
                    max_rounds=debate_cfg.get("max_rounds", 3),
                )
            except Exception as exc:
                log.warning("[multi_agent] debate re-call failed: %s", exc)
                debate_per_gen = {}
            # Merge into the existing per-generator pool.
            for gen_name, items in (debate_per_gen or {}).items():
                per_gen.setdefault(gen_name, []).extend(items)
            # Re-aggregate with the expanded pool so the debate
            # revisions can win the diversity ranking.
            agg, agg_stats = aggregate_round(per_gen, aggregation_cfg=aggregation_cfg)
            if verbose:
                print(f"  [debate] re-aggregated: input={agg_stats['n_input']} "
                      f"after_dedup={agg_stats['n_after_dedup']} "
                      f"kept={agg_stats['n_kept']}")
            # Re-flatten enriched / candidates for downstream evaluate.
            enriched, summary = evaluate_aggregated_candidates(
                candidates=[
                    {
                        "smiles": c.smiles,
                        "candidate_id": f"ma:r{round_num}:c{i}:debate",
                        "multi_agent_sources": list(c.sources),
                        "multi_agent_score": c.score,
                    }
                    for i, c in enumerate(agg)
                ],
                config=config,
            )
            if verbose and summary:
                print(f"  [debate] re-evaluate: n_total={summary.get('n_total')} "
                      f"n_valid={summary.get('n_valid')} "
                      f"best_property={summary.get('best_property')}")

    return {
        "mode": "multi_agent",
        "n_generators": len(gens),
        "n_judges": len(read_judges(loop_cfg)),
        "rounds_log": rounds_log,
        "warnings": warns,
        "stop_reason": stop_reason,
        "loop_state": {
            "progress_signal": progress_signal,
            "rounds_without_vina_improvement": state.rounds_without_vina_improvement,
            "rounds_without_safe_vina_improvement": state.rounds_without_safe_vina_improvement,
            "best_vina": state.best_vina,
            "best_safe_vina": state.best_safe_vina,
            "memory_rounds": len(memory.recent_rounds) if memory_enabled else 0,
            "failed_set_size": len(failed_set.failed),
        },
        "memory_dir": str(memory_base.resolve()) if (memory_enabled and not use_mock) else None,
        "manifest_path": str((out_path / "manifest.json").resolve()),
        "note": (
            "Stage 7 wiring: per-generator prompt_role + router dispatch "
            "+ evaluate + multi-judge vote + debate trigger. "
            "Phase 4.7 brings WorkingMemory + FailedLigandSet + LoopController "
            "(safe_vina) + manifest.json + EvaluationCache/DockingCache onto "
            "parity with loop.py. Generator re-call after debate is left as "
            "a follow-up (the debate critique is folded into the next round's "
            "focus instead)."
        ),
    }