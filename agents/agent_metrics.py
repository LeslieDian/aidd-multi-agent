"""agents/agent_metrics.py - Loop-level metrics for Phase 4.3 (P2-3) and
4-category calibration (Phase 4.4, 2026-09-27).

Aggregates per-round `summary` + `judgment` records into a single
metrics block that describes one run's trend.  A single run cannot establish
that feedback or memory caused an improvement; that claim requires repeated,
budget-matched control experiments.

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
- run_shows_improvement: final best Vina improved by at least 0.1 kcal/mol
- agent_is_learning: always null here; reserved for controlled experiments

Calibration block (Phase 4.4):
- n_evidence_rules, mean_abs_error, under_claim_rate, over_claim_rate,
  worst_pair.{parent_smiles, child_smiles, abs_error}
"""
from __future__ import annotations

from typing import Any, Optional


# Descriptive threshold for one run.  It is not a causal learning threshold.
LEARNING_DELTA_THRESHOLD = -0.1


def _safe(values: list, idx: int, default=None):
    if idx < 0 or idx >= len(values):
        return default
    return values[idx]


# ----------------------------------------------------------------------
# Phase 4.4 (2026-09-27): calibration metrics from a RuleStore.
# The 4-category RuleStore accumulates evidence rules from
# ``add_prediction_error`` (parent, child, predicted_delta, observed_delta,
# direction). We summarise those here so the run-level metrics.json
# surfaces model calibration, not just candidate scores.
# ----------------------------------------------------------------------


def compute_calibration_metrics(rule_store: Optional[Any]) -> dict[str, Any]:
    """Summarise the EVIDENCE (calibration) rules in `rule_store`.

    Returns a JSON-safe dict. Returns a zero-filled block when
    `rule_store` is None or has no EVIDENCE rules, so downstream readers
    don't have to special-case missing calibration.
    """
    empty = {
        "n_evidence_rules": 0,
        "n_observations": 0,
        "mean_abs_error": None,
        "median_abs_error": None,
        "max_abs_error": None,
        "under_claim_rate": None,
        "over_claim_rate": None,
        "by_direction": {"under": 0, "over": 0},
        "worst_pair": None,
    }
    if rule_store is None:
        return empty
    # Lazy import to avoid a hard cycle between agent_metrics and rule_memory.
    from agents.rule_memory import EVIDENCE
    rules = rule_store.by_category(EVIDENCE)
    if not rules:
        return empty
    errors: list[float] = []
    directions = {"under": 0, "over": 0}
    worst = None
    for r in rules:
        # Prefer observations_log (full history) over the top-level
        # predicted_delta / observed_delta, so accumulated rules report
        # every past mismatch, not just the latest one.
        log = r.pattern.get("observations_log") or []
        pairs: list[tuple[float, float]] = []
        if log:
            for entry in log:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    try:
                        pairs.append((float(entry[0]), float(entry[1])))
                    except (TypeError, ValueError):
                        continue
        if not pairs:
            # Old rules persisted before observations_log existed.
            try:
                pairs.append((float(r.pattern.get("predicted_delta", 0.0)),
                              float(r.pattern.get("observed_delta", 0.0))))
            except (TypeError, ValueError):
                continue
        for predicted, observed in pairs:
            err = abs(predicted - observed)
            errors.append(err)
            d = r.pattern.get("direction")
            if d in directions:
                directions[d] += 1
            if worst is None or err > worst["abs_error"]:
                worst = {
                    "parent_smiles": r.pattern.get("parent_smiles"),
                    "child_smiles": r.pattern.get("child_smiles"),
                    "predicted_delta": predicted,
                    "observed_delta": observed,
                    "abs_error": round(err, 4),
                    "direction": d,
                    "observations": r.observations,
                }
    if not errors:
        return {**empty, "n_evidence_rules": len(rules)}
    errors_sorted = sorted(errors)
    n = len(errors_sorted)
    median = errors_sorted[n // 2] if n % 2 else (
        errors_sorted[n // 2 - 1] + errors_sorted[n // 2]
    ) / 2
    n_under = directions["under"]
    n_over = directions["over"]
    n_dir = max(1, n_under + n_over)
    return {
        "n_evidence_rules": len(rules),
        "n_observations": sum(r.observations for r in rules),
        "mean_abs_error": round(sum(errors) / n, 4),
        "median_abs_error": round(median, 4),
        "max_abs_error": round(max(errors), 4),
        "under_claim_rate": round(n_under / n_dir, 3),
        "over_claim_rate": round(n_over / n_dir, 3),
        "by_direction": directions,
        "worst_pair": worst,
    }


def compute_agent_metrics(
    summary_history: list[dict],
    judgments: list[dict] | None = None,
    loop_state: dict | None = None,
    rule_store: Optional[Any] = None,
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
        rule_store: optional RuleStore (Phase 4.4). When present, an
            additional `calibration` block summarising EVIDENCE rules
            (predicted vs observed deltas) is included.

    Returns:
        dict with curves + aggregates + verdict + calibration. JSON-safe
        (no custom objects).
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
            if j.get("status") == "disabled":
                adoption_curve_llm.append(None)
                adoption_curve_det.append(None)
                drift_curve.append(None)
                continue
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

    run_shows_improvement = _run_trend(best_vina_curve, best_vina_delta)

    calibration = compute_calibration_metrics(rule_store)

    return {
        "schema_version": 3,
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
            "run_shows_improvement": run_shows_improvement,
            "run_rationale": _run_trend_rationale(best_vina_curve, best_vina_delta),
            "agent_is_learning": None,
            "rationale": (
                "not assessed from a single run; requires repeated, "
                "budget-matched control experiments"
            ),
        },
        "calibration": calibration,
        "calibration_rationale": (
            "Phase 4.4 EVIDENCE rule summary: |predicted_delta - observed_delta| "
            "for each insufficient_evidence screening row. under_claim_rate > 0.5 "
            "means the model systematically overstates the property gain of "
            "proposed edits; under_claim_rate < 0.5 means it understates them. "
            "Empty when no RuleStore is supplied or the run produced no "
            "insufficient_evidence rows."
        ),
    }


def _run_trend(vina_curve: list, vina_delta) -> bool | None:
    """Describe score change within a run; do not infer causal learning."""
    real = [v for v in vina_curve if v is not None]
    if len(real) < 2 or vina_delta is None:
        return None
    return vina_delta <= LEARNING_DELTA_THRESHOLD


def _run_trend_rationale(vina_curve, vina_delta) -> str:
    real = [v for v in vina_curve if v is not None]
    if vina_delta is None or len(real) < 2:
        return "insufficient data (need >= 2 rounds with Vina)"
    if vina_delta <= LEARNING_DELTA_THRESHOLD:
        return f"best_vina dropped by {-vina_delta:.3f} kcal/mol"
    return f"no final score improvement at the {abs(LEARNING_DELTA_THRESHOLD)} kcal/mol threshold"
