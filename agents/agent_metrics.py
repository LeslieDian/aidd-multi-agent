"""agents/agent_metrics.py - Loop-level metrics for Phase 4.3 (P2-3).

Aggregates per-round `summary` + `judgment` records into a single
metrics block that answers the question "is the agent actually learning
across rounds?". This is the metric layer the original Phase 4.1/4.2
design lacked — without it the user can only eyeball `summary.json`
history.

Curves (per round):
- valid_rate_curve:      list[float], fraction of valid candidates
- best_vina_curve:       list[float | None], best Vina in each round
- scaffold_diversity_curve: list[int], n_unique_scaffolds
- avg_admet_curve:       list[float | None]
- adoption_rate_curve_llm:  list[float | None], Judge LLM self-report
- adoption_rate_curve_det:  list[float | None], Tanimoto>thr ground truth

Aggregates (scalar):
- valid_rate_first, valid_rate_last, valid_rate_improvement
- best_vina_first, best_vina_last, best_vina_delta  (negative = improvement)
- adoption_rate_avg_llm, adoption_rate_avg_det
- adoption_llm_vs_det_drift_avg
- scaffold_diversity_first, scaffold_diversity_last
- rounds_total
- rounds_without_improvement
- agent_is_learning: best_vina_delta < -0.1 OR monotonically non-increasing
                     across the last 3 rounds
"""
from __future__ import annotations

from typing import Any


# Tunable: "agent is learning" = last 3 best_vinas are non-increasing
# (each round at least as good as the prior) OR a single > 0.1 kcal/mol
# improvement from R0 to last round. 0.1 is conservative; the prior Phase
# C noise floor was about +/-0.2.
LEARNING_DELTA_THRESHOLD = -0.1


def _safe(values: list, idx: int, default=None):
    if idx < 0 or idx >= len(values):
        return default
    return values[idx]


def compute_agent_metrics(
    summary_history: list[dict],
    judgments: list[dict] | None = None,
    loop_state: dict | None = None,
) -> dict[str, Any]:
    """Build the metrics block from per-round summary + judgment records.

    Args:
        summary_history: list of `summarize_round()` outputs (one per round).
            Each dict has n_total, n_valid, valid_ratio, n_unique_scaffolds,
            avg_admet, best_vina, ...
        judgments: list of `judge_round()` outputs (one per round). Round 0
            may have empty reflection; that's fine.
        loop_state: optional LoopState snapshot, used for
            `rounds_without_improvement`.

    Returns:
        dict with curves + aggregates + verdict. JSON-safe (no custom
        objects).
    """
    judgments = judgments or []
    loop_state = loop_state or {}

    n = len(summary_history)
    valid_rate_curve: list[float] = []
    best_vina_curve: list[Any] = []
    scaffold_curve: list[int] = []
    avg_admet_curve: list[Any] = []
    adoption_curve_llm: list[Any] = []
    adoption_curve_det: list[Any] = []
    drift_curve: list[Any] = []

    for i, s in enumerate(summary_history):
        valid_rate_curve.append(float(s.get("valid_ratio") or 0.0))
        best_vina_curve.append(s.get("best_vina"))
        scaffold_curve.append(int(s.get("n_unique_scaffolds") or 0))
        avg_admet_curve.append(s.get("avg_admet"))
        if i < len(judgments):
            j = judgments[i]
            denom = max(1, j.get("adoption_denominator", 0) or 0)
            adoption_curve_llm.append(
                round(j.get("adopted_count", 0) / denom, 3) if denom else None
            )
            det = j.get("adoption_deterministic") or {}
            adoption_curve_det.append(det.get("adoption_rate"))
            drift_curve.append(j.get("adoption_llm_vs_det_drift"))
        else:
            adoption_curve_llm.append(None)
            adoption_curve_det.append(None)
            drift_curve.append(None)

    # Aggregates
    valid_rate_first = valid_rate_curve[0] if valid_rate_curve else None
    valid_rate_last = valid_rate_curve[-1] if valid_rate_curve else None
    valid_rate_improvement = (
        round(valid_rate_last - valid_rate_first, 3)
        if valid_rate_first is not None and valid_rate_last is not None
        else None
    )

    # Filter None before taking first/last
    vina_real = [v for v in best_vina_curve if v is not None]
    best_vina_first = vina_real[0] if vina_real else None
    best_vina_last = vina_real[-1] if vina_real else None
    best_vina_delta = (
        round(best_vina_last - best_vina_first, 3)
        if best_vina_first is not None and best_vina_last is not None
        else None
    )

    ad_llm_real = [v for v in adoption_curve_llm if v is not None]
    ad_det_real = [v for v in adoption_curve_det if v is not None]
    drift_real = [v for v in drift_curve if v is not None]
    adoption_rate_avg_llm = (
        round(sum(ad_llm_real) / len(ad_llm_real), 3) if ad_llm_real else None
    )
    adoption_rate_avg_det = (
        round(sum(ad_det_real) / len(ad_det_real), 3) if ad_det_real else None
    )
    drift_avg = (
        round(sum(drift_real) / len(drift_real), 3) if drift_real else None
    )

    scaffold_diversity_first = scaffold_curve[0] if scaffold_curve else None
    scaffold_diversity_last = scaffold_curve[-1] if scaffold_curve else None

    # Verdict
    agent_is_learning = _verdict(
        best_vina_curve,
        best_vina_delta,
        valid_rate_improvement,
        scaffold_diversity_first,
        scaffold_diversity_last,
    )

    return {
        "schema_version": 1,
        "rounds_total": n,
        "rounds_without_improvement": loop_state.get(
            "rounds_without_vina_improvement"
        ),
        "curves": {
            "valid_rate": valid_rate_curve,
            "best_vina": best_vina_curve,
            "scaffold_diversity": scaffold_curve,
            "avg_admet": avg_admet_curve,
            "adoption_rate_llm": adoption_curve_llm,
            "adoption_rate_deterministic": adoption_curve_det,
            "adoption_llm_vs_det_drift": drift_curve,
        },
        "aggregates": {
            "valid_rate_first": valid_rate_first,
            "valid_rate_last": valid_rate_last,
            "valid_rate_improvement": valid_rate_improvement,
            "best_vina_first": best_vina_first,
            "best_vina_last": best_vina_last,
            "best_vina_delta": best_vina_delta,
            "scaffold_diversity_first": scaffold_diversity_first,
            "scaffold_diversity_last": scaffold_diversity_last,
            "adoption_rate_avg_llm": adoption_rate_avg_llm,
            "adoption_rate_avg_det": adoption_rate_avg_det,
            "adoption_llm_vs_det_drift_avg": drift_avg,
        },
        "verdict": {
            "agent_is_learning": agent_is_learning,
            "rationale": _verdict_rationale(
                best_vina_curve, best_vina_delta, valid_rate_improvement,
                scaffold_diversity_first, scaffold_diversity_last,
            ),
        },
    }


def _verdict(
    vina_curve: list, vina_delta, valid_improvement,
    scaf_first, scaf_last,
) -> bool | None:
    """`None` when not enough data; otherwise True/False."""
    real = [v for v in vina_curve if v is not None]
    if len(real) < 2 or vina_delta is None:
        return None
    # Strong signal: across-the-board drop in best_vina >= 0.1
    if vina_delta <= LEARNING_DELTA_THRESHOLD:
        return True
    # Or: last 3 rounds non-increasing (no upward regression)
    if len(real) >= 3:
        tail = real[-3:]
        if tail[0] >= tail[1] >= tail[2]:
            return True
    # Or: scaffolds doubled (we are exploring)
    if (scaf_first is not None and scaf_last is not None
            and scaf_first > 0 and scaf_last >= 2 * scaf_first):
        return True
    return False


def _verdict_rationale(
    vina_curve, vina_delta, valid_improvement, scaf_first, scaf_last
) -> str:
    real = [v for v in vina_curve if v is not None]
    if vina_delta is None or len(real) < 2:
        return "insufficient data (need >= 2 rounds with Vina)"
    if vina_delta <= LEARNING_DELTA_THRESHOLD:
        return f"best_vina dropped by {-vina_delta:.3f} kcal/mol (>= {abs(LEARNING_DELTA_THRESHOLD)})"
    if len(real) >= 3 and real[-3] >= real[-2] >= real[-1]:
        return f"last 3 rounds non-increasing: {[round(v,3) for v in real[-3:]]}"
    if (scaf_first is not None and scaf_last is not None
            and scaf_first > 0 and scaf_last >= 2 * scaf_first):
        return f"scaffold diversity doubled: {scaf_first} -> {scaf_last}"
    return f"no monotonic improvement (best_vina delta = {vina_delta}); loop may be stalled"