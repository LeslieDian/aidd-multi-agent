import json
from pathlib import Path
import pytest
from scripts.compare_2d_policies import prepare, audit, run_arm, new_state, load_frozen, catalogue, CatalogRegistry, action, require_connectivity_gate, SCOUT_FRAGMENTS


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


# ---------------------------------------------------------------------------
# Multi-parent support (2026-09-20): the stability study needs parents beyond
# phenol, because every run so far used the same one and the phenetole
# scenario has zero reachable qualifying products.
# ---------------------------------------------------------------------------

def test_arbitrary_parent_catalogue_is_parent_derived_and_reproducible(tmp_path):
    """One shared catalogue rule must apply to any parent."""
    output = tmp_path / "multiparent"
    manifest = prepare(output, scenario="parent", parent_smiles="Cc1ccccc1")
    assert manifest["parent_smiles"] == "Cc1ccccc1"
    assert manifest["scenario"] == "parent"
    # Toluene has 7 heavy atoms x 10 fragments.
    assert len(manifest["catalogue"]) == 70
    assert all(row["edit"]["operation"] == "attach_fragment" for row in manifest["catalogue"])
    sites = {row["edit"]["arguments"]["atom_index"] for row in manifest["catalogue"]}
    assert sites == set(range(7))
    fragments = {row["edit"]["arguments"]["fragment_smiles"] for row in manifest["catalogue"]}
    assert fragments == set(SCOUT_FRAGMENTS)
    # The same parent and code must give the same frozen catalogue.
    other = tmp_path / "multiparent_repeat"
    repeat = prepare(other, scenario="parent", parent_smiles="Cc1ccccc1")
    assert repeat["catalogue"] == manifest["catalogue"]


def test_parent_scenario_requires_an_explicit_parent(tmp_path):
    with pytest.raises(ValueError, match="requires an explicit parent_smiles"):
        prepare(tmp_path / "no_parent", scenario="parent")


def test_invalid_parent_is_rejected_before_freezing(tmp_path):
    with pytest.raises(ValueError, match="Invalid parent SMILES"):
        prepare(tmp_path / "bad_parent", scenario="parent", parent_smiles="not-a-molecule")


def test_multiparent_audit_qualifies_and_rule_arm_stays_in_budget(tmp_path):
    """A scout-selected parent must be a usable positive control end to end."""
    output = tmp_path / "toluene"
    manifest = prepare(output, scenario="parent", parent_smiles="Cc1ccccc1")
    reachable = audit(output)
    assert reachable["qualifying_products"] > 0, "the scout promised this parent has a reachable target"
    assert reachable["audit_only_not_arm_budget"]
    assert reachable["scores_hidden_from_policies"]
    rule = run_arm(output, "rule")
    assert rule["total_charged_evaluations"] <= 11
    assert rule["network_failures"] == 0
    assert rule["stop_evidence_valid"]


def test_multiparent_agent_arm_still_requires_the_connectivity_gate(tmp_path, monkeypatch):
    """The gate must not be bypassable just because the parent changed."""
    import scripts.compare_2d_policies as module
    output = tmp_path / "gated"
    prepare(output, scenario="parent", parent_smiles="Cc1ccccc1")
    audit(output)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "runs/samples").mkdir(parents=True)
    with pytest.raises(ValueError, match="connectivity gate summary is missing"):
        module.require_connectivity_gate(output)


def test_greedy_baseline_drives_loop_without_harness(tmp_path):
    """Greedy baseline evaluates catalogue products in order, picks best
    qualifying, and produces a metrics-compatible state. It must not depend
    on the agent harness loop or any HTTP client."""
    from scripts.compare_2d_policies import prepare, audit, run_arm, BaselinePolicy
    output = tmp_path / "baseline_greedy"
    prepare(output, scenario="parent", parent_smiles="Cc1ccccc1")
    audit(output)
    metrics = run_arm(output, "greedy")
    assert metrics["arm"] == "greedy"
    # Greedy ran offline with no model_calls_used at all.
    assert metrics["planner_attempts"] == 0
    assert metrics["model_requests_recorded"] == 0
    # It did evaluate products within budget.
    assert metrics["new_structure_evaluations"] >= 1
    # Termination outcome is one of the valid budget/goal outcomes.
    assert metrics["termination_outcome"] in {
        "goal_met", "budget_exhausted", "goal_not_met_after_valid_exhaustion",
    }
    # best_compliant_delta is None when nothing qualifies; otherwise a number.
    assert metrics["best_compliant_delta"] is None or isinstance(metrics["best_compliant_delta"], float)


def test_random_baseline_uses_deterministic_seed(tmp_path):
    """Random baseline must produce the same result on repeated runs at the
    same seed (deterministic shuffle, deterministic evaluation cache)."""
    from scripts.compare_2d_policies import prepare, audit, run_arm
    out1 = tmp_path / "baseline_random_a"
    prepare(out1, scenario="parent", parent_smiles="Nc1ccccc1")
    audit(out1)
    metrics_a = run_arm(out1, "random")
    out2 = tmp_path / "baseline_random_b"
    prepare(out2, scenario="parent", parent_smiles="Nc1ccccc1")
    audit(out2)
    metrics_b = run_arm(out2, "random")
    assert metrics_a["best_compliant_delta"] == metrics_b["best_compliant_delta"]
    assert metrics_a["successful_screened_products"] == metrics_b["successful_screened_products"]


def test_baseline_arm_is_refused_without_audit(tmp_path):
    """Baselines must refuse to run if reachability audit is missing or
    showed zero qualifying products (same rule as rule/agent arms)."""
    from scripts.compare_2d_policies import prepare
    import pytest
    output = tmp_path / "no_audit"
    prepare(output, scenario="parent", parent_smiles="Cc1ccccc1")
    with pytest.raises(ValueError, match="completed reachability audit"):
        run_arm(output, "greedy")
