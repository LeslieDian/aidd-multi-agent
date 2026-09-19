"""Versioned structure checks, effect decisions and factual reports (no model text)."""
from copy import deepcopy
import hashlib
import json

from .molecule_ops import normalize_constraints, verify_refinement, evidence_delta, candidate_goal_assessment


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def judge_effect(delta, metric, direction, rules):
    """Thresholds are engineering tolerances, not statistical significance tests."""
    observed = delta[metric]["delta"]
    minimum = rules["min_effects"][metric]
    checks = []
    missing = []
    violations = []
    for name, limit in rules["max_regressions"].items():
        change = delta[name]["delta"]
        regression = None if change is None else (-change if name in {"property_score", "composite_score"} else change)
        passed = None if regression is None else regression <= limit + 1e-12
        checks.append({"metric": name, "delta": change, "regression": regression,
                       "maximum": limit, "passed": passed})
        if passed is None:
            missing.append(name)
        elif not passed:
            violations.append(name)
    directed = None if observed is None else observed * (1 if direction == "increase" else -1)
    if not delta["same_protocol"] or directed is None or missing:
        outcome = "insufficient_evidence"
    elif violations:
        outcome = "tradeoff_exceeded"
    elif abs(directed) + 1e-12 < minimum:
        outcome = "inconclusive"
    elif directed < 0:
        outcome = "not_supported"
    else:
        outcome = "supported"
    return {"outcome": outcome, "observed_delta": observed, "directed_delta": directed,
            "minimum_effect": minimum, "tradeoff_checks": checks, "violations": violations,
            "missing_metrics": missing, "comparison_protocol_consistent": delta["same_protocol"],
            "threshold_basis": "engineering_tolerance_not_statistical_or_biological_validation"}


def revalidate(state):
    """Keep original checks immutable; append when structures or constraints change."""
    constraints = normalize_constraints(state.constraints, dock_enabled=state.dock_enabled)
    for cid, child in state.candidates.items():
        parent_id = child.get("parent_id")
        if not parent_id:
            continue
        parent = state.candidates.get(parent_id)
        key = signature([constraints, child.get("smiles"), (parent or {}).get("smiles")])
        if child.get("current_validation", {}).get("signature") != key:
            check = (verify_refinement(parent["smiles"], child["smiles"], constraints) if parent else
                     {"passed": False, "failures": ["missing_parent"], "checks": []})
            edit = child.get("edit_record") or {}
            if edit and constraints["allowed_parent_atom_indices"]:
                touched = {edit["parent_atom_index"], *edit.get("removed_parent_atom_indices", [])}
                if edit.get("operation") == "change_bond_order":
                    touched.add(edit["neighbor_atom_index"])
                passed = touched.issubset(set(constraints["allowed_parent_atom_indices"]))
                check["checks"].append({"name": "recorded_edit_site", "passed": passed,
                                        "observed": sorted(touched)})
                if not passed:
                    check["failures"].append("recorded_edit_site")
                    check["passed"] = False
            check.update(signature=key, revision=state.revision, constraints=deepcopy(constraints))
            child.setdefault("validation_history", []).append(deepcopy(check))
            child["current_validation"] = check
        hypothesis = state.hypotheses.get(child.get("hypothesis_id"))
        if hypothesis:
            delta = evidence_delta(parent, child) if parent else None
            if not child["current_validation"]["passed"]:
                result = {"outcome": "edit_rejected", "observed_delta": None,
                          "comparison_protocol_consistent": None}
            elif delta and all(c.get("evaluation_status") in {"complete", "screening_only"} for c in (parent, child)):
                result = judge_effect(delta, hypothesis["expected_metric"], hypothesis["expected_direction"], constraints)
            else:
                result = {"outcome": "insufficient_evidence", "observed_delta": None}
            result.update(revision=state.revision)
            child["current_improvement"] = result


def assess_hypothesis(state, child):
    from .attribution import compare_breakdown, check_predictions
    revalidate(state)
    hid = child.get("hypothesis_id")
    if hid not in state.hypotheses:
        return None
    hypothesis = state.hypotheses[hid]
    result = deepcopy(child["current_improvement"])
    result["assessed_at_step"] = state.steps_used
    result["rules"] = deepcopy(normalize_constraints(state.constraints))
    parent = state.candidates[child["parent_id"]]
    result["attribution_delta"] = compare_breakdown(parent, child, state.config["scoring"])
    result["prediction_checks"] = check_predictions(hypothesis.get("predictions", []), parent, child)
    hypothesis.setdefault("assessment_history", []).append(result)
    hypothesis.update(result, status="assessed")
    hypothesis["next_action"] = hypothesis["next_if_supported"] if result["outcome"] == "supported" else hypothesis["next_if_not_supported"]
    return deepcopy(hypothesis)


def build_report(state, requested_ids, explanation):
    from .planning import experience
    from .attribution import breakdown, compare_breakdown, check_predictions
    revalidate(state)
    assessments = {cid: candidate_goal_assessment(c, state.constraints) for cid, c in state.candidates.items()}
    qualified, references, rejected = [], [], []
    for cid, c in state.candidates.items():
        row = {"candidate_id": cid, "smiles": c["smiles"], "parent_id": c.get("parent_id"),
               "evaluation_status": c.get("evaluation_status"), "protocol_id": c.get("protocol_id"),
               "property_score": c.get("property_score"), "composite_score": c.get("composite_score"),
               "vina": (c.get("dock") or {}).get("score"), "assessment": assessments[cid],
               "current_validation": c.get("current_validation"), "improvement": c.get("current_improvement"),
               "requested_by_model": cid in requested_ids}
        row["property_attribution"] = breakdown(c, state.config["scoring"])
        if c.get("parent_id") in state.candidates:
            parent = state.candidates[c["parent_id"]]
            row["attribution_delta"] = compare_breakdown(parent, c, state.config["scoring"])
            row["prediction_checks"] = check_predictions(state.hypotheses.get(c.get("hypothesis_id"), {}).get("predictions", []), parent, c)
        if c.get("candidate_role") == "seed":
            references.append(row)
        elif assessments[cid]["passed"]:
            qualified.append(row)
        else:
            rejected.append(row)
    outcome = "goal_met" if qualified else "goal_not_met"
    stop_reason = "qualified_candidates_found" if qualified else "no_candidate_meets_current_constraints"
    if not qualified and sum(c.get("candidate_role") == "deterministic_edit" for c in state.candidates.values()) >= normalize_constraints(state.constraints)["max_edits"]:
        stop_reason = "edit_budget_exhausted"
    summary = (f"当前约束版本 V{state.revision}：合格候选 {len(qualified)} 个，参考母体 {len(references)} 个，"
               f"未达标候选 {len(rejected)} 个。" + ("已找到满足当前完成条件的候选。" if qualified else "尚未找到满足当前完成条件的候选。"))
    return {"report_version": 2, "revision": state.revision, "outcome": outcome,
            "candidate_ids": [r["candidate_id"] for r in qualified], "summary": summary,
            "qualified_candidates": qualified, "reference_parents": references, "rejected_candidates": rejected,
            "goal_assessments": assessments, "evidence": qualified,
            "stop_reason": stop_reason,
            "model_explanation": {"text": explanation, "verified": False},
            "constraints": deepcopy(state.constraints), "strategy_history": deepcopy(state.strategies),
            "edit_proposals": deepcopy(state.edit_proposals), "edit_selections": deepcopy(state.edit_selections),
            "task_experience": experience(state),
            "evaluation_accounting": {"budget_used": state.evaluations_used, "budget_limit": state.max_evaluations,
                                      "screening_records": len(state.option_screenings)},
            "limitations": "代理指标与工程阈值不证明实验活性、安全性或统计显著性。"}
