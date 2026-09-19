"""Aggregate budget-matched AIDD benchmark runs into JSON and Markdown."""
from __future__ import annotations

import json
import math
import statistics
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any
from experiments.contract import treatments

try:
    from scipy import stats as scipy_stats
except Exception:  # pragma: no cover - report still works without scipy
    scipy_stats = None


METRIC_SPECS = {
    "best_composite_global": ("max", "Best composite"),
    "top5_composite_mean": ("max", "Top-5 composite mean"),
    "best_safe_composite_global": ("max", "Best safety-passing composite"),
    "top5_safe_composite_mean": ("max", "Top-5 safety-passing composite mean"),
    "best_vina_global": ("min", "Best Vina"),
    "top5_vina_mean": ("min", "Top-5 Vina mean"),
    "best_safe_vina_global": ("min", "Best Vina among safety-passing"),
    "top5_safe_vina_mean": ("min", "Top-5 safe Vina mean"),
    "valid_rate": ("max", "Valid rate"),
    "unique_valid_smiles": ("max", "Unique valid molecules"),
    "unique_scaffolds": ("max", "Unique scaffolds"),
    "herg_flag_rate": ("min", "hERG flag rate"),
    "mean_herg_risk_score": ("min", "Mean hERG risk proxy"),
    "safety_pass_rate": ("max", "Safety-gate pass rate"),
    "pareto_front_candidates": ("max", "Pareto-front candidates"),
    "safe_pareto_candidates": ("max", "Safe Pareto-front candidates"),
    "cross_round_duplicates": ("min", "Cross-round duplicates"),
    "best_safe_vina_delta": ("min", "First-to-last safety-passing Vina delta"),
    "safe_run_improvement_rate": ("max", "Runs with safe first-to-last Vina improvement"),
    "best_vina_delta": ("min", "First-to-last Vina delta"),
    "run_improvement_rate": ("max", "Runs showing improvement"),
    "adoption_rate_avg_llm": ("neutral", "Judge-reported adoption"),
    "adoption_rate_avg_det": ("max", "Structural adoption"),
    "adoption_llm_vs_det_drift_avg": ("neutral", "Adoption count drift"),
    "tokens_used": ("min", "Tokens"),
    "duration_seconds": ("min", "Duration seconds"),
    "cache_hits": ("neutral", "Cache hits"),
    "new_dockings": ("min", "New dockings"),
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mean_top(values: list[float], k: int, reverse: bool) -> float | None:
    if not values:
        return None
    chosen = sorted(values, reverse=reverse)[:k]
    return round(statistics.mean(chosen), 6)


def extract_run_metrics(run_dir: str | Path) -> dict[str, Any]:
    run_path = Path(run_dir)
    summary = json.loads((run_path / "summary.json").read_text(encoding="utf-8"))
    meta_path = run_path / "benchmark_run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    rounds = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(run_path.glob("round_*.json"))
    ]

    raw_proposals = 0
    batch_duplicates = 0
    known_failed_removed = 0
    candidates: list[dict] = []
    for record in rounds:
        filt = (record.get("summary") or {}).get("candidate_filter") or {}
        raw_proposals += int(filt.get("raw") or 0)
        batch_duplicates += int(filt.get("batch_duplicates_removed") or 0)
        known_failed_removed += int(filt.get("known_failed_removed") or 0)
        candidates.extend(record.get("candidates") or [])

    valid = [c for c in candidates if (c.get("validate") or {}).get("valid")]
    complete = [c for c in candidates if c.get("evaluation_status") == "complete"]
    canonical_counts: dict[str, int] = {}
    unique_records: dict[str, dict] = {}
    for candidate in valid:
        canonical = (candidate.get("validate") or {}).get("smiles") or candidate.get("smiles")
        canonical_counts[canonical] = canonical_counts.get(canonical, 0) + 1
        current = unique_records.get(canonical)
        if current is None or (
            _finite(candidate.get("composite_score"))
            and candidate.get("composite_score") > (current.get("composite_score") or -math.inf)
        ):
            unique_records[canonical] = candidate

    unique_complete = [
        row for row in unique_records.values() if row.get("evaluation_status") == "complete"
    ]
    safe_complete = [row for row in unique_complete if row.get("safety_gate_pass") is True]
    composites = [float(c["composite_score"]) for c in unique_complete if _finite(c.get("composite_score"))]
    safe_composites = [
        float(c["composite_score"]) for c in safe_complete if _finite(c.get("composite_score"))
    ]
    vinas = [float((c.get("dock") or {})["score"]) for c in unique_complete
             if _finite((c.get("dock") or {}).get("score"))]
    # Safety-gated twin of `vinas`. Computed from the candidate records rather
    # than from `summary.best_safe_vina`, so it can also be reported for runs
    # saved before that field existed.
    safe_vinas = [float((c.get("dock") or {})["score"]) for c in safe_complete
                  if _finite((c.get("dock") or {}).get("score"))]
    scaffolds = {c.get("scaffold") for c in valid if c.get("scaffold")}
    herg_values = [float((c.get("admet") or {}).get("herg_risk")) for c in unique_complete
                   if _finite((c.get("admet") or {}).get("herg_risk"))]
    herg_risk_scores = [
        float((c.get("admet") or {}).get("herg_risk_score")) for c in unique_complete
        if _finite((c.get("admet") or {}).get("herg_risk_score"))
    ]
    cache_hits = sum(bool((c.get("evaluation_cache") or {}).get("hit")) for c in candidates)
    new_dockings = sum(
        c.get("evaluation_status") == "complete"
        and bool((c.get("dock") or {}).get("valid"))
        and not bool((c.get("evaluation_cache") or {}).get("hit"))
        for c in candidates
    )
    agent_metrics = summary.get("agent_metrics") or {}
    run_improved = agent_metrics.get("run_shows_improvement")

    safe_rounds = []
    for record in rounds:
        scores = [float(c['dock']['score']) for c in record.get('candidates', [])
                  if c.get('evaluation_status') == 'complete' and c.get('safety_gate_pass') is True
                  and _finite((c.get('dock') or {}).get('score'))]
        safe_rounds.append(min(scores) if scores else None)
    safe_delta = (safe_rounds[-1] - safe_rounds[0] if len(safe_rounds) >= 2
                  and safe_rounds[0] is not None and safe_rounds[-1] is not None else None)
    return {
        'best_safe_vina_delta': safe_delta,
        'safe_run_improvement_rate': float(safe_delta < 0) if safe_delta is not None else None,
        "run_dir": str(run_path.resolve()),
        "run_id": summary.get("run_id"),
        "group": meta.get("group"),
        "repeat": meta.get("repeat"),
        "status": summary.get("status"),
        "rounds_completed": summary.get("rounds_completed"),
        "raw_proposals": raw_proposals,
        "evaluated_candidates": len(candidates),
        "valid_candidates": len(valid),
        "complete_candidates": len(complete),
        "invalid_candidates": len(candidates) - len(valid),
        "batch_duplicates": batch_duplicates,
        "known_failed_removed": known_failed_removed,
        "cross_round_duplicates": sum(max(0, count - 1) for count in canonical_counts.values()),
        "best_vina_delta": agent_metrics.get("best_vina_delta"),
        "run_improvement_rate": float(run_improved) if isinstance(run_improved, bool) else None,
        "adoption_rate_avg_llm": agent_metrics.get("adoption_rate_avg_llm"),
        "adoption_rate_avg_det": agent_metrics.get("adoption_rate_avg_det"),
        "adoption_llm_vs_det_drift_avg": agent_metrics.get("adoption_llm_vs_det_drift_avg"),
        "unique_valid_smiles": len(unique_records),
        "unique_scaffolds": len(scaffolds),
        "valid_rate": round(len(valid) / max(1, len(candidates)), 6),
        "best_composite_global": max(composites) if composites else None,
        "top5_composite_mean": _mean_top(composites, 5, reverse=True),
        "best_safe_composite_global": max(safe_composites) if safe_composites else None,
        "top5_safe_composite_mean": _mean_top(safe_composites, 5, reverse=True),
        "best_vina_global": min(vinas) if vinas else None,
        "top5_vina_mean": _mean_top(vinas, 5, reverse=False),
        "best_safe_vina_global": min(safe_vinas) if safe_vinas else None,
        "top5_safe_vina_mean": _mean_top(safe_vinas, 5, reverse=False),
        "herg_flag_rate": round(statistics.mean(herg_values), 6) if herg_values else None,
        "mean_herg_risk_score": (
            round(statistics.mean(herg_risk_scores), 6) if herg_risk_scores else None
        ),
        "safety_pass_rate": round(len(safe_complete) / max(1, len(unique_complete)), 6),
        "pareto_front_candidates": sum(c.get("pareto_rank") == 1 for c in candidates),
        "safe_pareto_candidates": sum(
            c.get("pareto_rank") == 1 and c.get("safety_gate_pass") is True for c in candidates
        ),
        "cache_hits": cache_hits,
        "new_dockings": new_dockings,
        "tokens_used": summary.get("tokens_used") or 0,
        "duration_seconds": meta.get("duration_seconds"),
    }


def _summary_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "stdev": None, "ci95": [None, None], "values": []}
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) >= 2 else None
    if len(values) >= 2 and scipy_stats is not None:
        half = float(scipy_stats.t.ppf(0.975, len(values) - 1)) * stdev / math.sqrt(len(values))
        ci = [mean - half, mean + half]
    else:
        ci = [None, None]
    return {
        "n": len(values),
        "mean": round(mean, 6),
        "stdev": round(stdev, 6) if stdev is not None else None,
        "ci95": [round(x, 6) if x is not None else None for x in ci],
        "values": [round(x, 6) for x in values],
    }


def _compare_metric_groups(reference: dict, treatment: dict) -> dict[str, Any]:
    comparison = {}
    for metric, (direction, _) in METRIC_SPECS.items():
        a = reference.get(metric, {}).get("values", [])
        b = treatment.get(metric, {}).get("values", [])
        delta = (statistics.mean(b) - statistics.mean(a)) if a and b else None
        p_value = None
        if len(a) >= 2 and len(b) >= 2 and scipy_stats is not None:
            if statistics.pstdev(a) > 0 or statistics.pstdev(b) > 0:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    test = scipy_stats.ttest_ind(b, a, equal_var=False)
                if _finite(float(test.pvalue)):
                    p_value = float(test.pvalue)
        delta_ci95 = _welch_delta_ci(a, b)
        favorable = None
        if delta is not None and direction in {"min", "max"}:
            favorable = delta < 0 if direction == "min" else delta > 0
        comparison[metric] = {
            "delta_vs_reference": round(delta, 6) if delta is not None else None,
            "delta_ci95": delta_ci95,
            "direction": direction,
            "favorable": favorable,
            "welch_p_value": round(p_value, 6) if p_value is not None else None,
            "statistically_significant": bool(p_value is not None and p_value < 0.05),
        }
    return comparison


def _welch_delta_ci(reference: list[float], treatment: list[float], alpha: float = .05) -> list[float | None]:
    """95% Welch interval for treatment minus reference."""
    if len(reference) < 2 or len(treatment) < 2 or scipy_stats is None:
        return [None, None]
    delta = statistics.mean(treatment) - statistics.mean(reference)
    ref_term = statistics.variance(reference) / len(reference)
    treatment_term = statistics.variance(treatment) / len(treatment)
    variance = ref_term + treatment_term
    if variance == 0:
        return [round(delta, 6), round(delta, 6)]
    denominator = (
        (ref_term ** 2) / (len(reference) - 1)
        + (treatment_term ** 2) / (len(treatment) - 1)
    )
    degrees = (variance ** 2) / denominator if denominator else min(
        len(reference), len(treatment)
    ) - 1
    half = float(scipy_stats.t.ppf(1 - alpha / 2, degrees)) * math.sqrt(variance)
    return [round(delta - half, 6), round(delta + half, 6)]


def build_report(benchmark_dir: str | Path) -> dict[str, Any]:
    root = Path(benchmark_dir)
    manifest = json.loads((root / "benchmark_manifest.json").read_text(encoding="utf-8"))
    runs = []
    excluded_runs = []
    represented_dirs = set()
    for group in manifest["groups"]:
        nested = list((root / group).glob("repeat_*/attempt_*"))
        legacy = [path for path in (root / group).glob("repeat_*") if (path / "summary.json").exists()]
        for run_dir in sorted(nested + legacy):
            if (run_dir / "summary.json").exists():
                represented_dirs.add(str(run_dir.resolve()))
                metrics = extract_run_metrics(run_dir)
                meta_path = run_dir / "benchmark_run.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
                if meta.get("eligible", True):
                    runs.append(metrics)
                else:
                    metrics["quality_reasons"] = meta.get("quality_reasons", [])
                    excluded_runs.append(metrics)
    for meta in manifest.get("runs", []):
        run_dir = meta.get("run_dir")
        if meta.get("eligible", False) or not run_dir or run_dir in represented_dirs:
            continue
        excluded_runs.append({
            "run_dir": run_dir,
            "run_id": meta.get("run_id"),
            "group": meta.get("group"),
            "repeat": meta.get("repeat"),
            "status": meta.get("status"),
            "quality_reasons": meta.get("quality_reasons", []),
            "duration_seconds": meta.get("duration_seconds"),
        })

    groups: dict[str, Any] = {}
    for group in manifest["groups"]:
        group_runs = [run for run in runs if run.get("group") == group]
        metrics = {}
        for metric in METRIC_SPECS:
            values = [float(run[metric]) for run in group_runs if _finite(run.get(metric))]
            metrics[metric] = _summary_stats(values)
        groups[group] = {
            "description": manifest["groups"][group].get("description", ""),
            "runs": len(group_runs),
            "metrics": metrics,
        }

    baseline_name = manifest.get("baseline_group", "baseline")
    comparisons: dict[str, Any] = {}
    baseline = groups.get(baseline_name, {}).get("metrics", {})
    for group, group_data in groups.items():
        if group == baseline_name:
            continue
        comparison = _compare_metric_groups(baseline, group_data["metrics"])
        for result in comparison.values():
            result["delta_vs_baseline"] = result.pop("delta_vs_reference")
        comparisons[group] = comparison

    incremental_comparisons = {}
    for reference, treatment in [('reflection', 'reflection_memory'),
                                 ('reflection', 'reflection_failed_set'),
                                 ('reflection_failed_set', 'reflection_memory')]:
        if reference in groups and treatment in groups:
            incremental_comparisons[f'{treatment}_vs_{reference}'] = _compare_metric_groups(
                groups[reference]['metrics'], groups[treatment]['metrics'])

    primary = manifest["execution"].get("primary_metric", "best_composite_global")
    confirmatory_decision = _confirmatory_decision(
        manifest, groups, comparisons, primary
    )
    return {
        'attempt_accounting': {
            g: {'eligible': sum(r.get('group') == g for r in runs),
                'excluded': sum(r.get('group') == g for r in excluded_runs),
                'recorded_tokens_all_attempts': sum(r.get('tokens_used') or 0 for r in runs + excluded_runs if r.get('group') == g),
                'seconds_all_attempts': sum(r.get('duration_seconds') or 0 for r in runs + excluded_runs if r.get('group') == g),
                'tokens_note': 'Recorded usage only; failed requests may have unknown billed usage.'}
            for g in groups},
        'analysis_version': '20260919-contract-review',
        "schema_version": 2,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark_id": manifest["benchmark_id"],
        "primary_metric": primary,
        "baseline_group": baseline_name,
        "groups": groups,
        "runs": runs,
        "excluded_runs": excluded_runs,
        "comparisons": comparisons,
        "incremental_comparisons": incremental_comparisons,
        "confirmatory_decision": confirmatory_decision,
        "early_stop": manifest.get("early_stop"),
        "interpretation": _interpret(groups, comparisons, baseline_name, primary),
    }


def _confirmatory_decision(
    manifest: dict, groups: dict, comparisons: dict, primary: str,
) -> dict | None:
    spec = manifest.get("confirmatory") or {}
    if not spec:
        return None
    reference = spec.get("reference_group", "reflection")
    arms = treatments(spec)
    if len(arms) > 1:
        from copy import deepcopy
        decisions = {}
        alpha = float(spec.get('alpha', .05)) / len(arms)
        for arm in arms:
            child = deepcopy(manifest)
            child['confirmatory'].pop('treatment_groups', None)
            child['confirmatory'].update(treatment_group=arm, alpha=alpha)
            decisions[arm] = _confirmatory_decision(child, groups, comparisons, primary)
        return {'status': 'per_treatment_decisions', 'multiplicity': 'bonferroni',
                'family_alpha': spec.get('alpha', .05), 'per_treatment_alpha': alpha,
                'treatments': decisions, 'automatic_default_change': False}
    treatment = arms[0]
    if reference not in groups or treatment not in groups:
        return {"status": "invalid_design", "reason": "missing confirmatory group"}
    comparison = comparisons.get(treatment) if manifest.get("baseline_group") == reference else None
    if not comparison:
        comparison = _compare_metric_groups(
            groups[reference]["metrics"], groups[treatment]["metrics"]
        )
    primary_result = comparison.get(primary) or {}
    safety_metric = spec.get("safety_metric", "mean_herg_risk_score")
    safety_result = comparison.get(safety_metric) or {}
    safety_margin = float(spec.get("safety_noninferiority_margin", 0.05))
    alpha = float(spec.get('alpha', .05))
    safety_ci = safety_result.get('delta_ci95') or [None, None]
    if alpha != .05:
        safety_ci = _welch_delta_ci(groups[reference]['metrics'].get(safety_metric, {}).get('values', []),
                                   groups[treatment]['metrics'].get(safety_metric, {}).get('values', []), alpha)
    safety_upper = safety_ci[1]
    p = primary_result.get('welch_p_value')
    significant = (p < alpha if _finite(p) else
                   bool(primary_result.get('statistically_significant')) if alpha == .05 else False)
    efficacy_supported = bool(
        primary_result.get("favorable")
        and significant
    )
    safety_noninferior = bool(
        safety_upper is not None and safety_upper <= safety_margin
    )
    improved_run_rate = (
        groups[treatment]["metrics"].get(spec.get("improvement_metric", "run_improvement_rate"), {}).get("mean")
    )
    min_improved = float(spec.get("min_improved_run_rate", 0.7))
    vina_delta = groups[treatment]["metrics"].get(spec.get("improvement_delta_metric", "best_vina_delta"), {}).get("mean")
    stable_improvement = bool(
        improved_run_rate is not None and improved_run_rate >= min_improved
        and vina_delta is not None and vina_delta < 0
    )
    planned = manifest.get('execution', {}).get('repeats')
    complete = planned is None or all(groups[g].get('runs', 0) >= planned for g in [reference, treatment])
    if planned is not None:
        endpoint_coverage = all(groups[g]['metrics'].get(primary, {}).get('n', 0) >= planned
                                and groups[g]['metrics'].get(safety_metric, {}).get('n', 0) >= planned
                                for g in [reference, treatment])
        endpoint_coverage = endpoint_coverage and groups[treatment]['metrics'].get(
            spec.get('improvement_metric', 'run_improvement_rate'), {}).get('n', 0) >= planned
    else:
        endpoint_coverage = True
    approved = endpoint_coverage and complete and efficacy_supported and safety_noninferior and stable_improvement
    return {
        "status": ("approve_long_term_memory" if treatment == 'reflection_memory' else 'approve_treatment') if approved else ("do_not_approve" if complete or manifest.get('early_stop') else 'insufficient_repeats'),
        'complete_planned_sample': complete,
        'complete_endpoint_coverage': endpoint_coverage,
        'alpha': alpha,
        'improvement_metric': spec.get('improvement_metric', 'run_improvement_rate'),
        'improvement_delta_metric': spec.get('improvement_delta_metric', 'best_vina_delta'),
        'safety_interval_confidence': 1 - alpha,
        "reference_group": reference,
        "treatment_group": treatment,
        "primary_metric": primary,
        "efficacy_supported": efficacy_supported,
        "primary_result": primary_result,
        "stable_improvement": stable_improvement,
        "improved_run_rate": improved_run_rate,
        "minimum_improved_run_rate": min_improved,
        "mean_best_vina_delta": vina_delta,
        "safety_noninferior": safety_noninferior,
        "safety_metric": safety_metric,
        "safety_delta_ci95": safety_result.get("delta_ci95"),
        "safety_decision_interval": safety_ci,
        "safety_noninferiority_margin": safety_margin,
        "early_stop": manifest.get("early_stop"),
        "rule": (
            "Approve only when the primary endpoint significantly improves, "
            "the improvement is stable across runs, and the upper 95% CI for "
            "the safety-risk delta stays within the non-inferiority margin."
        ),
    }


def _interpret(groups: dict, comparisons: dict, baseline: str, primary: str) -> dict:
    conclusions = []
    for group, comparison in comparisons.items():
        result = comparison.get(primary) or {}
        n = groups[group]["metrics"][primary]["n"]
        baseline_n = groups[baseline]["metrics"][primary]["n"]
        if n < 3 or baseline_n < 3:
            verdict = "insufficient_repeats"
        elif result.get("statistically_significant") and result.get("favorable"):
            verdict = "supports_improvement"
        elif result.get("statistically_significant") and result.get("favorable") is False:
            verdict = "supports_degradation"
        else:
            verdict = "inconclusive"
        conclusions.append({"group": group, "verdict": verdict, "primary_metric": primary})
    return {
        "conclusions": conclusions,
        "note": (
            "Welch tests and 95% intervals are descriptive at this small sample size. "
            "A non-significant result is inconclusive, not proof of no effect."
        ),
    }


def write_report(benchmark_dir: str | Path) -> tuple[Path, Path]:
    root = Path(benchmark_dir)
    report = build_report(root)
    json_path = root / "benchmark_report.json"
    md_path = root / "benchmark_report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_to_markdown(report), encoding="utf-8")
    return json_path, md_path


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _to_markdown(report: dict) -> str:
    lines = [
        f"# Benchmark report: {report['benchmark_id']}",
        "",
        f"Primary metric: `{report['primary_metric']}`. Baseline: `{report['baseline_group']}`.",
        "",
        "## Group summary",
        "",
        "| Group | Runs | Best composite | Best Vina | Valid rate | Unique molecules | Scaffolds | Tokens | New dockings | Cache hits |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group, data in report["groups"].items():
        m = data["metrics"]
        lines.append(
            f"| {group} | {data['runs']} | {_fmt(m['best_composite_global']['mean'])} "
            f"± {_fmt(m['best_composite_global']['stdev'])} | {_fmt(m['best_vina_global']['mean'])} "
            f"± {_fmt(m['best_vina_global']['stdev'])} | {_fmt(m['valid_rate']['mean'])} | "
            f"{_fmt(m['unique_valid_smiles']['mean'])} | {_fmt(m['unique_scaffolds']['mean'])} | "
            f"{_fmt(m['tokens_used']['mean'])} | {_fmt(m['new_dockings']['mean'])} | "
            f"{_fmt(m['cache_hits']['mean'])} |"
        )
    lines.extend(["", "## Primary comparison", "", "| Group vs baseline | Delta | Welch p | Verdict |", "|---|---:|---:|---|"])
    primary = report["primary_metric"]
    verdicts = {item["group"]: item["verdict"] for item in report["interpretation"]["conclusions"]}
    for group, comparison in report["comparisons"].items():
        result = comparison[primary]
        lines.append(
            f"| {group} | {_fmt(result['delta_vs_baseline'])} | "
            f"{_fmt(result['welch_p_value'])} | {verdicts[group]} |"
        )
    lines.extend(['', '## Safety-gated metrics (separate from all-candidate metrics)', '',
                  '| Group | Best safe composite | Best safe Vina | Safe first-last delta | Safe improved runs |',
                  '|---|---:|---:|---:|---:|'])
    for group, data in report['groups'].items():
        m = data['metrics']
        lines.append('| ' + group + ' | ' + ' | '.join(_fmt(m[k]['mean']) for k in
            ['best_safe_composite_global', 'best_safe_vina_global', 'best_safe_vina_delta', 'safe_run_improvement_rate']) + ' |')
    lines.extend(['', 'Missing safety endpoints are unknown, not improvements. Rate denominators exclude unknown endpoints; confirmation requires full endpoint coverage.', '',
                  '## All-attempt cost and failures', '',
                  '| Group | Eligible | Excluded | Recorded tokens | Seconds |', '|---|---:|---:|---:|---:|'])
    for group, row in report.get('attempt_accounting', {}).items():
        lines.append(f"| {group} | {row['eligible']} | {row['excluded']} | {row['recorded_tokens_all_attempts']} | {_fmt(row['seconds_all_attempts'])} |")
    lines.extend(['', 'Tokens are recorded usage, not a billing reconciliation; failed-request usage may be unavailable. Cache reuse affects runtime and docking cost.'])
    decision = report.get('confirmatory_decision')
    if decision:
        lines.extend(['', '## Confirmatory decision', '', '```json', json.dumps(decision, indent=2), '```'])
    for label, incremental in report.get('incremental_comparisons', {}).items():
        lines.extend(['', '## Incremental comparison: ' + label, '',
                      '| Metric | Treatment minus reference | Welch p | Favorable |', '|---|---:|---:|---|'])
        for metric in ('best_composite_global', 'best_safe_composite_global', 'best_vina_global',
                       'best_safe_vina_global', 'best_vina_delta', 'best_safe_vina_delta', 'cross_round_duplicates', 'tokens_used'):
            result = incremental[metric]
            lines.append(f"| {METRIC_SPECS[metric][1]} | {_fmt(result['delta_vs_reference'])} | {_fmt(result['welch_p_value'])} | {result['favorable']} |")
    lines.extend([
        "",
        "## Agent behavior",
        "",
        "| Group | Improved runs | Vina delta | Judge adoption | Structural adoption | Adoption count drift |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for group, data in report["groups"].items():
        m = data["metrics"]
        lines.append(
            f"| {group} | {_fmt(m['run_improvement_rate']['mean'])} | "
            f"{_fmt(m['best_vina_delta']['mean'])} | "
            f"{_fmt(m['adoption_rate_avg_llm']['mean'])} | "
            f"{_fmt(m['adoption_rate_avg_det']['mean'])} | "
            f"{_fmt(m['adoption_llm_vs_det_drift_avg']['mean'])} |"
        )
    lines.extend([
        "",
        "## Per-run results",
        "",
        "| Group | Repeat | Best composite | Best Vina | Valid rate | Unique | Scaffolds | Cross-round duplicates | Tokens | Seconds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for run in report["runs"]:
        lines.append(
            f"| {run['group']} | {run['repeat']} | {_fmt(run['best_composite_global'])} | "
            f"{_fmt(run['best_vina_global'])} | {_fmt(run['valid_rate'])} | "
            f"{run['unique_valid_smiles']} | {run['unique_scaffolds']} | "
            f"{run['cross_round_duplicates']} | {run['tokens_used']} | "
            f"{_fmt(run['duration_seconds'])} |"
        )
    if report["excluded_runs"]:
        lines.extend([
            "",
            "## Excluded attempts",
            "",
            "| Group | Repeat | Reason |",
            "|---|---:|---|",
        ])
        for run in report["excluded_runs"]:
            reasons = "; ".join(run.get("quality_reasons") or ["quality gate failed"])
            lines.append(f"| {run['group']} | {run['repeat']} | {reasons} |")
    lines.extend(["", "## Interpretation", "", report["interpretation"]["note"], ""])
    return "\n".join(lines)
