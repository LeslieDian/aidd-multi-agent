import json
from pathlib import Path
import pytest
from scripts.compare_2d_policies import prepare, audit, run_arm, new_state, load_frozen, catalogue, CatalogRegistry, action, require_connectivity_gate


def test_frozen_catalogue_and_equal_budgets(tmp_path):
    output = tmp_path / "experiment"
    manifest = prepare(output)
    assert len(catalogue()) == 35
    assert manifest["constraints"]["min_effects"]["property_score"] == .01
    assert manifest["llm_transport"] == {
        "provider": "MiniMax", "base_url_host": "api.minimaxi.com", "model": "MiniMax-M3",
        "timeout_seconds": 60, "max_sdk_retries": 0, "trust_env_proxy": False,
        "tls_verify": True, "thinking": "disabled",
        "max_attempts": 3, "retry_base_delay": 1.0, "retry_max_delay": 30.0, "retry_jitter": 0.25}
    for name in ("rule", "agent"):
        state = new_state(manifest, name)
        assert state.max_evaluations == 11
        assert state.candidates["c1"]["smiles"] == "CCOc1ccccc1"
        assert not state.option_screenings
    assert load_frozen(output)["source_hashes"]
    path = output / "manifest.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest changed"):
        load_frozen(output)


def test_agent_arm_is_blocked_without_a_passed_connectivity_gate(tmp_path, monkeypatch):
    """A failed/missing 3/3 connectivity gate must stop v4 before any model call."""
    import scripts.compare_2d_policies as module
    output = tmp_path / "gate"
    prepare(output, scenario="phenol")
    audit(output)
    fake_gate = tmp_path / "gate_summary.json"
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "runs/samples").mkdir(parents=True)
    real_gate = tmp_path / "runs/samples/minimax_connectivity_20260919_summary.json"

    with pytest.raises(ValueError, match="connectivity gate summary is missing"):
        require_connectivity_gate(output)

    real_gate.write_text(json.dumps({"attempted": 3, "successful": 1, "gate": "failed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not passed"):
        require_connectivity_gate(output)

    real_gate.write_text(json.dumps({"attempted": 3, "successful": 3, "gate": "failed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="gate status"):
        require_connectivity_gate(output)

    real_gate.write_text(json.dumps({"attempted": 3, "successful": 3, "gate": "passed"}), encoding="utf-8")
    assert require_connectivity_gate(output)["successful"] == 3


def test_connectivity_gate_uses_the_newest_summary(tmp_path, monkeypatch):
    """A fresh acceptance run must supersede an older gate, not be ignored."""
    import scripts.compare_2d_policies as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    samples = tmp_path / "runs/samples"
    samples.mkdir(parents=True)
    (samples / "minimax_connectivity_20260919_summary.json").write_text(
        json.dumps({"attempted": 3, "successful": 3, "gate": "passed"}), encoding="utf-8")
    (samples / "minimax_connectivity_20260920_summary.json").write_text(
        json.dumps({"attempted": 3, "successful": 2, "gate": "failed"}), encoding="utf-8")
    assert module.connectivity_gate_path().name == "minimax_connectivity_20260920_summary.json"
    with pytest.raises(ValueError, match="not passed"):
        module.require_connectivity_gate()
    # Removing the newer, failed gate falls back to the older passing one.
    (samples / "minimax_connectivity_20260920_summary.json").unlink()
    assert module.require_connectivity_gate()["successful"] == 3


def test_audit_is_separate_and_rule_cannot_overspend(tmp_path):
    output = tmp_path / "experiment"
    manifest = prepare(output)
    reachable = audit(output)
    assert reachable["audit_only_not_arm_budget"]
    assert reachable["audit_evaluations_including_parent"] == reachable["unique_valid_products"] + 1
    fresh = new_state(manifest, "agent")
    assert fresh.evaluations_used == 0
    assert not any("evaluation_status" in c for c in fresh.candidates.values())
    result = run_arm(output, "rule")
    assert result["new_structure_evaluations"] <= 10
    assert result["total_charged_evaluations"] == 1 + result["new_structure_evaluations"]
    assert result["planner_attempts"] == 0
    assert result["rejected_actions"] == 0
    assert result["final_outcome"] in {"goal_met", "goal_not_met"}
    with pytest.raises(ValueError, match="already exists"):
        run_arm(output, "rule")


def test_proposals_outside_shared_catalogue_rejected(tmp_path):
    manifest = prepare(tmp_path / "experiment")
    state = new_state(manifest, "agent")
    options = [{"edit": {"operation": "attach_fragment", "arguments": {"atom_index": 6,
        "fragment_smiles": "Cl", "fragment_atom_index": 0}}, "rationale": "test", "expected_benefit": "test",
        "allowed_cost": "test", "expected_metric": "property_score", "expected_direction": "increase",
        "predictions": [{"metric": "property_score", "direction": "increase", "min_change": .01}]}] * 2
    with pytest.raises(ValueError, match="frozen catalogue") as exc:
        CatalogRegistry(manifest).preflight(state, action("propose_edits", parent_id="c1", options=options))
    # The rejection must name what was rejected and what is allowed.
    assert "unmatched=" in str(exc.value)
    assert "allowed_catalogue_ids=" in str(exc.value)
    assert exc.value.audit_event["type"] == "off_catalogue_edit_attempt"


def test_new_positive_control_is_reachable_and_hides_scores(tmp_path):
    output = tmp_path / "positive_control"
    manifest = prepare(output, scenario="phenol")
    assert manifest["constraints"]["min_effects"]["property_score"] == .01
    with pytest.raises(ValueError, match="requires completed reachability"):
        run_arm(output, "rule")
    result = audit(output)
    assert result["qualifying_products"] > 0
    state = new_state(manifest, "agent")
    assert state.candidates["c1"]["smiles"] == "Oc1ccccc1"
    assert not state.option_screenings
    assert "property_score" not in state.candidates["c1"]
    assert all(set(row) == {"id", "edit"} for row in manifest["catalogue"])
    rule = run_arm(output, "rule")
    assert rule["total_charged_evaluations"] <= 11


def test_goal_not_met_requires_exhaustion_or_budget(tmp_path):
    output = tmp_path / "stop_gate"
    manifest = prepare(output, scenario="phenol")
    audit(output)
    state = new_state(manifest, "agent")
    registry = CatalogRegistry(manifest)
    state.candidates["c1"]["evaluation_status"] = "screening_only"
    with pytest.raises(ValueError, match="unexplored feasible products"):
        registry.preflight(state, action("finish", candidate_ids=["c1"], summary="all failed"))
    try:
        registry.preflight(state, action("finish", candidate_ids=["c1"], summary="all failed"))
    except ValueError as exc:
        assert exc.audit_event["type"] == "invalid_early_stop_attempt"
        assert not exc.audit_event["facts"]["deterministic_stop_allowed"]
        # The rejection must name what is left to do, not only that it is illegal.
        assert exc.audit_event["unexplored_catalogue_ids"]
        assert "unexplored_catalogue_ids=" in str(exc)
    state.evaluations_used = state.max_evaluations
    registry.preflight(state, action("finish", candidate_ids=["c1"], summary="budget exhausted"))


def test_goal_not_met_allowed_after_all_unique_products_are_explored(tmp_path):
    output = tmp_path / "stop_gate_exhausted"
    manifest = prepare(output, scenario="phenol")
    audit(output)
    state = new_state(manifest, "agent")
    state.candidates["c1"]["evaluation_status"] = "screening_only"
    rows = json.loads((output / "structural_catalogue.json").read_text(encoding="utf-8"))
    products = {r["precheck"]["product_smiles"] for r in rows if r["precheck"]["passed"]}
    state.option_screenings = {str(i): {"evaluation": {"smiles": smiles}}
                               for i, smiles in enumerate(products)}
    CatalogRegistry(manifest).preflight(
        state, action("finish", candidate_ids=["c1"], summary="space exhausted"))
    event = state.events[-1]
    assert event["type"] == "diagnostic_stop_decision"
    assert event["facts"]["unexplored_unique_products"] == 0
    assert event["facts"]["deterministic_stop_allowed"]
