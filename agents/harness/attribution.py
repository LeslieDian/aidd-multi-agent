"""Arithmetic attribution of the existing scoring formula, never biological causality."""
import math

METRICS = ("property_score", "qed", "sa_score", "logp", "mw", "tpsa", "herg_risk",
           "admet_quality_score", "absorption", "bioavailability", "vina", "composite_score")


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def metric(candidate, name):
    if name == "sa_score":
        return (candidate.get("validate") or {}).get(name)
    if name == "vina":
        return (candidate.get("dock") or {}).get("score")
    if name in {"property_score", "composite_score"}:
        return candidate.get(name)
    return (candidate.get("admet") or {}).get("herg_risk_score" if name == "herg_risk" else name)


def breakdown(candidate, config):
    val, admet = candidate.get("validate") or {}, candidate.get("admet") or {}
    sa, quality, score = val.get("sa_score"), admet.get("admet_quality_score", admet.get("summary_score")), candidate.get("property_score")
    base = {"formula_version": "property_055_015_030", "scope": "scoring_formula_only", "observed_score": score}
    if not all(finite(x) for x in (sa, quality, score)) or not val.get("valid") or not admet.get("valid"):
        return {**base, "status": "insufficient_evidence", "components": []}
    sa_normalized = max(0.0, min(1.0, 1.0 - (sa - 1.0) / 9.0))
    penalty = 0.3 if sa > config.get("sa_score", {}).get("reject_above", 6.0) else 1.0
    rows = [
        {"metric": "admet_quality_score", "raw": quality, "normalized": quality, "weight": .55, "contribution": .55 * quality},
        {"metric": "lipinski_pass", "raw": val.get("lipinski_pass"), "normalized": int(bool(val.get("lipinski_pass"))), "weight": .15, "contribution": .15 * bool(val.get("lipinski_pass"))},
        {"metric": "sa_score", "raw": sa, "normalized": sa_normalized * penalty, "weight": .30, "penalty_multiplier": penalty, "contribution": .30 * sa_normalized * penalty},
    ]
    total = sum(r["contribution"] for r in rows)
    children = []
    for key, weight in (("absorption", .375), ("bioavailability", .375), ("qed", .25)):
        if finite(admet.get(key)):
            children.append({"metric": key, "raw": admet[key], "weight_in_quality": weight,
                             "effective_weight": .55 * weight, "contribution": .55 * weight * admet[key]})
    if len(children) == 3:
        children.append({"metric": "stored_quality_rounding_residual", "contribution": rows[0]["contribution"] - sum(c["contribution"] for c in children)})
    else:
        children = []
    return {**base, "status": "verified" if abs(total - score) <= 1e-10 else "formula_mismatch",
            "components": rows, "quality_subcomponents": children, "contribution_sum": total,
            "residual": score - total}


def compare_breakdown(parent, child, config):
    before, after = breakdown(parent, config), breakdown(child, config)
    if (before["status"] != "verified" or after["status"] != "verified" or
            not parent.get("protocol_id") or parent.get("protocol_id") != child.get("protocol_id")):
        return {"status": "insufficient_evidence", "reason": "missing data, formula mismatch or different evaluation protocols"}
    rows = [{"metric": p["metric"], "parent_raw": p["raw"], "child_raw": c["raw"],
             "parent_contribution": p["contribution"], "child_contribution": c["contribution"],
             "contribution_delta": c["contribution"] - p["contribution"]} for p, c in zip(before["components"], after["components"])]
    observed = child["property_score"] - parent["property_score"]
    contribution_sum = sum(r["contribution_delta"] for r in rows)
    summary = "; ".join(f"{r['metric']}: {r['contribution_delta']:+.8f}" for r in rows)
    return {"status": "verified", "scope": "scoring_formula_only", "components": rows,
            "observed_delta": observed, "contribution_delta_sum": contribution_sum,
            "residual": observed - contribution_sum, "balanced": abs(observed - contribution_sum) <= 1e-10,
            "summary": summary, "parent": before, "child": after}


def check_predictions(predictions, parent, child):
    comparable = bool(parent.get("protocol_id") and parent.get("protocol_id") == child.get("protocol_id")
        and all(c.get("evaluation_status") in {"complete", "screening_only"} for c in (parent, child)))
    rows = []
    for prediction in predictions:
        before, after = metric(parent, prediction["metric"]), metric(child, prediction["metric"])
        change = after - before if comparable and finite(before) and finite(after) else None
        directed = change * (1 if prediction["direction"] == "increase" else -1) if change is not None else None
        minimum = prediction["min_change"]
        status = "insufficient_evidence" if directed is None else "supported" if directed + 1e-12 >= minimum else "refuted" if directed <= -minimum + 1e-12 else "inconclusive"
        rows.append({**prediction, "parent": before, "child": after, "delta": change, "status": status})
    return rows
