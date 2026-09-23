"""Frozen one-parent diagnostic: finite-space audit and equal-budget rule/LLM arms.

The reachability audit is NOT an arm and its scores are never passed to either policy.
No production harness behavior is changed by this experiment.

Baseline arms (greedy / random) bypass the harness and drive evaluate_candidates
directly. They are honest offline evaluators used as stronger-than-rule baselines
for the multi-parent stability study (P3, 2026-09-21).
"""
import argparse
import os
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from agents.evaluator import evaluate_candidates
from agents.harness import TaskState, CheckpointStore, Harness
from agents.harness.runtime import LLMPolicy
from agents.harness.tools import ToolRegistry, default_registry
from agents.harness.planning import preview
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints, evidence_delta
from agents.harness.evidence import judge_effect
from tools.provenance import file_hash, digest, evaluation_protocol, versions


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def edit_key(edit):
    return json.dumps(edit, sort_keys=True)


SCOUT_FRAGMENTS = ("C", "N", "O", "F", "Cl", "CCC", "CCO", "C(F)(F)F", "C#N", "C(=O)N")


def catalogue_for_parent(parent_smiles):
    """Build a one-hop catalogue for an arbitrary parent.

    The catalogue rule is fixed and parent-independent: attach every fragment
    in ``SCOUT_FRAGMENTS`` at every heavy atom of the parent. Sites therefore
    come from the parent itself instead of hand-picked chemistry, which is what
    makes several parents comparable under one protocol.

    Fragments that fail RDKit's valence/feasibility check are kept in the
    catalogue and rejected by precheck, so the structural pass rate stays a
    meaningful, auditable quantity rather than being pre-filtered away.
    """
    from rdkit import Chem
    molecule = Chem.MolFromSmiles(parent_smiles)
    if molecule is None:
        raise ValueError(f"Invalid parent SMILES: {parent_smiles}")
    rows = []
    for site in range(molecule.GetNumAtoms()):
        for fragment in SCOUT_FRAGMENTS:
            rows.append({"id": f"e{len(rows) + 1:02}",
                         "edit": {"operation": "attach_fragment",
                                  "arguments": {"atom_index": site,
                                                "fragment_smiles": fragment,
                                                "fragment_atom_index": 0}}})
    return rows


def catalogue(scenario="phenetole", parent_smiles=None):
    rows = []
    def add(operation, **arguments):
        rows.append({"id": f"e{len(rows)+1:02}", "edit": {"operation": operation, "arguments": arguments}})
    if scenario == "parent":
        # Multi-parent stability study: one shared catalogue rule, one parent.
        if not parent_smiles:
            raise ValueError("scenario 'parent' requires an explicit parent_smiles")
        return catalogue_for_parent(parent_smiles)
    if scenario == "phenol":
        # A new, explicitly separate diagnostic. Indices refer to Oc1ccccc1:
        # hydroxyl O=0, substituted ring C=1, remaining ring atoms=2..6.
        for fragment in ("C", "CC", "CCC", "C(C)C"):
            add("attach_fragment", atom_index=0, fragment_smiles=fragment, fragment_atom_index=0)
        for site in (2, 3, 4, 5, 6):
            for fragment in ("C", "N", "O", "F"):
                add("attach_fragment", atom_index=site, fragment_smiles=fragment, fragment_atom_index=0)
        return rows
    if scenario != "phenetole":
        raise ValueError("Unknown frozen scenario")
    for site in (0, 1, 4, 5, 6, 7, 8):
        for fragment in ("C", "N", "O", "F"):
            add("attach_fragment", atom_index=site, fragment_smiles=fragment, fragment_atom_index=0)
    for fragment in ("C", "CCC", "CCCC", "CCO", "C(C)C"):
        add("replace_substituent", atom_index=2, neighbor_atom_index=1, fragment_smiles=fragment, fragment_atom_index=0)
    add("remove_terminal_group", atom_index=2, neighbor_atom_index=1)
    add("change_bond_order", atom_index=0, neighbor_atom_index=1, bond_order="DOUBLE")
    return rows


def new_state(manifest, arm):
    # The stop rule is stated explicitly because the default task text invites an
    # early stop ("若无合理方案可以提前停止"), while this frozen diagnostic forbids
    # one until the feasible space or the evaluation budget is exhausted. Leaving
    # that contradiction in place made the planner repeatedly attempt an illegal
    # goal_not_met stop and exhausted its error budget (observed in v7, 2026-09-20).
    goal = ("固定二维诊断任务：从母体 c1 出发做单步局部修改，提高 property_score 至少 0.01，保留骨架，"
        "hERG 风险不允许上升，其他数值约束以 constraints 为准。所有编辑必须精确来自下列目录，"
        "不得修改其他母体或自创目录外操作。自己决定先评估哪些方案并使用结果反馈。"
        "新结构评分预算为10，母体另计1，备选评分也计入；相同产物只计一次。"
        "若找到合格方案，请选择、执行并比较后 finish。"
        "重要停止规则：本诊断要求穷尽验证后才可结束。只要仍有未评估的可行目录产物且评分预算未用尽，"
        "finish 会被拒绝。请继续提出并筛选剩余可行方案；只有在评分预算耗尽或所有可行产物都已评估后，"
        "才允许用 finish 报告无合格方案。"
        "目录没有提供评分，禁止假定已知其结果。预测阈值必须为正数。\n"
        + json.dumps(manifest["catalogue"], ensure_ascii=False))
    config = deepcopy(manifest["config"])
    config["_diagnostic_output"] = manifest["output_dir"]
    state = TaskState(goal=goal, config=config, mock=arm != "agent", dock_enabled=False,
        max_steps=int(os.environ.get("AIDD_MAX_STEPS", "45")), max_model_calls=int(os.environ.get("AIDD_MAX_MODEL_CALLS", "60")), max_evaluations=int(os.environ.get("AIDD_MAX_EVALUATIONS", "11")), constraints=deepcopy(manifest["constraints"]))
    add_seed_candidates(state, [manifest["parent_smiles"]], source="frozen_diagnostic")
    return state


def source_hashes():
    files = sorted((ROOT / "agents/harness").glob("*.py")) + [ROOT / p for p in (
        "agents/evaluator.py", "agents/llm.py", "tools/admet_score.py", "tools/validate_mol.py", "tools/sascorer.py",
        "scripts/compare_2d_policies.py")]
    return {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in files}


def prepare(output, scenario="phenetole", parent_smiles=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    planner_name = config.get("harness", {}).get("planner") or config["llm"]["judge"]
    planner = config["llm"]["providers"][planner_name]
    if scenario == "parent":
        if not parent_smiles:
            raise ValueError("scenario 'parent' requires an explicit parent_smiles")
        resolved_parent = parent_smiles
        rationale = ("Multi-parent stability study. The parent was selected from "
                     "scripts/scout_parents.py output, which is offline arithmetic only and "
                     "never passed to a policy; the catalogue rule is identical across parents.")
    elif scenario == "phenol":
        resolved_parent = "Oc1ccccc1"
        rationale = ("phenol chosen as a separate reachable positive-control task based on "
                     "earlier parent-child evidence; not a blind generalization benchmark")
    else:
        resolved_parent = "CCOc1ccccc1"
        rationale = "original diagnostic"
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "output_dir": str(output.resolve()),
        "parent_smiles": resolved_parent,
        "scenario": scenario, "selection_rationale": rationale,
        "config": config, "versions": versions(), "source_hashes": source_hashes(),
        "catalogue": catalogue(scenario, resolved_parent),
        "llm_transport": {"provider": planner_name, "base_url_host": urlparse(planner["base_url"]).hostname,
            "model": planner["model"], "timeout_seconds": config.get("harness", {}).get("request_timeout", 60),
            "max_sdk_retries": 0, "trust_env_proxy": config.get("llm", {}).get("trust_env_proxy", False),
            "tls_verify": True, "thinking": planner.get("extra_body", {}).get("thinking", {}).get("type"),
            "max_attempts": config.get("harness", {}).get("max_attempts", 3),
            "retry_base_delay": config.get("harness", {}).get("retry_base_delay", 1.0),
            "retry_max_delay": config.get("harness", {}).get("retry_max_delay", 30.0),
            "retry_jitter": config.get("harness", {}).get("retry_jitter", 0.25)},
        "constraints": normalize_constraints({"allow_generation": False, "allow_freeform_refine": False,
            "require_planned_edits": True, "require_option_screening": True, "require_verified_refinement": True,
            "require_meaningful_improvement": True, "max_edits": 3}),
        "budgets": {"parent_evaluations_per_arm": 1, "unique_new_structure_evaluations_per_arm": 10,
                    "max_steps": 45, "max_request_attempts": 60, "max_committed_edits": 3},
        "baseline_rule": "precheck feasible, fewest changed atoms, highest Morgan similarity, catalogue ID; deduplicate products; batches of 2",
        "stop_policy": "first supported result can stop; agent may stop earlier; no forced budget exhaustion",
        "analysis": "first-hit cost uses end-of-batch cumulative evaluations (no within-batch ordering advantage); fixed 0.01 effect threshold",
        "interpretation": "one parent, one model run, constrained one-hop space; no statistical superiority or pharmacological conclusion"}
    # Freeze choices before ANY score is calculated, including baseline order's rule.
    write(output / "manifest.json", manifest)
    (output / "manifest.sha256").write_text(file_hash(output / "manifest.json"), encoding="ascii")
    state = new_state(manifest, "rule")
    structural = [{**row, "precheck": preview(state, "c1", row["edit"])} for row in manifest["catalogue"]]
    ordered = sorted([r for r in structural if r["precheck"]["passed"]], key=lambda r: (
        r["precheck"]["verification"]["changed_atoms"], -r["precheck"]["verification"]["similarity"], r["id"]))
    unique, seen = [], set()
    for row in ordered:
        product = row["precheck"]["product_smiles"]
        if product not in seen:
            unique.append(row["id"])
            seen.add(product)
    write(output / "structural_catalogue.json", structural)
    write(output / "rule_order.json", unique)
    return manifest


def load_frozen(output):
    output = Path(output)
    if file_hash(output / "manifest.json") != (output / "manifest.sha256").read_text(encoding="ascii"):
        raise ValueError("Frozen manifest changed")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if manifest["source_hashes"] != source_hashes():
        raise ValueError("Experiment source changed after freeze; do not mix versions")
    return manifest


def audit(output):
    output = Path(output)
    manifest = load_frozen(output)
    if (output / "reachability.json").exists():
        raise ValueError("Audit already exists")
    rows = json.loads((output / "structural_catalogue.json").read_text(encoding="utf-8"))
    products = sorted({r["precheck"]["product_smiles"] for r in rows if r["precheck"]["passed"]})
    cfg = manifest["config"]
    values = evaluate_candidates([{"smiles": s} for s in [manifest["parent_smiles"], *products]], cfg["scoring"], cfg["target"], dock_enabled=False)
    parent, children = values[0], values[1:]
    results = []
    for child in children:
        decision = judge_effect(evidence_delta(parent, child), "property_score", "increase", manifest["constraints"])
        results.append({"smiles": child["smiles"], "catalogue_ids": [r["id"] for r in rows if r["precheck"].get("product_smiles") == child["smiles"]],
                        "decision": decision, "evaluation": child})
    result = {"audit_only_not_arm_budget": True, "scores_hidden_from_policies": True,
        "catalogue_actions": len(rows), "structurally_valid_actions": sum(r["precheck"]["passed"] for r in rows),
        "unique_valid_products": len(products), "audit_evaluations_including_parent": len(values), "parent": parent,
        "qualifying_products": sum(r["decision"]["outcome"] == "supported" for r in results), "results": results}
    write(output / "reachability.json", result)
    return {k: v for k, v in result.items() if k not in {"results", "parent"}}


class CatalogRegistry(ToolRegistry):
    def __init__(self, manifest):
        self.tools = default_registry().tools
        self.enforce_state_machine = True
        for name in ("generate", "refine", "import_candidates"):
            self.tools.pop(name)
        self.rows = manifest["catalogue"]
        self.allowed = {edit_key(r["edit"]) for r in manifest["catalogue"]}

    def preflight(self, state, action, *, enforce_state_machine=False):
        args = action["arguments"]
        if isinstance(args, dict) and "parent_id" in args and args["parent_id"] != "c1":
            # The diagnostic is one-hop only. Check this before generic ID
            # validation so the planner gets the most specific error: name the
            # constraint and the only legal parent instead of "unknown candidate"
            # (observed in v9, 2026-09-20).
            raise ValueError(
                f"Frozen comparison only allows one-hop edits of c1; got parent_id={args['parent_id']!r}; "
                "allowed_parent_ids=['c1']")
        tool = super().preflight(state, action, enforce_state_machine=enforce_state_machine)
        if action["tool"] == "propose_edits" and any(edit_key(o["edit"]) not in self.allowed for o in args["options"]):
            offending = [o["edit"] for o in args["options"] if edit_key(o["edit"]) not in self.allowed]
            error = ValueError(
                "Use exact edits from the frozen catalogue, including operation and atom indices; "
                "unmatched=" + json.dumps(offending, ensure_ascii=True)
                + "; allowed_catalogue_ids=" + json.dumps([r["id"] for r in self.rows]))
            error.audit_event = {"type": "off_catalogue_edit_attempt", "unmatched": offending}
            raise error
        if action["tool"] == "finish":
            rows = json.loads((Path(state.config["_diagnostic_output"]) / "structural_catalogue.json").read_text())
            valid = {r["precheck"]["product_smiles"] for r in rows if r["precheck"].get("passed")}
            explored = {entry["evaluation"]["smiles"] for entry in state.option_screenings.values()}
            remaining = valid - explored
            remaining_budget = max(0, state.max_evaluations - state.evaluations_used)
            remaining_edits = max(0, state.constraints.get("max_edits", 3) -
                                  sum(c.get("candidate_role") == "deterministic_edit"
                                      for c in state.candidates.values()))
            stop_reasons = []
            if remaining_budget == 0:
                stop_reasons.append("budget_exhausted")
            if remaining_edits == 0:
                stop_reasons.append("edit_budget_exhausted")
            if not remaining:
                stop_reasons.append("all_feasible_products_explored")
            goal_met = any(c.get("parent_id") and c.get("current_improvement", {}).get("outcome") == "supported"
                           for c in state.candidates.values())
            facts = {"finite_space_products": len(valid), "explored_unique_products": len(explored),
                     "unexplored_unique_products": len(remaining), "remaining_evaluation_budget": remaining_budget,
                     "remaining_edit_budget": remaining_edits,
                     "deterministic_stop_allowed": bool(stop_reasons), "deterministic_stop_reasons": stop_reasons}
            if not goal_met and not stop_reasons:
                by_smiles = {r["precheck"].get("product_smiles"): r["id"] for r in rows if r["precheck"].get("passed")}
                unexplored_ids = sorted(by_smiles[s] for s in remaining if s in by_smiles)
                error = ValueError(
                    "Illegal goal_not_met stop: unexplored feasible products remain; "
                    "unexplored_catalogue_ids=" + json.dumps(unexplored_ids)
                    + "; remaining_evaluation_budget=" + str(remaining_budget)
                    + "; propose and screen them, or stop only once the budget or the feasible space is exhausted")
                error.audit_event = {"type": "invalid_early_stop_attempt", "facts": facts,
                                     "unexplored_catalogue_ids": unexplored_ids}
                raise error
            state.events.append({"type": "diagnostic_stop_decision", "facts": facts,
                                 "reason": "goal_met" if goal_met else stop_reasons[0]})
        return tool


def action(name, **args):
    return {"tool": name, "arguments": args, "reason": "Frozen deterministic baseline policy"}


def baseline_action(name, *, reason, **args):
    return {"tool": name, "arguments": args, "reason": reason}


class RulePolicy:
    def __init__(self, manifest, order):
        by_id = {r["id"]: r for r in manifest["catalogue"]}
        self.order = [by_id[i] for i in order]

    def decide(self, state, registry):
        if "evaluation_status" not in state.candidates["c1"]:
            return action("evaluate", candidate_ids=["c1"])
        for h in state.hypotheses.values():
            if h["status"] == "edit_executed":
                return action("compare_parent_child", candidate_ids=[h["child_id"]])
            if h["status"] == "assessed":
                return action("finish", candidate_ids=[h["child_id"]], summary="Frozen rule result")
        for s in state.edit_selections.values():
            if s["status"] == "selected":
                return action("execute_selected_edit", selection_id=s["selection_id"])
        for p in reversed(list(state.edit_proposals.values())):
            if "screening_comparison" not in p:
                return action("evaluate_options", proposal_id=p["proposal_id"])
            good = [(i, o) for i, o in enumerate(p["options"]) if o.get("screening", {}).get("effect_assessment", {}).get("outcome") == "supported"]
            if good:
                i, _ = max(good, key=lambda row: row[1]["screening"]["property_score"])
                return action("select_edit", proposal_id=p["proposal_id"], option_index=i, rationale="Highest measured property among supported options in fixed-order batch", evidence_ids=[])
        used = {edit_key(o["edit"]) for p in state.edit_proposals.values() for o in p["options"]}
        remaining = state.max_evaluations - state.evaluations_used
        edits = [r for r in self.order if edit_key(r["edit"]) not in used][:min(2, remaining)]
        if len(edits) < 2:
            return action("finish", candidate_ids=["c1"], summary="No supported candidate in fixed-order budget")
        options = [{"edit": r["edit"], "rationale": "Fixed structural ranking: " + r["id"],
            "expected_benefit": "Property improvement is unverified", "allowed_cost": "Frozen numerical limits",
            "expected_metric": "property_score", "expected_direction": "increase",
            "predictions": [{"metric": "property_score", "direction": "increase", "min_change": .01}]} for r in edits]
        return action("propose_edits", parent_id="c1", options=options)


class BaselinePolicy:
    """Greedy / random baseline: import catalogue products in a fixed order and
    evaluate each until the budget is exhausted. No planning, no model.

    The actual loop is driven by run_baseline() (which calls the evaluator
    directly and writes its own state file), not by the Harness loop. This
    class exists only to expose the order + mode to that driver.
    """

    def __init__(self, manifest, mode="greedy", seed=0):
        rows = manifest["catalogue"]
        state = new_state(manifest, "rule")
        structural = [{**row, "precheck": preview(state, "c1", row["edit"])} for row in rows]
        by_product = {}
        for row in structural:
            if not row["precheck"].get("passed"):
                continue
            smiles = row["precheck"]["product_smiles"]
            entry = by_product.setdefault(smiles, {"smiles": smiles, "catalogue_ids": []})
            entry["catalogue_ids"].append(row["id"])
        order = list(by_product.values())
        if mode == "greedy":
            order.sort(key=lambda e: (-len(e["catalogue_ids"]), e["smiles"]))
        elif mode == "random":
            import random as _random
            rng = _random.Random(seed)
            rng.shuffle(order)
        else:
            raise ValueError(f"baseline mode must be greedy or random, got {mode!r}")
        self.products = order
        self.mode = mode

    def decide(self, state, registry):
        # Not used; the standalone driver in run_baseline drives the loop.
        raise NotImplementedError("BaselinePolicy is metadata-only; use run_baseline()")


def metrics(state, arm):
    successes, total_new, first_hit = [], 0, None
    for event in state.events:
        if event["type"] == "tool_result" and event["action"]["tool"] == "evaluate_options":
            p = event["result"]
            total_new += p["screening_comparison"]["new_evaluations"]
            if first_hit is None and any(o.get("screening", {}).get("effect_assessment", {}).get("outcome") == "supported" for o in p["options"]):
                first_hit = total_new
    parent = state.candidates["c1"]
    for entry in state.option_screenings.values():
        child = entry["evaluation"]
        decision = judge_effect(evidence_delta(parent, child), "property_score", "increase", state.constraints)
        successes.append({"smiles": child["smiles"], "property_score": child["property_score"], "decision": decision})
    options = [o for p in state.edit_proposals.values() for o in p["options"]]
    feasible = [r for r in successes if r["decision"]["outcome"] not in {"tradeoff_exceeded", "insufficient_evidence"}]
    errors = [e for e in state.events if e.get("type") == "error"]
    retries = [e for e in state.events if e.get("type") == "retry"]
    requests = [e for e in state.events if e.get("type") == "model_request"]
    def category(event):
        if event.get("error_category"):
            return event["error_category"]
        message = event.get("error", "")
        return "network" if any(token in message for token in (
            "APIConnectionError", "TimeoutError", "ConnectionError")) else "tool"
    failed_decisions = sum(e.get("phase") == "decision" for e in retries) + sum(
        category(e) == "network" and not e.get("action") for e in errors)
    rejected = [e for e in errors if e.get("action") and category(e) in {"schema", "state_machine", "tool"}]
    stop_events = [e for e in state.events if e.get("type") == "diagnostic_stop_decision"]
    stop_evidence = stop_events[-1] if stop_events else None
    final_outcome = (state.final or {}).get("outcome")
    if final_outcome == "goal_met":
        termination_outcome = "goal_met"
    elif stop_evidence and stop_evidence.get("reason") == "all_feasible_products_explored":
        termination_outcome = "goal_not_met_after_valid_exhaustion"
    elif stop_evidence and stop_evidence.get("reason") in {"budget_exhausted", "edit_budget_exhausted"}:
        termination_outcome = stop_evidence["reason"]
    elif state.reason in {"consecutive_errors", "interrupted_action_requires_acknowledgement"}:
        termination_outcome = "execution_failure"
    elif state.reason == "repeated_action":
        # A decision loop is a property of the planner's choices, not of the
        # execution layer. Reporting it as execution_failure would repeat the
        # exact conflation this diagnostic exists to avoid.
        termination_outcome = "decision_loop"
    elif state.reason in {"user_paused", "user_cancelled"}:
        termination_outcome = "user_stopped"
    else:
        termination_outcome = final_outcome or state.reason
    comparisons = [e for e in state.events if e.get("type") == "tool_result"
                   and e.get("action", {}).get("tool") == "compare_parent_child"]
    strategy_changes = [s for s in state.strategies if s.get("choice") == "switch_strategy"]
    rollbacks = [s for s in state.strategies if s.get("choice") == "rollback"]
    usage_reported = [r["token_usage"] for r in requests if r.get("token_usage_status") == "reported"]
    token_totals = {}
    for usage in usage_reported:
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)):
                token_totals[key] = token_totals.get(key, 0) + value
    return {"arm": arm, "status": state.status, "reason": state.reason, "final_outcome": final_outcome,
        "termination_outcome": termination_outcome, "stop_evidence": stop_evidence,
        "stop_evidence_valid": bool(stop_evidence) or final_outcome is not None,
        "new_structure_evaluations": len(state.option_screenings), "total_charged_evaluations": state.evaluations_used,
        "successful_screened_products": sum(r["decision"]["outcome"] == "supported" for r in successes),
        "first_hit_new_evaluations_batch_end": first_hit,
        "best_compliant_delta": max((r["decision"]["observed_delta"] for r in feasible), default=None),
        "accepted_proposed_options": len(options), "structure_passes": sum(o["precheck"]["passed"] for o in options),
        "structure_pass_rate_among_accepted_options": sum(o["precheck"]["passed"] for o in options)/len(options) if options else None,
        "rejected_actions": len(rejected),
        "network_retries": sum(category(e) == "network" for e in retries),
        "network_failures": sum(category(e) == "network" for e in errors),
        "schema_errors": sum(category(e) == "schema" for e in errors),
        "state_machine_rejections": sum(category(e) == "state_machine" for e in errors),
        "tool_errors": sum(category(e) == "tool" for e in errors),
        "invalid_early_stop_attempts": sum(e.get("type") == "invalid_early_stop_attempt" for e in state.events),
        "planner_attempts": state.model_calls_used if arm == "agent" else 0,
        "successful_planner_responses": max(0, state.model_calls_used - failed_decisions) if arm == "agent" else 0,
        "executed_tool_actions": sum(e.get("type") == "tool_result" for e in state.events),
        "parent_child_comparisons": len(comparisons),
        "strategy_changes": len(strategy_changes),
        "rollbacks": len(rollbacks),
        "evaluation_reuse_count": sum(1 for e in state.events if e.get("type") == "tool_result"
                                      and e.get("action", {}).get("tool") == "evaluate"
                                      and (e.get("result") or {}).get("reused")),
        "model_requests_recorded": len(requests),
        "model_request_outcomes": sorted({r.get("outcome") for r in requests if r.get("outcome")}),
        "token_usage": token_totals or "unavailable",
        "monetary_cost": "unavailable",
        "steps": state.steps_used,
        "actual_edits": sum(c.get("candidate_role") == "deterministic_edit" for c in state.candidates.values()),
        "screened_products": successes,
        "intent_check": "Exact catalogue parameters enforced; free-text chemical claims require separate manual audit"}


def run_arm(output, arm):
    output = Path(output)
    manifest = load_frozen(output)
    reachability = output / "reachability.json"
    if manifest.get("scenario") in {"phenol", "parent"} and (not reachability.exists() or json.loads(reachability.read_text(encoding="utf-8"))["qualifying_products"] == 0):
        raise ValueError("New comparison requires completed reachability audit with a qualifying product")
    if arm == "agent":
        require_connectivity_gate(output)
    allowed_arms = {"agent", "rule", "greedy", "random"}
    if arm not in allowed_arms:
        raise ValueError(f"Unknown arm {arm!r}; allowed={sorted(allowed_arms)}")
    store = CheckpointStore(output / arm)
    if store.path.exists():
        raise ValueError("Arm already exists; no reruns or automatic restarts in this diagnostic")
    store.save(new_state(manifest, arm))
    registry = CatalogRegistry(manifest)
    if arm == "agent":
        # One policy (and therefore one HTTP client) for the whole arm; it is
        # closed explicitly below, even if the arm fails.
        policy = LLMPolicy()
        try:
            state = drive_arm(store, policy, registry, arm)
        finally:
            policy.close()
    elif arm == "rule":
        state = drive_arm(store, RulePolicy(manifest, json.loads((output / "rule_order.json").read_text())),
                          registry, arm)
    elif arm in {"greedy", "random"}:
        # Baselines are offline; no connectivity gate is required.
        state = run_baseline(store, manifest, mode=arm)
    result = metrics(state, arm)
    write(output / arm / "metrics.json", result)
    return result


def run_baseline(store, manifest, mode="greedy", seed=42):
    """Standalone baseline driver: evaluates catalogue products in fixed order
    until the budget is exhausted, then picks the best qualifying one.

    Bypasses the Harness loop because:
    - the baseline has no model decisions to log,
    - the agent state machine has rules that don't apply to a no-decision loop,
    - we want a metrics-compatible state object, not a real execution trace.

    Returns a TaskState whose candidates/evaluations/events are populated so
    metrics() produces the same shape as for rule/agent arms.
    """
    from agents.harness.attribution import breakdown
    state = store.load()
    state.status = "running"
    state.reason = ""
    policy = BaselinePolicy(manifest, mode=mode, seed=seed)
    scoring = state.config["scoring"]
    target = state.config["target"]
    # Evaluate parent first so current_improvement can be computed for each child.
    parent_eval = evaluate_candidates([{"smiles": state.candidates["c1"]["smiles"]}],
                                      scoring, target, dock_enabled=state.dock_enabled,
                                      artifact_dir=str(store.directory / "artifacts"))[0]
    parent_eval["property_attribution"] = breakdown(parent_eval, scoring)
    parent_eval["current_improvement"] = {
        "outcome": "insufficient_evidence", "observed_delta": None,
        "directed_delta": None, "minimum_effect": 0.01,
        "tradeoff_checks": [], "violations": [], "missing_metrics": [],
        "comparison_protocol_consistent": False,
        "threshold_basis": "baseline: parent is the reference, not a child"}
    state.candidates["c1"].update(parent_eval)
    state.evaluations_used += 1
    state.events.append({"type": "baseline_event", "stage": "evaluate_parent",
                         "smiles": parent_eval["smiles"],
                         "property_score": parent_eval.get("property_score"),
                         "sequence": state.evaluations_used})
    # Iterate products in order until budget is exhausted.
    evaluated_smiles = {state.candidates["c1"]["smiles"]}
    for product in policy.products:
        if state.evaluations_used >= state.max_evaluations:
            break
        if product["smiles"] in evaluated_smiles:
            continue
        cid = f"b{state.evaluations_used + 1}"
        cands = [{"smiles": product["smiles"]}]
        evald = evaluate_candidates(cands, scoring, target, dock_enabled=state.dock_enabled,
                                    artifact_dir=str(store.directory / "artifacts"))[0]
        evald["property_attribution"] = breakdown(evald, scoring)
        evald["parent_id"] = "c1"
        evald["candidate_role"] = "baseline_evaluation"
        evald["catalogue_ids"] = product["catalogue_ids"]
        evald["candidate_id"] = cid
        evald["revision"] = state.revision
        evald["is_mock"] = state.mock
        evald["provider"] = "baseline"
        evald["model"] = None
        evald["requested_by_model"] = False
        delta = evidence_delta(state.candidates["c1"], evald)
        decision = judge_effect(delta, "property_score", "increase", state.constraints)
        evald["current_improvement"] = decision
        # Record as an option_screening entry so metrics() can read it like the
        # agent's evaluate_options output.
        state.option_screenings[cid] = {"evaluation": evald, "decision": decision,
                                        "screened_at_step": state.evaluations_used}
        state.candidates[cid] = evald
        evaluated_smiles.add(product["smiles"])
        state.evaluations_used += 1
        state.events.append({"type": "baseline_event", "stage": "evaluate_product",
                             "smiles": evald["smiles"], "catalogue_ids": product["catalogue_ids"],
                             "outcome": decision["outcome"],
                             "property_score": evald.get("property_score"),
                             "sequence": state.evaluations_used})
    # Pick best qualifying or finish honestly.
    qualifying = [(cid, c) for cid, c in state.candidates.items()
                  if cid != "c1" and c.get("current_improvement", {}).get("outcome") == "supported"]
    if qualifying:
        best_cid, best_c = max(qualifying, key=lambda kv: kv[1].get("property_score") or 0)
        state.final = {"outcome": "goal_met", "candidate_ids": [best_cid],
                       "summary": f"Baseline {mode}: best qualifying is {best_c['smiles']}"}
        state.status, state.reason = "completed", "agent_finished"
        state.events.append({"type": "diagnostic_stop_decision", "facts": {},
                             "reason": "goal_met", "best_smiles": best_c["smiles"]})
    else:
        state.final = {"outcome": "goal_not_met", "candidate_ids": ["c1"],
                       "summary": f"Baseline {mode}: no qualifying candidate within budget"}
        state.status, state.reason = "paused", "goal_not_met"
        state.events.append({"type": "diagnostic_stop_decision", "facts": {},
                             "reason": "budget_exhausted"})
    state.steps_used = state.evaluations_used
    store.save(state)
    return state


def connectivity_gate_path():
    """Newest committed connectivity summary, or None.

    The filename embeds an ISO date (``minimax_connectivity_YYYYMMDD_summary.json``),
    so lexical sorting is chronological. Using the newest one means a fresh
    acceptance run supersedes an older gate instead of being ignored.
    """
    candidates = sorted((ROOT / "runs/samples").glob("minimax_connectivity_*_summary.json"))
    return candidates[-1] if candidates else None


def require_connectivity_gate(output=None, gate_path=None):
    """Refuse to start the real agent arm unless the 3/3 connectivity gate passed.

    The gate is the small committed summary produced by
    ``scripts/check_minimax_connectivity.py``. Without it, a transport failure
    would again be misread as an agent decision failure.
    """
    gate = Path(gate_path) if gate_path is not None else connectivity_gate_path()
    if gate is None or not gate.exists():
        raise ValueError("Agent arm blocked: connectivity gate summary is missing")
    summary = json.loads(gate.read_text(encoding="utf-8"))
    if summary.get("successful") != summary.get("attempted") or summary.get("successful") != 3:
        raise ValueError(
            f"Agent arm blocked: connectivity gate not passed "
            f"({summary.get('successful')}/{summary.get('attempted')} successful)")
    if summary.get("gate") != "passed":
        raise ValueError(f"Agent arm blocked: connectivity gate status is {summary.get('gate')!r}")
    return {**summary, "summary_file": str(gate.relative_to(ROOT)).replace("\\", "/")}


def drive_arm(store, policy, registry, arm):
    for _ in range(45):
        state = Harness(store, policy, registry).run(1)
        print(json.dumps({"arm": arm, "step": state.steps_used, "calls": state.model_calls_used if arm == "agent" else 0,
                          "evaluations": state.evaluations_used, "reason": state.reason}), flush=True)
        if state.reason != "action_limit":
            break
    return state


def report(output):
    output = Path(output)
    load_frozen(output)
    audit_result = json.loads((output / "reachability.json").read_text(encoding="utf-8"))
    arms = {}
    for a in ("rule", "agent", "greedy", "random"):
        if (output / a / "metrics.json").exists():
            arms[a] = json.loads((output / a / "metrics.json").read_text(encoding="utf-8"))
    result = {"reachable_qualifying_products": audit_result["qualifying_products"], "finite_space_products": audit_result["unique_valid_products"],
              "arms": arms, "limitations": ["One parent and one real model run; no statistical superiority claim",
                "Shared fixed catalogue tests acquisition/selection, not unrestricted molecular invention",
                "Reachability scores evaluated separately and hidden from policies; excluded from arm budgets",
                "Rejected actions reported separately from accepted-option structural pass rate",
                "No docking, biological validation, or threshold adjustment",
                "Baseline arms (greedy/random) bypass the harness state machine and drive evaluate_candidates directly; they are not model-based."]}
    write(output / "comparison.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "audit", "rule", "agent", "greedy", "random", "report"])
    parser.add_argument("--output", required=True)
    parser.add_argument("--scenario", choices=["phenetole", "phenol", "parent"], default="phenetole")
    parser.add_argument("--parent", default=None,
                        help="Parent SMILES; required when --scenario parent")
    args = parser.parse_args()
    if args.stage == "prepare":
        result = prepare(args.output, args.scenario, args.parent)
        result = {"prepared": True, "catalogue_actions": len(result["catalogue"]),
                  "parent_smiles": result["parent_smiles"], "scenario": result["scenario"]}
    elif args.stage == "audit":
        result = audit(args.output)
    elif args.stage in {"rule", "agent", "greedy", "random"}:
        result = run_arm(args.output, args.stage)
    else:
        result = report(args.output)
    print(json.dumps(result, ensure_ascii=True, indent=2))
