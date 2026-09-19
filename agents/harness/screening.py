"""Budgeted, protocol-keyed local evaluation of proposed products."""
from copy import deepcopy
from tools.provenance import digest, evaluation_protocol
from .attribution import breakdown, compare_breakdown, check_predictions


def protocol(state):
    return digest(evaluation_protocol(state.config["target"], state.config["scoring"], False))


def cache_key(state, smiles):
    return digest([protocol(state), smiles])


def proposal_for(state, args):
    p = state.edit_proposals.get(args["proposal_id"])
    if not p or p["revision"] != state.revision:
        raise ValueError("Proposal missing or stale; propose under current constraints")
    return p


def eligible(state, proposal):
    from .planning import preview
    return [(i, o, preview(state, proposal["parent_id"], o["edit"]))
            for i, o in enumerate(proposal["options"]) if o["precheck"]["passed"]]


def screening_cost(state, args):
    proposal = proposal_for(state, args)
    return len({cache_key(state, check["product_smiles"]) for _, _, check in eligible(state, proposal)
                if check["passed"] and cache_key(state, check["product_smiles"]) not in state.option_screenings})


def screening_complete(state, proposal):
    return proposal.get("screening_protocol_id") == protocol(state) and all(
        "screening" in o and o["screening"].get("evaluation_status") == "screening_only"
        for o in proposal["options"] if o["precheck"]["passed"])


def evaluate_options(state, args, directory):
    from agents.evaluator import evaluate_candidates
    from .evidence import judge_effect
    from .molecule_ops import evidence_delta, normalize_constraints
    from rdkit import Chem, DataStructs
    from rdkit.Chem import rdFingerprintGenerator
    proposal = proposal_for(state, args)
    options = eligible(state, proposal)
    missing = {}
    for _, _, check in options:
        if check["passed"]:
            key = cache_key(state, check["product_smiles"])
            if key not in state.option_screenings:
                missing[key] = {"smiles": check["product_smiles"]}
    if missing:
        results = evaluate_candidates(list(missing.values()), state.config["scoring"], state.config["target"],
                                      dock_enabled=False, artifact_dir=str(directory / "option_artifacts"))
        for key, candidate in zip(missing, results):
            candidate["property_attribution"] = breakdown(candidate, state.config["scoring"])
            state.option_screenings[key] = {"evaluation": candidate, "source_proposal": args["proposal_id"],
                                            "charged_evaluations": 1, "step": state.steps_used}
    parent = state.candidates[proposal["parent_id"]]
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    known = [generator.GetFingerprint(Chem.MolFromSmiles(c["smiles"])) for c in state.candidates.values()]
    fingerprints = {i: generator.GetFingerprint(Chem.MolFromSmiles(check["product_smiles"])) for i, _, check in options if check["passed"]}
    rows = []
    for i, option, check in options:
        if not check["passed"]:
            option["screening_failure"] = check["failures"]
            continue
        key = cache_key(state, check["product_smiles"])
        child = deepcopy(state.option_screenings[key]["evaluation"])
        child["screening_cache_key"] = key
        child["attribution_delta"] = compare_breakdown(parent, child, state.config["scoring"])
        child["prediction_checks"] = check_predictions(option.get("predictions", []), parent, child)
        effect = judge_effect(evidence_delta(parent, child), option["expected_metric"], option["expected_direction"], normalize_constraints(state.constraints))
        child["effect_assessment"] = effect
        option["screening"] = child
        novelty = 1 - max((DataStructs.TanimotoSimilarity(fingerprints[i], fp) for fp in known), default=0)
        rows.append({"option_index": i, "property_score": child.get("property_score"),
                     "effect": effect, "novelty_vs_existing": novelty,
                     "pairwise_similarity": {str(j): DataStructs.TanimotoSimilarity(fingerprints[i], fp) for j, fp in fingerprints.items() if j != i},
                     "scoring_success": child.get("evaluation_status") == "screening_only"})
    ranked = sorted(rows, key=lambda r: (r["scoring_success"] and r["effect"]["outcome"] not in {"tradeoff_exceeded", "insufficient_evidence"},
                        r["property_score"] if r["property_score"] is not None else -1, r["novelty_vs_existing"]), reverse=True)
    proposal["screening_protocol_id"] = protocol(state)
    proposal["screening_comparison"] = {"rows": rows, "suggested_order": [r["option_index"] for r in ranked],
        "ranking_rule": "eligible evidence first, property score descending, novelty as tie-breaker; no biological inference",
        "new_evaluations": len(missing), "reused_evaluations": len(rows) - len(missing)}
    return deepcopy(proposal)


def reuse_for_candidate(state, candidate):
    """Only local results of the exact active protocol can satisfy candidate evaluation."""
    if state.dock_enabled:
        return False
    key = cache_key(state, candidate["smiles"])
    entry = state.option_screenings.get(key)
    if not entry or entry["evaluation"].get("protocol_id") != state.protocol_id or entry["evaluation"].get("evaluation_status") != "screening_only":
        return False
    candidate.update(deepcopy({k: v for k, v in entry["evaluation"].items() if k != "smiles"}))
    candidate["screening_reuse"] = {"cache_key": key, "source_proposal": entry["source_proposal"], "new_evaluations": 0}
    return True
