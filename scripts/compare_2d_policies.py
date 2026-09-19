"""Frozen one-parent diagnostic: finite-space audit and equal-budget rule/LLM arms.

The reachability audit is NOT an arm and its scores are never passed to either policy.
No production harness behavior is changed by this experiment.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

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


def catalogue(scenario="phenetole"):
    rows = []
    def add(operation, **arguments):
        rows.append({"id": f"e{len(rows)+1:02}", "edit": {"operation": operation, "arguments": arguments}})
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
    goal = ("固定二维诊断任务：从母体 c1 出发做单步局部修改，提高 property_score 至少 0.01，保留骨架，"
        "hERG 风险不允许上升，其他数值约束以 constraints 为准。所有编辑必须精确来自下列目录，"
        "不得修改其他母体或自创目录外操作。自己决定先评估哪些方案并使用结果反馈。"
        "新结构评分预算为10，母体另计1，备选评分也计入；相同产物只计一次。"
        "若找到合格方案，请选择、执行并比较后 finish；若无合理方案可以提前停止。"
        "目录没有提供评分，禁止假定已知其结果。预测阈值必须为正数。\n"
        + json.dumps(manifest["catalogue"], ensure_ascii=False))
    state = TaskState(goal=goal, config=deepcopy(manifest["config"]), mock=arm != "agent", dock_enabled=False,
        max_steps=45, max_model_calls=60, max_evaluations=11, constraints=deepcopy(manifest["constraints"]))
    add_seed_candidates(state, [manifest["parent_smiles"]], source="frozen_diagnostic")
    return state


def source_hashes():
    files = sorted((ROOT / "agents/harness").glob("*.py")) + [ROOT / p for p in (
        "agents/evaluator.py", "agents/llm.py", "tools/admet_score.py", "tools/validate_mol.py", "tools/sascorer.py",
        "scripts/compare_2d_policies.py")]
    return {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in files}


def prepare(output, scenario="phenetole"):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "parent_smiles": "Oc1ccccc1" if scenario == "phenol" else "CCOc1ccccc1",
        "scenario": scenario, "selection_rationale": "phenol chosen as a separate reachable positive-control task based on earlier parent-child evidence; not a blind generalization benchmark" if scenario == "phenol" else "original diagnostic",
        "config": config, "versions": versions(), "source_hashes": source_hashes(), "catalogue": catalogue(scenario),
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
        for name in ("generate", "refine", "import_candidates"):
            self.tools.pop(name)
        self.allowed = {edit_key(r["edit"]) for r in manifest["catalogue"]}

    def preflight(self, state, action):
        tool = super().preflight(state, action)
        args = action["arguments"]
        if "parent_id" in args and args["parent_id"] != "c1":
            raise ValueError("Frozen comparison only allows one-hop edits of c1")
        if action["tool"] == "propose_edits" and any(edit_key(o["edit"]) not in self.allowed for o in args["options"]):
            raise ValueError("Use exact edits from the frozen catalogue, including operation and atom indices")
        return tool


def action(name, **args):
    return {"tool": name, "arguments": args, "reason": "Frozen deterministic baseline policy"}


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
    return {"arm": arm, "status": state.status, "reason": state.reason, "final_outcome": (state.final or {}).get("outcome"),
        "new_structure_evaluations": len(state.option_screenings), "total_charged_evaluations": state.evaluations_used,
        "successful_screened_products": sum(r["decision"]["outcome"] == "supported" for r in successes),
        "first_hit_new_evaluations_batch_end": first_hit,
        "best_compliant_delta": max((r["decision"]["observed_delta"] for r in feasible), default=None),
        "accepted_proposed_options": len(options), "structure_passes": sum(o["precheck"]["passed"] for o in options),
        "structure_pass_rate_among_accepted_options": sum(o["precheck"]["passed"] for o in options)/len(options) if options else None,
        "rejected_actions": sum(e["type"] == "error" for e in state.events),
        "network_retries": sum(e["type"] == "retry" for e in state.events), "steps": state.steps_used,
        "planner_attempts": state.model_calls_used if arm == "agent" else 0,
        "actual_edits": len(state.candidates)-1, "screened_products": successes,
        "intent_check": "Exact catalogue parameters enforced; free-text chemical claims require separate manual audit"}


def run_arm(output, arm):
    output = Path(output)
    manifest = load_frozen(output)
    reachability = output / "reachability.json"
    if manifest.get("scenario") == "phenol" and (not reachability.exists() or json.loads(reachability.read_text(encoding="utf-8"))["qualifying_products"] == 0):
        raise ValueError("New comparison requires completed reachability audit with a qualifying product")
    store = CheckpointStore(output / arm)
    if store.path.exists():
        raise ValueError("Arm already exists; no reruns or automatic restarts in this diagnostic")
    store.save(new_state(manifest, arm))
    registry = CatalogRegistry(manifest)
    policy = LLMPolicy() if arm == "agent" else RulePolicy(manifest, json.loads((output / "rule_order.json").read_text()))
    for _ in range(45):
        state = Harness(store, policy, registry).run(1)
        print(json.dumps({"arm": arm, "step": state.steps_used, "calls": state.model_calls_used if arm == "agent" else 0,
                          "evaluations": state.evaluations_used, "reason": state.reason}), flush=True)
        if state.reason != "action_limit":
            break
    result = metrics(state, arm)
    write(output / arm / "metrics.json", result)
    return result


def report(output):
    output = Path(output)
    load_frozen(output)
    audit_result = json.loads((output / "reachability.json").read_text(encoding="utf-8"))
    arms = {a: json.loads((output / a / "metrics.json").read_text(encoding="utf-8")) for a in ("rule", "agent")}
    result = {"reachable_qualifying_products": audit_result["qualifying_products"], "finite_space_products": audit_result["unique_valid_products"],
              "arms": arms, "limitations": ["One parent and one real model run; no statistical superiority claim",
                "Shared fixed catalogue tests acquisition/selection, not unrestricted molecular invention",
                "Reachability scores evaluated separately and hidden from policies; excluded from arm budgets",
                "Rejected actions reported separately from accepted-option structural pass rate",
                "No docking, biological validation, or threshold adjustment"]}
    write(output / "comparison.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "audit", "rule", "agent", "report"])
    parser.add_argument("--output", required=True)
    parser.add_argument("--scenario", choices=["phenetole", "phenol"], default="phenetole")
    args = parser.parse_args()
    if args.stage == "prepare":
        result = prepare(args.output, args.scenario)
        result = {"prepared": True, "catalogue_actions": len(result["catalogue"])}
    elif args.stage == "audit":
        result = audit(args.output)
    elif args.stage in {"rule", "agent"}:
        result = run_arm(args.output, args.stage)
    else:
        result = report(args.output)
    print(json.dumps(result, ensure_ascii=True, indent=2))
