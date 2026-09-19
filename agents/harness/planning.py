"""Auditable alternatives, dry-run validation and task-local empirical evidence."""
from copy import deepcopy
import json

from .editor import apply_edit, EDIT_OPERATIONS
from .evidence import revalidate


def experience(state):
    from .attribution import compare_breakdown, check_predictions
    rows = []
    for hid, h in state.hypotheses.items():
        child = state.candidates.get(h.get("child_id"), {})
        if h.get("status") == "assessed":
            rows.append({"evidence_id": "h:" + hid, "parent_id": h["parent_id"],
                         "child_id": h.get("child_id"), "edit": h.get("edit_record"),
                         "assessment": child.get("current_improvement", {}),
                         "historical_outcome": h.get("outcome"),
                         "attribution_delta": compare_breakdown(state.candidates[h["parent_id"]], child, state.config["scoring"]),
                         "prediction_checks": check_predictions(h.get("predictions", []), state.candidates[h["parent_id"]], child),
                         "scope": "one observed parent/edit; not a general chemical rule"})
    for pid, proposal in state.edit_proposals.items():
        for i, option in enumerate(proposal["options"]):
            if option.get("screening"):
                screen = option["screening"]
                rows.append({"evidence_id": f"screen:{pid}:{i}", "parent_id": proposal["parent_id"],
                             "edit": option["edit"], "assessment": screen.get("effect_assessment"),
                             "attribution_delta": screen.get("attribution_delta"),
                             "prediction_checks": screen.get("prediction_checks"), "revision": proposal["revision"],
                             "scope": "local screening result, not a committed edit or biological observation"})
            if not option["precheck"]["passed"]:
                rows.append({"evidence_id": f"p:{pid}:{i}", "parent_id": proposal["parent_id"],
                             "edit": option["edit"], "precheck": option["precheck"],
                             "revision": proposal["revision"], "scope": "historical feasibility check"})
    return rows


def preview(state, parent_id, edit):
    """No score/model calls and no mutation of the task or candidate pool."""
    from .tools import default_registry
    try:
        if not isinstance(edit, dict) or set(edit) != {"operation", "arguments"}:
            raise ValueError("edit needs operation and arguments only")
        operation, args = edit["operation"], edit["arguments"]
        if operation not in EDIT_OPERATIONS or not isinstance(args, dict):
            raise ValueError("Unknown edit operation or malformed arguments")
        default_registry().validate({"tool": operation, "reason": "precheck",
            "arguments": {**args, "parent_id": parent_id, "hypothesis_id": "preview"}})
        if "parent_id" in args or "hypothesis_id" in args:
            raise ValueError("Do not include parent_id or hypothesis_id inside edit arguments")
        parent = state.candidates[parent_id]
        smiles, record = apply_edit(parent["smiles"], operation, **args)
        if any(c["smiles"] == smiles for c in state.candidates.values()):
            raise ValueError("duplicate_product: product already exists in task")
        scratch = deepcopy(state)
        scratch.candidates["__preview__"] = {"smiles": smiles, "parent_id": parent_id, "edit_record": record}
        revalidate(scratch)
        check = scratch.candidates["__preview__"]["current_validation"]
        if parent.get("parent_id") and not scratch.candidates[parent_id]["current_validation"]["passed"]:
            raise ValueError("parent violates current constraints")
        return {"passed": check["passed"], "product_smiles": smiles, "verification": check,
                "failures": check["failures"], "revision": state.revision,
                "note": "Feasibility only; no predicted benefit has been validated"}
    except (ValueError, KeyError, TypeError) as exc:
        return {"passed": False, "failures": [str(exc)], "revision": state.revision}


def propose(state, args, directory):
    options = args["options"]
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise ValueError("Provide 2-4 alternatives")
    rows = []
    seen = set()
    required = {"edit", "rationale", "expected_benefit", "allowed_cost", "expected_metric", "expected_direction", "predictions"}
    for option in options:
        if not isinstance(option, dict) or set(option) != required:
            raise ValueError("Each option needs edit, rationale, expected_benefit, allowed_cost, expected_metric, expected_direction")
        if any(not isinstance(option[k], str) or not option[k].strip() for k in required - {"edit", "predictions"}):
            raise ValueError("All option explanations and metric fields must be nonempty strings")
        if option["expected_metric"] not in {"property_score", "composite_score", "herg_risk", "vina"}:
            raise ValueError("Unknown expected_metric")
        direction = "increase" if option["expected_metric"] in {"property_score", "composite_score"} else "decrease"
        if option["expected_direction"] != direction:
            raise ValueError("Use the favorable metric direction")
        fingerprint = json.dumps(option["edit"], sort_keys=True)
        if fingerprint in seen:
            raise ValueError("Alternatives must be different edits")
        seen.add(fingerprint)
        check = preview(state, args["parent_id"], option["edit"])
        if check.get("product_smiles") and any(r["precheck"].get("product_smiles") == check["product_smiles"] for r in rows):
            check.update(passed=False, failures=["duplicate_alternative_product"])
        known_key = None
        if check.get("product_smiles") and state.option_screenings:
            from .screening import cache_key
            key = cache_key(state, check["product_smiles"])
            if key in state.option_screenings:
                known_key = key
        rows.append({**option, "precheck": check, "prediction_provenance": {
            "recorded_at_step": state.steps_used, "prior_screening_key": known_key,
            "new_to_task_screening": known_key is None}})
    pid = f"p{len(state.edit_proposals) + 1}"
    state.edit_proposals[pid] = {"proposal_id": pid, "parent_id": args["parent_id"],
                               "revision": state.revision, "options": rows}
    return deepcopy(state.edit_proposals[pid])


def select(state, args, directory):
    from .tools import _record_hypothesis
    proposal = state.edit_proposals.get(args["proposal_id"])
    if not proposal or proposal["revision"] != state.revision:
        raise ValueError("Proposal missing or stale; propose again under current constraints")
    if any(s["status"] == "selected" and s["revision"] == state.revision for s in state.edit_selections.values()):
        raise ValueError("An edit is already selected; execute it before selecting another")
    if state.candidates[proposal["parent_id"]].get("evaluation_status") not in {"complete", "screening_only"}:
        raise ValueError("Evaluate the parent before selecting an optimization hypothesis")
    index = args["option_index"]
    if index >= len(proposal["options"]):
        raise ValueError("Unknown option index")
    option = proposal["options"][index]
    check = preview(state, proposal["parent_id"], option["edit"])
    if not option["precheck"]["passed"] or not check["passed"]:
        raise ValueError("Option failed feasibility checks: " + json.dumps(check["failures"]))
    if state.constraints.get("require_option_screening"):
        from .screening import screening_complete
        if not screening_complete(state, proposal):
            raise ValueError("Call evaluate_options before selecting; all feasible alternatives need current screening evidence")
    if option.get("screening", {}).get("effect_assessment", {}).get("outcome") in {"tradeoff_exceeded", "insufficient_evidence"}:
        raise ValueError("Screening shows numerical tradeoff limits exceeded or insufficient evidence; choose another option or finish")
    ids = args["evidence_ids"]
    known = {r["evidence_id"] for r in experience(state)}
    if not isinstance(ids, list) or any(not isinstance(i, str) or i not in known for i in ids):
        raise ValueError("Cite only existing task experience IDs")
    assessed = [h for h in state.hypotheses.values() if h.get("status") == "assessed"]
    if assessed and "h:" + assessed[-1]["hypothesis_id"] not in ids:
        raise ValueError("Selection must cite the most recent assessed hypothesis")
    sid = f"s{len(state.edit_selections) + 1}"
    hid = "planned_" + sid
    _record_hypothesis(state, {"hypothesis_id": hid, "parent_id": proposal["parent_id"],
        "rationale": args["rationale"], "expected_metric": option["expected_metric"],
        "expected_direction": option["expected_direction"], "allowed_tradeoff": option["allowed_cost"],
        "next_if_supported": "consider continuing only with supported evidence",
        "next_if_not_supported": "use task experience to change the edit or stop"}, directory)
    state.hypotheses[hid]["selection_id"] = sid
    state.hypotheses[hid]["predictions"] = deepcopy(option.get("predictions", []))
    state.hypotheses[hid]["prediction_provenance"] = deepcopy(option.get("prediction_provenance"))
    selection = {**args, "selection_id": sid, "hypothesis_id": hid, "parent_id": proposal["parent_id"],
                 "edit": deepcopy(option["edit"]), "evidence_ids": ids,
                 "evidence_snapshot": [deepcopy(r) for r in experience(state) if r["evidence_id"] in ids],
                 "rationale_verified": False,
                 "screening_comparison": deepcopy(proposal.get("screening_comparison")),
                 "revision": state.revision, "status": "selected"}
    state.edit_selections[sid] = selection
    return deepcopy(selection)


def execute_selected(state, args, directory):
    from .tools import default_registry
    selection = state.edit_selections.get(args["selection_id"])
    if not selection or selection["status"] != "selected" or selection["revision"] != state.revision:
        raise ValueError("Selection missing, stale or already executed")
    edit = selection["edit"]
    result = default_registry().execute(state, {"tool": edit["operation"],
        "arguments": {**edit["arguments"], "parent_id": selection["parent_id"], "hypothesis_id": selection["hypothesis_id"]},
        "reason": selection["rationale"]}, directory)
    selection.update(status="executed", child_id=result["added_ids"][0])
    return result


def mock_decision(state):
    """Scripted UI demonstration only; never presented as autonomous reasoning."""
    from .molecule_ops import candidate_goal_assessment
    from rdkit import Chem
    def action(tool, **arguments):
        return {"tool": tool, "arguments": arguments, "reason": "Offline demo: " + tool}
    ids = list(state.candidates)
    if not ids:
        return action("pause", message="Import a parent for the scripted planned-edit demonstration")
    pending = [cid for cid in ids if "evaluation_status" not in state.candidates[cid]
               and (not state.candidates[cid].get("parent_id") or state.candidates[cid].get("current_validation", {}).get("passed"))]
    if pending:
        return action("evaluate", candidate_ids=pending)
    unfinished = [h for h in state.hypotheses.values() if h.get("status") == "edit_executed"]
    if unfinished:
        return action("compare_parent_child", candidate_ids=[unfinished[-1]["child_id"]])
    evaluated = [cid for cid in ids if state.candidates[cid].get("evaluation_status") in {"complete", "screening_only"}]
    done = any(c.get("parent_id") and candidate_goal_assessment(c, state.constraints)["passed"] for c in state.candidates.values())
    used = sum(c.get("candidate_role") == "deterministic_edit" for c in state.candidates.values())
    if done or used >= state.constraints["max_edits"] or not state.constraints["allow_refine"]:
        return action("finish", candidate_ids=evaluated[:20], summary="Scripted demonstration ended; inspect factual report")
    selected = [s for s in state.edit_selections.values() if s["status"] == "selected" and s["revision"] == state.revision]
    if selected:
        return action("execute_selected_edit", selection_id=selected[-1]["selection_id"])
    assessed = [h for h in state.hypotheses.values() if h.get("status") == "assessed"]
    last = assessed[-1] if assessed else None
    strategies = [s for s in state.strategies if last and s["hypothesis_id"] == last["hypothesis_id"] and s["revision"] == state.revision and s["status"] == "selected"]
    if last and not strategies:
        return action("choose_strategy", hypothesis_id=last["hypothesis_id"], choice="rollback", parent_id=last["parent_id"], rationale="Scripted demo returns to the original parent")
    consumed = {s["proposal_id"] for s in state.edit_selections.values()}
    proposals = [p for p in state.edit_proposals.values() if p["proposal_id"] not in consumed and p["revision"] == state.revision]
    if proposals:
        p = proposals[-1]
        if state.constraints.get("require_option_screening"):
            from .screening import screening_complete
            if not screening_complete(state, p):
                return action("evaluate_options", proposal_id=p["proposal_id"])
        feasible = [i for i, o in enumerate(p["options"]) if o["precheck"]["passed"]
                    and o.get("screening", {}).get("effect_assessment", {}).get("outcome") != "tradeoff_exceeded"]
        if not feasible:
            return action("finish", candidate_ids=evaluated[:20], summary="No feasible scripted alternatives remain")
        return action("select_edit", proposal_id=p["proposal_id"], option_index=feasible[0],
                      rationale="Scripted choice: first feasible alternative; no model reasoning",
                      evidence_ids=["h:" + last["hypothesis_id"]] if last else [])
    parent_id = strategies[-1]["parent_id"] if strategies else ids[0]
    mol = Chem.MolFromSmiles(state.candidates[parent_id]["smiles"])
    allowed = state.constraints["allowed_parent_atom_indices"]
    sites = [a.GetIdx() for a in mol.GetAtoms() if a.GetTotalNumHs() and (not allowed or a.GetIdx() in allowed)]
    if not sites:
        return action("finish", candidate_ids=evaluated[:20], summary="No attachment sites for scripted demonstration")
    options = []
    for fragment in ("C", "N", "O", "F"):
        option = {"edit": {"operation": "attach_fragment", "arguments": {"atom_index": sites[0],
            "fragment_smiles": fragment, "fragment_atom_index": 0}}, "rationale": "Scripted alternative",
            "expected_benefit": "Unverified property change", "allowed_cost": "Numerical constraints apply",
            "expected_metric": "property_score", "expected_direction": "increase",
            "predictions": [{"metric": "property_score", "direction": "increase", "min_change": 0.01}]}
        if preview(state, parent_id, option["edit"])["passed"]:
            options.append(option)
        if len(options) == 2:
            break
    if len(options) < 2:
        return action("finish", candidate_ids=evaluated[:20], summary="Fewer than two feasible scripted alternatives remain")
    return action("propose_edits", parent_id=parent_id, options=options)
