"""Allowlisted tool contracts and adapters to the existing AIDD components."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable
    retry_safe: bool = False


class ToolRegistry:
    def __init__(self, enforce_state_machine=False):
        self.tools = {}
        self.enforce_state_machine = enforce_state_machine

    def register(self, tool: Tool):
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self.tools[tool.name] = tool

    def describe(self, state=None):
        allowed = set(available_actions(state)["tools"]) if state is not None else None
        return [{"name": t.name, "description": t.description,
                 "parameters": {"type": "object", "properties": t.parameters,
                                "required": list(t.parameters), "additionalProperties": False}}
                for t in self.tools.values() if allowed is None or t.name in allowed]

    def validate(self, action):
        if not isinstance(action, dict) or set(action) != {"tool", "arguments", "reason"}:
            raise ValueError("Action must contain tool, arguments, reason only")
        if not isinstance(action["reason"], str) or not action["reason"].strip():
            raise ValueError("Action needs a short reason")
        name = action["tool"]
        if not isinstance(name, str) or name not in self.tools:
            raise ValueError("Unknown tool")
        args = action["arguments"]
        schema = self.tools[name].parameters
        if not isinstance(args, dict) or set(args) != set(schema):
            raise ValueError(f"Expected arguments: {list(schema)}")
        from .schema import validate_value
        for key, spec in schema.items():
            validate_value(args[key], spec, key)
        return self.tools[name]

    def preflight(self, state, action, *, enforce_state_machine=False):
        tool = self.validate(action)
        availability = available_actions(state)
        requested = action["tool"]
        enforce = (enforce_state_machine and self.enforce_state_machine) or requested == "choose_strategy"
        if enforce and requested not in availability["tools"]:
            raise ValueError(
                f"Illegal action at stage={availability['stage']}: {requested!r}; "
                f"allowed_tools={availability['tools']}; "
                f"allowed_ids={availability['references']}"
            )
        args = action["arguments"]
        from .evidence import revalidate
        revalidate(state)
        if "candidate_ids" in args:
            selected = _select(state, args["candidate_ids"])
            if tool.name in {"compare", "compare_parent_child", "finish"} and any("evaluation_status" not in c for c in selected):
                raise ValueError("Evaluate selected candidates before comparison or finish")
            if tool.name in {"evaluate", "retry_evaluation"} and any(
                    c.get("parent_id") and not c.get("current_validation", {}).get("passed") for c in selected):
                raise ValueError("Unverified refinements cannot be evaluated; inspect verification evidence and refine again")
            if tool.name == "finish" and any(c.get("evaluation_status") not in {"complete", "screening_only"} for c in selected):
                raise ValueError("Final candidates need successful evaluation evidence; retry failed evaluations or pause")
            if tool.name == "retry_evaluation" and any(c.get("evaluation_status") != "evaluation_error" for c in selected):
                raise ValueError("retry_evaluation only accepts evaluation_error candidates")
        if "parent_id" in args:
            parent = _select(state, [args["parent_id"]])[0]
            if tool.name not in {"choose_strategy"} and parent.get("parent_id") and not parent.get("current_validation", {}).get("passed"):
                raise ValueError("Parent violates current structural constraints; rollback to a valid parent")
            if tool.name == "record_hypothesis" and parent.get("evaluation_status") not in {"complete", "screening_only"}:
                raise ValueError("Evaluate the parent before recording an optimization hypothesis")
        edit_tools = {"attach_fragment", "replace_substituent", "remove_terminal_group",
                      "replace_bioisostere", "change_bond_order"}
        if tool.name in edit_tools:
            constraints = _constraints(state)
            if sum(c.get("candidate_role") == "deterministic_edit" for c in state.candidates.values()) >= constraints["max_edits"]:
                raise ValueError("Edit budget exhausted; compare existing evidence and finish")
            hypothesis = state.hypotheses.get(args["hypothesis_id"])
            if not hypothesis:
                raise ValueError("Record the hypothesis before executing a molecular edit")
            if hypothesis.get("parent_id") != args["parent_id"]:
                raise ValueError("Hypothesis parent does not match the edit parent")
            if hypothesis.get("status") != "proposed":
                raise ValueError("Each hypothesis can authorize only one molecular edit")
            if hypothesis.get("revision") != state.revision:
                raise ValueError("Hypothesis is stale after user intervention; record a new hypothesis")
            if constraints["require_planned_edits"]:
                from .planning import preview
                selection = state.edit_selections.get(hypothesis.get("selection_id"))
                edit = {"operation": tool.name, "arguments": {k: v for k, v in args.items() if k not in {"parent_id", "hypothesis_id"}}}
                if not selection or selection["edit"] != edit or selection["status"] != "selected":
                    raise ValueError("Propose alternatives and select_edit before execution; execute exactly the selected edit")
                check = preview(state, args["parent_id"], edit)
                if not check["passed"]:
                    raise ValueError("Edit precheck failed: " + str(check["failures"]))
            fingerprint = _edit_fingerprint(state, action)
            for event in state.events:
                prior = event.get("action")
                if (isinstance(prior, dict) and prior.get("tool") in edit_tools
                        and _edit_fingerprint(state, prior) == fingerprint):
                    raise ValueError("Repeated molecular edit is blocked even with a new hypothesis ID; choose a different edit")
        constraints = _constraints(state)
        if tool.name == "record_hypothesis" and constraints["require_planned_edits"]:
            raise ValueError("Use propose_edits and select_edit; select_edit records the hypothesis automatically")
        if tool.name == "evaluate_options":
            from .screening import proposal_for
            proposal_for(state, args)
        if tool.name == "refine" and constraints["require_planned_edits"]:
            raise ValueError("Planned optimization requires deterministic edits")
        if tool.name == "generate" and not constraints["allow_generation"]:
            raise ValueError("Generation is disabled by the current task constraints")
        if tool.name in edit_tools and not constraints["allow_refine"]:
            raise ValueError("Refinement is disabled by the current task constraints")
        if tool.name == "refine" and (not constraints["allow_refine"] or not constraints["allow_freeform_refine"]):
            raise ValueError("Free-form LLM refinement is disabled; use a deterministic edit tool")
        return tool

    def cost(self, state, action):
        name, args = action["tool"], action["arguments"]
        if name == "evaluate_options":
            from .screening import screening_cost
            return {"model_calls": 0, "evaluations": screening_cost(state, args)}
        selected = _select(state, args["candidate_ids"]) if name in {"evaluate", "retry_evaluation"} else []
        return {
            "model_calls": int(name in {"generate", "refine"}),
            "evaluations": sum("evaluation_status" not in candidate
                               for candidate in selected) if name == "evaluate" else
                           len(selected) if name == "retry_evaluation" else 0,
        }

    def execute(self, state, action, directory, *, enforce_state_machine=False):
        tool = self.preflight(state, action, enforce_state_machine=enforce_state_machine)
        return tool.handler(state, action["arguments"], directory)


def _select(state, ids):
    if any(cid not in state.candidates for cid in ids):
        raise ValueError("Unknown candidate ID; inspect task state before choosing IDs")
    return [state.candidates[cid] for cid in ids]


def available_actions(state):
    """Return the deterministic action stage and valid evidence references."""
    if state is None:
        return {"stage": "unknown", "tools": [], "references": {}}
    if state.status in {"completed", "cancelled"}:
        return {"stage": "terminal", "tools": [], "references": {}}
    ids = list(state.candidates)
    if not ids:
        tools = ["pause"]
        if state.constraints.get("allow_generation", True):
            tools.insert(0, "generate")
        if not state.constraints.get("allow_generation", True):
            tools = ["pause"]
        return {"stage": "no_candidates", "tools": tools, "references": {"candidate_ids": []}}
    evaluated = [cid for cid in ids if state.candidates[cid].get("evaluation_status") in {"complete", "screening_only"}]
    if not state.constraints.get("require_planned_edits"):
        return {"stage": "general", "tools": [name for name in (
            "evaluate", "generate", "refine", "import_candidates", "record_hypothesis",
            "choose_strategy", "compare", "compare_parent_child", "history", "pause", "finish",
            "retry_evaluation", "attach_fragment", "replace_substituent",
            "remove_terminal_group", "replace_bioisostere", "change_bond_order")],
                "references": {"candidate_ids": ids}}
    pending = [cid for cid in ids if "evaluation_status" not in state.candidates[cid]]
    failed = [cid for cid in ids if state.candidates[cid].get("evaluation_status") == "evaluation_error"]
    if pending:
        return {"stage": "candidate_evaluation", "tools": ["evaluate", "pause"],
                "references": {"candidate_ids": pending}}
    if failed:
        return {"stage": "evaluation_recovery", "tools": ["retry_evaluation", "pause"],
                "references": {"candidate_ids": failed}}
    selected = [s for s in state.edit_selections.values()
                if s.get("status") == "selected" and s.get("revision") == state.revision]
    if selected:
        return {"stage": "execute_selected_edit", "tools": ["execute_selected_edit", "pause"],
                "references": {"selection_ids": [s["selection_id"] for s in selected]}}
    executed = [h for h in state.hypotheses.values()
                if h.get("status") == "edit_executed" and h.get("child_id")]
    if executed:
        return {"stage": "parent_child_comparison", "tools": ["compare_parent_child", "pause"],
                "references": {"candidate_ids": [h["child_id"] for h in executed]}}
    decided_hypotheses = {s.get("hypothesis_id") for s in state.strategies
                          if s.get("revision") == state.revision and s.get("status") == "selected"}
    assessed = [h for h in state.hypotheses.values()
                if h.get("status") == "assessed" and h.get("child_id")
                and h.get("hypothesis_id") not in decided_hypotheses]
    if assessed:
        last = assessed[-1]
        return {"stage": "strategy_decision", "tools": ["choose_strategy", "finish", "pause"],
                "references": {"hypothesis_ids": [last["hypothesis_id"]],
                               "parent_ids": [last["parent_id"], last["child_id"]]}}
    used_edits = sum(c.get("candidate_role") == "deterministic_edit" for c in state.candidates.values())
    if used_edits >= _constraints(state)["max_edits"]:
        return {"stage": "edit_budget_exhausted", "tools": ["finish", "pause"],
                "references": {"candidate_ids": evaluated}}
    consumed = {s.get("proposal_id") for s in state.edit_selections.values()}
    proposals = [p for p in state.edit_proposals.values()
                 if p.get("revision") == state.revision and p.get("proposal_id") not in consumed]
    if proposals:
        proposal = proposals[-1]
        from .screening import screening_complete
        if state.constraints.get("require_option_screening") and not screening_complete(state, proposal):
            return {"stage": "option_screening", "tools": ["evaluate_options", "pause"],
                    "references": {"proposal_ids": [proposal["proposal_id"]]}}
        rejected_outcomes = {"tradeoff_exceeded", "insufficient_evidence"}
        feasible = [i for i, option in enumerate(proposal["options"])
                    if option.get("precheck", {}).get("passed") and (
                        not state.constraints.get("require_option_screening") or
                        option.get("screening", {}).get("effect_assessment", {}).get("outcome")
                        not in rejected_outcomes)]
        if feasible:
            supported = [i for i in feasible if proposal["options"][i].get("screening", {})
                         .get("effect_assessment", {}).get("outcome") == "supported"]
            tools = ["select_edit", "pause"] if supported else ["select_edit", "propose_edits", "finish", "pause"]
            return {"stage": "select_edit", "tools": tools,
                    "references": {"proposal_ids": [proposal["proposal_id"]], "option_indices": feasible}}
        return {"stage": "screening_no_qualifying_option", "tools": ["propose_edits", "finish", "pause"],
                "references": {"candidate_ids": evaluated, "proposal_ids": [proposal["proposal_id"]]}}
    return {"stage": "propose_edits", "tools": ["propose_edits", "pause"],
            "references": {"candidate_ids": evaluated}}


def _constraints(state):
    from .molecule_ops import normalize_constraints
    return normalize_constraints(state.constraints, dock_enabled=state.dock_enabled)


def _import_candidates(state, args, directory):
    from .molecule_ops import add_seed_candidates
    added = add_seed_candidates(state, args["smiles"], source="tool_import")
    return {"added_ids": added, "source": "user_supplied_smiles"}


def _record_hypothesis(state, args, directory):
    hypothesis_id = args["hypothesis_id"]
    if hypothesis_id in state.hypotheses:
        raise ValueError("Hypothesis ID already exists")
    if args["expected_metric"] not in {"property_score", "composite_score", "vina", "herg_risk"}:
        raise ValueError("expected_metric must be property_score, composite_score, vina or herg_risk")
    if args["expected_direction"] not in {"increase", "decrease"}:
        raise ValueError("expected_direction must be increase or decrease")
    favorable = "increase" if args["expected_metric"] in {"property_score", "composite_score"} else "decrease"
    if args["expected_direction"] != favorable:
        raise ValueError("Expected direction must be favorable for the selected metric")
    completed = [h for h in state.hypotheses.values() if h.get("child_id")]
    strategy = None
    if completed:
        previous = completed[-1]
        if previous.get("status") != "assessed":
            raise ValueError("Compare the last child with its parent before planning another edit")
        strategy = next((s for s in reversed(state.strategies)
                         if s["hypothesis_id"] == previous["hypothesis_id"] and s["revision"] == state.revision), None)
        if not strategy or strategy.get("executed_child_id"):
            raise ValueError("Call choose_strategy after the last assessment before recording another hypothesis")
        if strategy["parent_id"] != args["parent_id"]:
            raise ValueError("Hypothesis must use the parent selected by choose_strategy")
        if strategy.get("next_hypothesis_id"):
            raise ValueError("Strategy already has a pending hypothesis")
    hypothesis = {**args, "status": "proposed", "revision": state.revision,
                  "created_at_step": state.steps_used,
                  "decision_rules": {k: _constraints(state)[k] for k in ("min_effects", "max_regressions")}}
    if strategy:
        strategy["next_hypothesis_id"] = hypothesis_id
    state.hypotheses[hypothesis_id] = hypothesis
    return {"hypothesis_id": hypothesis_id, "status": "proposed", "hypothesis": hypothesis}


def _edit_fingerprint(state, action):
    from .evidence import signature
    args = dict(action["arguments"])
    args.pop("hypothesis_id", None)
    parent = state.candidates.get(args.pop("parent_id", None), {})
    return signature([action["tool"], parent.get("smiles"), args])


def _choose_strategy(state, args, directory):
    from .evidence import assess_hypothesis
    h = state.hypotheses.get(args["hypothesis_id"])
    if not h or h.get("status") != "assessed" or not h.get("child_id"):
        raise ValueError(
            "Choose a strategy only after a hypothesis has been assessed; "
            "screening results are not an assessed hypothesis. "
            "Propose a new batch or finish when no screened option meets the numerical threshold."
        )
    child = state.candidates[h["child_id"]]
    assess_hypothesis(state, child)
    choice = args["choice"]
    if choice not in {"continue", "rollback", "switch_strategy"}:
        raise ValueError("choice must be continue, rollback or switch_strategy")
    if choice == "continue" and (h["outcome"] != "supported" or args["parent_id"] != h["child_id"]):
        raise ValueError("Continue requires supported evidence and the assessed child as parent")
    if choice == "rollback" and args["parent_id"] != h["parent_id"]:
        raise ValueError("Rollback must return to the hypothesis source parent")
    parent = _select(state, [args["parent_id"]])[0]
    if parent.get("parent_id") and not parent.get("current_validation", {}).get("passed"):
        raise ValueError("Selected strategy parent violates current constraints")
    state.strategies.append({**args, "revision": state.revision, "step": state.steps_used,
                             "basis": h["outcome"], "status": "selected"})
    return dict(state.strategies[-1])


def _edit(operation):
    def handler(state, args, directory):
        from .editor import apply_edit
        from .molecule_ops import verify_refinement
        parent = _select(state, [args["parent_id"]])[0]
        edit_args = {key: value for key, value in args.items()
                     if key not in {"parent_id", "hypothesis_id"}}
        child_smiles, edit_record = apply_edit(parent["smiles"], operation, **edit_args)
        if any(candidate["smiles"] == child_smiles for candidate in state.candidates.values()):
            raise ValueError("Deterministic edit produced an existing candidate")
        verification = verify_refinement(parent["smiles"], child_smiles, _constraints(state))
        candidate_id = f"c{len(state.candidates) + 1}"
        candidate = {
            "candidate_id": candidate_id,
            "smiles": child_smiles,
            "parent_id": args["parent_id"],
            "candidate_role": "deterministic_edit",
            "revision": state.revision,
            "requested_change": operation,
            "modification_verified": verification["passed"],
            "refinement_verification": verification,
            "edit_record": edit_record,
            "hypothesis_id": args["hypothesis_id"],
            "provider": "rdkit",
            "model": None,
            "is_mock": state.mock,
        }
        state.candidates[candidate_id] = candidate
        from .screening import reuse_for_candidate
        reuse_for_candidate(state, candidate)
        hypothesis = state.hypotheses[args["hypothesis_id"]]
        hypothesis.update(status="edit_executed", child_id=candidate_id, operation=operation,
                          edit_record=edit_record, verification_passed=verification["passed"])
        if not verification["passed"]:
            hypothesis.update(status="assessed", outcome="edit_rejected", observed_delta=None,
                              next_action=hypothesis["next_if_not_supported"],
                              assessed_at_step=state.steps_used,
                              comparison_protocol_consistent=None)
        from .evidence import revalidate
        revalidate(state)
        current = candidate["current_validation"]
        if not current["passed"]:
            from .evidence import assess_hypothesis
            assess_hypothesis(state, candidate)
        for strategy in reversed(state.strategies):
            if strategy.get("next_hypothesis_id") == args["hypothesis_id"]:
                strategy.update(status="executed", executed_child_id=candidate_id, executed_operation=operation,
                                executed_at_step=state.steps_used)
                break
        selection = state.edit_selections.get(hypothesis.get("selection_id"))
        if selection:
            selection.update(status="executed", child_id=candidate_id)
        return {"added_ids": [candidate_id],
                "verified_ids": [candidate_id] if current["passed"] else [],
                "rejected_ids": [] if current["passed"] else [candidate_id],
                "hypothesis_id": args["hypothesis_id"], "edit_record": edit_record,
                "verification": verification, "current_validation": current}
    return handler


def _generate(state, args, directory):
    from agents.generator import generate_with_provider
    from rdkit import Chem
    parents = _select(state, [args["parent_id"]]) if "parent_id" in args else []
    constraints = _constraints(state)
    provider = state.config["llm"]["generators"][0]
    context = {"goal": state.goal, "instructions": state.instructions,
               "constraints": constraints,
               "parent_candidates": parents,
               "existing_smiles": [c["smiles"] for c in state.candidates.values()][-40:]}
    import json
    from .reliability import client_config
    result = generate_with_provider(provider, client_config(state.config), n=args["count"],
                                    focus=args["focus"], memory_context=json.dumps(context, ensure_ascii=False),
                                    use_mock=state.mock)
    if result.get("error"):
        raise RuntimeError(result["error"])
    existing = {c["smiles"] for c in state.candidates.values()}
    additions = []
    for smiles in result["smiles_list"]:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            continue
        smiles = Chem.MolToSmiles(mol)
        if smiles in existing:
            continue
        existing.add(smiles)
        cid = f"c{len(state.candidates) + len(additions) + 1}"
        verification = None
        if parents:
            from .molecule_ops import verify_refinement
            verification = verify_refinement(parents[0]["smiles"], smiles, constraints)
        additions.append({"candidate_id": cid, "smiles": smiles,
                          "parent_id": args.get("parent_id"), "revision": state.revision,
                          "candidate_role": "refinement" if parents else "generated",
                          "requested_change": args["focus"],
                          "modification_verified": verification["passed"] if verification else None,
                          "refinement_verification": verification,
                          "provider": provider, "model": result["model"],
                          "rationale": result["rationale"], "is_mock": state.mock})
    for candidate in additions:
        state.candidates[candidate["candidate_id"]] = candidate
    from .evidence import revalidate
    revalidate(state)
    return {"added_ids": [c["candidate_id"] for c in additions],
            "verified_ids": [c["candidate_id"] for c in additions if c.get("modification_verified") is True],
            "rejected_ids": [c["candidate_id"] for c in additions if c.get("modification_verified") is False],
            "usage": result.get("usage", {}),
            "note": "Structural constraints are verified deterministically; the natural-language edit intent remains a model claim."}


def _evaluate(state, args, directory):
    from agents.evaluator import evaluate_candidates
    selected = _select(state, args["candidate_ids"])
    protocol_id = state.protocol_id
    for candidate in selected:
        if candidate.get("evaluation_status") and candidate.get("protocol_id") != protocol_id:
            for key in ("evaluation_status", "protocol_id", "property_attribution", "composite_score",
                        "property_score", "safety_gate_pass", "dock", "admet", "validate"):
                candidate.pop(key, None)
    missing = [c for c in selected if "evaluation_status" not in c]
    repository = getattr(state, "repository", None)
    if repository is not None and protocol_id:
        for candidate in list(missing):
            persisted = repository.get_evaluation(state.task_id, candidate["candidate_id"], protocol_id)
            if persisted is not None:
                state.candidates[candidate["candidate_id"]] = persisted
        missing = [c for c in missing if "evaluation_status" not in state.candidates[c["candidate_id"]]]
    if missing:
        result = evaluate_candidates(missing, state.config["scoring"], state.config["target"],
                                     dock_enabled=state.dock_enabled,
                                     artifact_dir=str(directory / "artifacts"))
        for candidate in result:
            from .attribution import breakdown
            candidate["property_attribution"] = breakdown(candidate, state.config["scoring"])
            state.candidates[candidate["candidate_id"]] = candidate
            if repository is not None and candidate.get("protocol_id"):
                repository.record_evaluation_attempt(
                    state.task_id, candidate["candidate_id"], candidate["protocol_id"],
                    candidate.get("evaluation_status", "unknown"), candidate,
                )
    return {"evaluated_ids": [c["candidate_id"] for c in missing],
            "reused_ids": [c["candidate_id"] for c in selected if c not in missing],
            "statuses": {cid: state.candidates[cid]["evaluation_status"] for cid in args["candidate_ids"]}}


def _retry_evaluation(state, args, directory):
    for candidate in _select(state, args["candidate_ids"]):
        candidate.setdefault("evaluation_attempts", []).append({
            "status": candidate["evaluation_status"], "dock": candidate.get("dock"),
            "validate": candidate.get("validate")})
        candidate.pop("evaluation_status")
    return _evaluate(state, args, directory)


def _history(state, args, directory):
    # Full event history stays on disk. Explicit retrieval keeps the planner context bounded.
    return {"events": [e for e in state.events if e.get("type") != "tool_result"
                        or e.get("action", {}).get("tool") != "history"][-args["limit"]:]}


def _pause(state, args, directory):
    state.status, state.reason = "paused", "agent_requested_input"
    return {"message": args["message"]}


def _compare(state, args, directory):
    from agents.evaluator import assign_pareto_metadata, candidate_priority_key
    from copy import deepcopy
    selected = deepcopy(_select(state, args["candidate_ids"]))
    if any("evaluation_status" not in c for c in selected):
        raise ValueError("Evaluate selected candidates before comparison")
    assign_pareto_metadata(selected, state.config["scoring"])
    if state.dock_enabled:
        selected.sort(key=candidate_priority_key, reverse=True)
    else:
        selected.sort(key=lambda c: (
            c.get("evaluation_status") == "screening_only",
            bool(c.get("safety_gate_pass")),
            c.get("property_score") if c.get("property_score") is not None else -1,
        ), reverse=True)
    protocols = {c.get("protocol_id") for c in selected if c.get("protocol_id")}
    return {"candidates": [{k: c.get(k) for k in (
        "candidate_id", "smiles", "evaluation_status", "composite_score",
        "property_score", "safety_gate_pass", "pareto_rank", "protocol_id",
        "modification_verified")} for c in selected],
        "same_protocol": len(protocols) <= 1,
        "note": "Screening-only candidates have no complete binding score; safety is a proxy. "
                + ("Scores use different protocols; compare only shared raw fields." if len(protocols) > 1 else "")}


def _compare_parent_child(state, args, directory):
    from .molecule_ops import candidate_goal_assessment, evidence_delta
    rows = []
    for child in _select(state, args["candidate_ids"]):
        parent_id = child.get("parent_id")
        if not parent_id or parent_id not in state.candidates:
            raise ValueError(f"{child['candidate_id']} has no recorded parent")
        parent = state.candidates[parent_id]
        if "evaluation_status" not in parent or "evaluation_status" not in child:
            raise ValueError("Evaluate both parent and child before parent-child comparison")
        delta = evidence_delta(parent, child)
        hypothesis = _assess_hypothesis(state, child, delta)
        rows.append({
            "parent_id": parent_id,
            "child_id": child["candidate_id"],
            "requested_change": child.get("requested_change"),
            "modification_verified": child.get("modification_verified"),
            "verification": child.get("refinement_verification"),
            "current_validation": child.get("current_validation"),
            "evidence_delta": delta,
            "goal_assessment": candidate_goal_assessment(child, _constraints(state)),
            "hypothesis": hypothesis,
        })
    return {"comparisons": rows,
            "note": "Deltas are child minus parent. Lower Vina and hERG-risk values are favorable; higher property/composite values are favorable."}


def _assess_hypothesis(state, child, delta):
    from .evidence import assess_hypothesis
    return assess_hypothesis(state, child)


def _finish(state, args, directory):
    from .evidence import build_report
    selected = _select(state, args["candidate_ids"])
    if any("evaluation_status" not in c for c in selected):
        raise ValueError("Final candidates must have evaluation evidence")
    state.final = build_report(state, args["candidate_ids"], args["summary"])
    success = state.final["outcome"] == "goal_met"
    state.status, state.reason = ("completed", "agent_finished") if success else ("paused", "goal_not_met")
    return state.final


def default_registry():
    from .planning import propose, select, execute_selected
    from .screening import evaluate_options
    from .attribution import METRICS
    registry = ToolRegistry(enforce_state_machine=True)
    text = {"type": "string"}
    ids = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20, "uniqueItems": True}
    generation = {"count": {"type": "integer", "minimum": 1, "maximum": 10}, "focus": text}
    atom = {"type": "integer", "minimum": 0, "maximum": 999}
    hypothesis = {
        "hypothesis_id": text, "parent_id": text, "rationale": text,
        "expected_metric": text, "expected_direction": text, "allowed_tradeoff": text,
        "next_if_supported": text, "next_if_not_supported": text,
    }
    attach = {"parent_id": text, "atom_index": atom, "fragment_smiles": text,
              "fragment_atom_index": atom, "hypothesis_id": text}
    replace = {"parent_id": text, "atom_index": atom, "neighbor_atom_index": atom,
               "fragment_smiles": text, "fragment_atom_index": atom, "hypothesis_id": text}
    remove = {"parent_id": text, "atom_index": atom, "neighbor_atom_index": atom,
              "hypothesis_id": text}
    bond = {"parent_id": text, "atom_index": atom, "neighbor_atom_index": atom,
            "bond_order": text, "hypothesis_id": text}
    def obj(properties, required=None):
        return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}
    predictions = {"type": "array", "minItems": 1, "maxItems": 6, "items": obj({
        "metric": {"type": "string", "enum": list(METRICS)},
        "direction": {"type": "string", "enum": ["increase", "decrease"]},
        "min_change": {"type": "number", "minimum": 1e-9}})}
    option_schema = obj({"edit": obj({"operation": {"type": "string", "enum": ["attach_fragment", "replace_substituent", "remove_terminal_group", "replace_bioisostere", "change_bond_order"]},
        "arguments": obj({"atom_index": atom, "neighbor_atom_index": atom, "fragment_smiles": text, "fragment_atom_index": atom, "bond_order": text}, ["atom_index"])}),
        "rationale": text, "expected_benefit": text, "allowed_cost": text,
        "expected_metric": text, "expected_direction": text, "predictions": predictions})
    for tool in (
        Tool("generate", "Explore candidates under the current goal and instructions.", generation, _generate, True),
        Tool("refine", "Request local modifications to a parent and verify structural constraints.",
             {**generation, "parent_id": text}, _generate, True),
        Tool("import_candidates", "Import exact user-supplied SMILES as immutable seed candidates.",
             {"smiles": ids}, _import_candidates),
        Tool("record_hypothesis", "Persist an optimization hypothesis before editing an evaluated parent. "
             "expected_metric is property_score/composite_score/vina/herg_risk; direction is increase/decrease.",
             hypothesis, _record_hypothesis),
        Tool("choose_strategy", "After an assessed hypothesis select continue (supported child), rollback (original parent), "
             "or switch_strategy (different edit). Required before the next hypothesis.",
             {"hypothesis_id": text, "choice": text, "parent_id": text, "rationale": text}, _choose_strategy),
        Tool("attach_fragment", "Deterministically attach a fragment by atom indices using a single bond.",
             attach, _edit("attach_fragment")),
        Tool("replace_substituent", "Remove the branch beginning at neighbor_atom_index and attach a replacement fragment.",
             replace, _edit("replace_substituent")),
        Tool("remove_terminal_group", "Remove a non-ring terminal branch selected by a core atom and its neighbor.",
             remove, _edit("remove_terminal_group")),
        Tool("replace_bioisostere", "Execute a deterministic substituent replacement; bioisostere equivalence remains a hypothesis.",
             replace, _edit("replace_bioisostere")),
        Tool("change_bond_order", "Change an existing non-arbitrary bond to SINGLE, DOUBLE or TRIPLE.",
             bond, _edit("change_bond_order")),
        Tool("evaluate", "Evaluate IDs using fixed task protocol; reuse saved evaluations.", {"candidate_ids": ids}, _evaluate),
        Tool("compare", "Compare evaluated IDs using fixed scoring; returns evidence.", {"candidate_ids": ids}, _compare),
        Tool("compare_parent_child", "Compare evaluated children with their recorded parents and report metric deltas.",
             {"candidate_ids": ids}, _compare_parent_child),
        Tool("retry_evaluation", "Retry only evaluation_error candidates; preserve prior error evidence.", {"candidate_ids": ids}, _retry_evaluation),
        Tool("history", "Retrieve prior decisions, results and errors.",
             {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}, _history),
        Tool("pause", "Pause for missing information or an unresolved blocker; explain what is needed.", {"message": text}, _pause),
        Tool("finish", "Complete task with evaluated candidate IDs and a qualified summary.",
             {"candidate_ids": ids, "summary": text}, _finish),
        Tool("propose_edits", "Dry-run 2-4 distinct alternatives, without creating candidates or evaluating scores. "
             "options is a structured array. Predictions must be recorded BEFORE screening; include expected changes in score components. "
             "Prechecks return precise failures; revise invalid options.",
             {"parent_id": text, "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": option_schema}}, propose),
        Tool("select_edit", "Select a prechecked alternative and automatically record its hypothesis. Explain why it beats "
             "other options and how cited experience changes this decision. evidence_ids is a structured string array "
             "([] only if no assessed history). After a previous comparison call choose_strategy first.",
             {"proposal_id": text, "option_index": {"type": "integer", "minimum": 0, "maximum": 3},
              "rationale": text, "evidence_ids": {"type": "array", "minItems": 0, "maxItems": 20, "uniqueItems": True, "items": text}}, select),
        Tool("evaluate_options", "Compute local screening properties for feasible alternatives, never docking. Counts every new "
             "unique product evaluation against the same evaluation budget; cached results reused. Returns score attribution, "
             "prediction checks, tradeoffs and diversity. Call before select_edit when require_option_screening is true.",
             {"proposal_id": text}, evaluate_options),
        Tool("execute_selected_edit", "Execute a selected edit exactly once, rechecking current constraints and edit budget.",
             {"selection_id": text}, execute_selected),
    ):
        registry.register(tool)
    return registry
