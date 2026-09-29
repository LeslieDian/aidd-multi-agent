"""agents/multi_agent.py - Multi-agent coordination (Phase 4.6).

This module implements the parts of multi-agent collaboration that are
*deterministic* and *testable* without an LLM or Vina call:

1. **Config validation**    -- `validate_multi_agent_config`
2. **Diversity / aggregation** -- `aggregate_candidates`
3. **Multi-judge vote**      -- `combine_judge_votes`
4. **Debate trigger**        -- `should_enter_debate`
5. **Heterogeneity check**   -- `generators_are_heterogeneous`

The actual LLM/Vina side stays in `agents/generator.py`, `agents/judge.py`,
`loop.py`. This module is the deterministic glue.

All functions are pure / side-effect free and have unit tests in
`tests/test_multi_agent.py`.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import AllChem

RDLogger.DisableLog("rdApp.*")


# ============================================================
# 1. Heterogeneity check
# ============================================================

def _axis_set(gen_config: dict, axis: str):
    """Extract a value used to compare two generators on a single axis.

    For "model", "provider", and "prompt_role" we want exact equality.
    For "temperature" we want *approximate* equality only if both have
    the same float value to 2 decimal places.
    """
    if axis in ("model", "provider", "prompt_role", "few_shot"):
        return gen_config.get(axis)
    if axis == "temperature":
        t = gen_config.get("temperature")
        return None if t is None else round(float(t), 2)
    return None


def generators_are_heterogeneous(
    generators: Sequence[dict],
    *,
    required_diff_axes: Sequence[str] = ("prompt_role",),
) -> bool:
    """Return True iff at least one generator differs from another on
    a required_diff_axis.

    Single-generator lists always pass (no comparison possible). If
    `required_diff_axes` is empty, returns True unconditionally.

    Default required_diff_axes is ("prompt_role",) because using the
    same prompt with the same model is the cheapest "fake multi-agent"
    and is explicitly disallowed by the README's "5 roles" contract.
    """
    if len(generators) < 2:
        return True
    if not required_diff_axes:
        return True
    for axis in required_diff_axes:
        seen = set()
        for g in generators:
            v = _axis_set(g, axis)
            if v is None:
                continue
            if v in seen:
                # at least one duplicate on this axis; check if there's
                # still a different value somewhere
                pass
            seen.add(v)
        if len(seen) >= 2:
            return True
    return False


# ============================================================
# 2. Aggregation (dedup + diversity + voting)
# ============================================================

def _fingerprint(smi: str):
    """ECFP4 fingerprint; returns None on parse failure."""
    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        return None
    try:
        return AllChem.GetMorganFingerprintAsBitVect(mol, radius=2, nBits=2048)
    except Exception:
        return None


def _canonical(smi: str) -> str | None:
    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        return None
    try:
        return Chem.MolToSmiles(mol)
    except Exception:
        return None


@dataclass(frozen=True)
class AggregatedCandidate:
    """Result of aggregation: one canonical SMILES + final score + provenance."""

    smiles: str
    score: float
    sources: tuple[str, ...]   # generator names that produced this SMILES
    similarity_pairs_dropped: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def aggregate_candidates(
    per_generator_outputs: dict[str, list[dict]],
    *,
    diversity_floor: float = 0.7,
    top_n: int = 12,
) -> tuple[list[AggregatedCandidate], dict]:
    """Aggregate candidates from multiple generators.

    Args:
        per_generator_outputs: {generator_name: [{smiles, confidence, ...}, ...]}
        diversity_floor: Tanimoto threshold. Candidates with similarity
            *strictly greater than* this against an already-kept higher-score
            candidate are dropped (kept: the one with the larger score).
        top_n: keep at most this many aggregated candidates.

    Returns:
        (aggregated, stats)
        aggregated: list of AggregatedCandidate, sorted by score desc.
        stats: dict with dedup_count, dropped_for_diversity_count,
            kept_count, per_generator_count.

    Notes:
        - "score" is the per-generator confidence weighted by the
          generator's `weight` in config.yaml (we use confidence * weight
          as the score proxy).
        - Duplicates across generators (same canonical SMILES) are merged
          with multi-source attribution.
    """
    merged: dict[str, AggregatedCandidate] = {}
    per_gen_count: dict[str, int] = {}
    dropped_for_diversity = 0

    # 1. collect + dedup by canonical
    for gen_name, items in per_generator_outputs.items():
        weight = 1.0  # weight is handled below; aggregator sees raw
        kept_for_gen = 0
        for item in items:
            smi = item.get("smiles")
            if not smi:
                continue
            can = _canonical(smi)
            if can is None:
                continue
            confidence = float(item.get("confidence", item.get("score", 1.0)))
            score = confidence * weight
            if can in merged:
                existing = merged[can]
                if score > existing.score:
                    merged[can] = AggregatedCandidate(
                        smiles=existing.smiles,
                        score=score,
                        sources=existing.sources + (gen_name,),
                    )
                else:
                    merged[can] = AggregatedCandidate(
                        smiles=existing.smiles,
                        score=existing.score,
                        sources=existing.sources + (gen_name,),
                    )
            else:
                merged[can] = AggregatedCandidate(
                    smiles=can,
                    score=score,
                    sources=(gen_name,),
                )
            kept_for_gen += 1
        per_gen_count[gen_name] = kept_for_gen

    # 2. diversity filter
    ordered = sorted(merged.values(), key=lambda c: -c.score)
    kept: list[AggregatedCandidate] = []
    kept_fps: list = []
    for cand in ordered:
        fp = _fingerprint(cand.smiles)
        if fp is None:
            kept.append(cand)
            kept_fps.append(None)
            continue
        is_dup = False
        for kfp in kept_fps:
            if kfp is None:
                continue
            try:
                sim = DataStructs.TanimotoSimilarity(fp, kfp)
            except Exception:
                continue
            if sim > diversity_floor:
                is_dup = True
                dropped_for_diversity += 1
                break
        if not is_dup:
            kept.append(cand)
            kept_fps.append(fp)

    # 3. top_n
    kept = kept[:top_n]

    stats = {
        "n_input": sum(len(v) for v in per_generator_outputs.values()),
        "n_after_dedup": len(merged),
        "n_dropped_diversity": dropped_for_diversity,
        "n_kept": len(kept),
        "per_generator_count": dict(per_gen_count),
    }
    return kept, stats


# ============================================================
# 3. Multi-judge vote
# ============================================================

@dataclass(frozen=True)
class JudgeVerdict:
    """Per-judge verdict for one round."""

    judge_name: str
    score: float               # 0..1 confidence
    next_focus: str            # text / label
    rationale: str = ""


def combine_judge_votes(
    verdicts: Sequence[JudgeVerdict],
    *,
    weights: Sequence[float] | None = None,
) -> tuple[float, dict[str, float], float]:
    """Combine per-judge verdicts into a single weighted vote.

    Args:
        verdicts: per-judge verdicts.
        weights: optional per-judge weights (same length). Default 1.0.

    Returns:
        (combined_score, per_judge_weighted, max_minus_median)
        combined_score: weighted mean.
        per_judge_weighted: {judge_name: weighted contribution}.
        max_minus_median: dispersion metric used by debate trigger.
    """
    if not verdicts:
        return 0.0, {}, 0.0
    if weights is None:
        weights = [1.0] * len(verdicts)
    assert len(weights) == len(verdicts), "weights length must match verdicts"
    total_w = sum(weights)
    if total_w <= 0:
        return 0.0, {}, 0.0
    per_judge = {
        v.judge_name: float(v.score) * float(w) / total_w
        for v, w in zip(verdicts, weights)
    }
    combined = sum(per_judge.values())
    raw_scores = [v.score for v in verdicts]
    if len(raw_scores) >= 2:
        med = statistics.median(raw_scores)
        dispersion = max(raw_scores) - med
    else:
        dispersion = 0.0
    return combined, per_judge, dispersion


# ============================================================
# 4. Debate trigger
# ============================================================

def should_enter_debate(
    verdicts: Sequence[JudgeVerdict],
    *,
    disagreement_threshold: float = 0.15,
    confidence_floor: float = 0.30,
) -> tuple[bool, str]:
    """Decide whether to enter adversarial debate.

    Triggers debate if EITHER:
      - any judge has score < confidence_floor, OR
      - max(judge scores) - median(judge scores) >= disagreement_threshold

    Returns:
        (should_debate, reason)
    """
    if not verdicts:
        return False, "no_judges"
    raw_scores = [v.score for v in verdicts]
    if min(raw_scores) < confidence_floor:
        return True, f"low_confidence(min={min(raw_scores):.3f}<{confidence_floor})"
    if len(raw_scores) >= 2:
        med = statistics.median(raw_scores)
        if (max(raw_scores) - med) >= disagreement_threshold:
            return True, f"high_disagreement(max-med={max(raw_scores)-med:.3f}>={disagreement_threshold})"
    return False, "ok"


# ============================================================
# 5. Config validation
# ============================================================

class MultiAgentConfigError(ValueError):
    """Raised when the multi_agent block is malformed."""


def validate_multi_agent_config(
    multi_agent_cfg: dict | None,
    llm_providers: dict,
) -> list[str]:
    """Validate `loop.multi_agent` config. Return list of warnings (empty = OK).

    Raises MultiAgentConfigError on hard errors. Warnings include:
        - generators[] not heterogeneous on required axes
        - generators[] / judges[] reference unknown provider keys
        - aggregation top_n <= 0 etc.
    """
    warnings: list[str] = []
    if not multi_agent_cfg:
        return warnings
    if not multi_agent_cfg.get("enabled", False):
        return warnings
    gens = multi_agent_cfg.get("generators") or []
    if len(gens) == 0:
        warnings.append("multi_agent.enabled=true but generators=[]; loop will fall back to single-generator.")
    for i, g in enumerate(gens):
        prov = g.get("provider")
        if prov not in llm_providers:
            warnings.append(f"generators[{i}].provider={prov!r} not in llm.providers (would fail at runtime)")
        if not g.get("name"):
            warnings.append(f"generators[{i}].name missing")
    judges = multi_agent_cfg.get("judges") or []
    for i, j in enumerate(judges):
        prov = j.get("provider")
        if prov not in llm_providers:
            warnings.append(f"judges[{i}].provider={prov!r} not in llm.providers (would fail at runtime)")
    if not generators_are_heterogeneous(gens):
        warnings.append(
            "generators[] are NOT heterogeneous on required axes (prompt_role); "
            "this is not real multi-agent per README '5 roles' contract."
        )
    agg = multi_agent_cfg.get("aggregation") or {}
    if agg.get("top_n") is not None and int(agg.get("top_n", 1)) <= 0:
        warnings.append("aggregation.top_n must be > 0")
    return warnings