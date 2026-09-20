"""Offline acceptance tests for persistent agent tasks."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.tools import Tool, ToolRegistry
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints, verify_refinement
from agents.harness.editor import apply_edit, molecule_atom_table, molecule_svg_data_url


def create(tmp_path, **kwargs):
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    store = CheckpointStore(tmp_path / "task")
    store.save(TaskState(goal="Find EGFR candidates", config=config, mock=True, **kwargs))
    return store


def action(name, **kwargs):
    return {"tool": name, "arguments": kwargs, "reason": "Test decision"}


class Policy:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.observed = []

    def decide(self, state, registry):
        self.observed.append(deepcopy(state))
        return next(self.actions)


def test_pause_resume_with_new_instruction_retains_evidence(tmp_path, monkeypatch):
    store = create(tmp_path)
    first = Harness(store).run(max_actions=2)
    assert first.status == "paused"
    assert len(first.candidates) == 3
    assert all(c["evaluation_status"] == "screening_only" for c in first.candidates.values())
    original = deepcopy(first.candidates)
    import agents.evaluator
    monkeypatch.setattr(agents.evaluator, "evaluate_candidates",
                        lambda *a, **k: pytest.fail("Completed evaluations must not be repeated"))
    ids = list(first.candidates)
    policy = Policy([action("compare", candidate_ids=ids),
                     action("finish", candidate_ids=ids, summary="Prioritize property evidence")])
    result = Harness(CheckpointStore(store.directory), policy=policy).run(
        max_actions=2, instruction="优先比较已有候选，暂时不要生成新分子")
    assert result.status == "completed"
    assert result.candidates == original
    assert policy.observed[0].instructions[-1]["text"].startswith("优先比较")
    assert result.revision == 1
    assert result.steps_used == 4
    assert len([e for e in result.events if e["type"] == "tool_result"]) == 4
    assert store.load().final == result.final


def test_interrupted_call_requires_explicit_recovery_and_never_replays(tmp_path):
    store = create(tmp_path)
    registry = ToolRegistry()
    def interrupt(state, args, directory):
        state.candidates["partial"] = {"smiles": "CCO"}
        raise KeyboardInterrupt
    registry.register(Tool("interrupt", "test", {}, interrupt))
    state = Harness(store, Policy([action("interrupt")]), registry).run(1)
    assert state.pending
    assert state.candidates == {}
    blocked = Harness(store).run(1)
    assert blocked.reason == "interrupted_action_requires_acknowledgement"
    assert blocked.steps_used == 1
    resumed = Harness(store).run(1, acknowledge_interrupted=True)
    assert resumed.pending is None
    assert any(e["type"] == "interrupted_action" for e in resumed.events)
    assert resumed.candidates


def test_invalid_decision_recoverable_and_failed_mutations_discarded(tmp_path):
    store = create(tmp_path)
    registry = ToolRegistry()
    def broken(state, args, directory):
        state.candidates["partial"] = {}
        raise ValueError("tool failure")
    registry.register(Tool("broken", "test", {}, broken))
    policy = Policy([action("unknown"), action("broken"), action("unknown")])
    state = Harness(store, policy, registry).run(3)
    assert state.reason == "consecutive_errors"
    assert state.candidates == {}
    assert state.pending is None
    assert len([e for e in state.events if e["type"] == "error"]) == 3


def test_budget_persists_across_resume(tmp_path):
    store = create(tmp_path, max_steps=2)
    Harness(store).run(1)
    state = Harness(store).run(4)
    assert state.steps_used == 2
    assert state.reason == "step_budget_exhausted"
    assert Harness(store).run(4).steps_used == 2


def test_repeated_action_pauses(tmp_path):
    store = create(tmp_path)
    registry = ToolRegistry()
    registry.register(Tool("noop", "test", {}, lambda *args: {}))
    state = Harness(store, Policy([action("noop")] * 3), registry).run(4)
    assert state.reason == "repeated_action"
    assert len([e for e in state.events if e["type"] == "tool_result"]) == 2


def test_reworded_repeat_is_still_a_repeat(tmp_path):
    """Rewording a rejected action must not evade the repeat guard.

    A planner that kept rephrasing an illegal ``finish`` summary retried the same
    illegal stop until the consecutive-error budget tripped, reporting a
    state-machine disagreement as an ``execution_failure`` (observed in v7).
    """
    store = create(tmp_path)
    registry = ToolRegistry()
    registry.register(Tool("noop", "test", {}, lambda *args: {}))
    policy = Policy([{"tool": "noop", "arguments": {}, "reason": f"attempt {i}"} for i in range(3)])
    state = Harness(store, policy, registry).run(4)
    assert state.reason == "repeated_action"
    assert len([e for e in state.events if e["type"] == "tool_result"]) == 2


def test_unknown_candidate_rejected_without_tool_call(tmp_path):
    store = create(tmp_path)
    state = Harness(store, Policy([action("evaluate", candidate_ids=["missing"])])).run(1)
    assert "Unknown candidate" in state.events[-1]["error"]
    assert not state.candidates


def test_checkpoint_version_and_exclusive_lock(tmp_path):
    store = create(tmp_path)
    with store.lock():
        with pytest.raises(RuntimeError, match="already running"):
            with CheckpointStore(store.directory).lock():
                pass
    with store.lock():
        assert store.load().goal
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        store.load()


def test_cli_acceptance(tmp_path, capsys):
    from agent_task import main
    directory = str(tmp_path / "cli")
    assert main(["start", "--goal", "EGFR screening", "--mock", "--steps", "2",
                 "--task-dir", directory]) == 0
    assert CheckpointStore(directory).load().status == "paused"
    assert main(["resume", "--task-dir", directory, "--instruction", "Compare existing results",
                 "--steps", "2"]) == 0
    assert CheckpointStore(directory).load().status == "completed"
    assert main(["status", "--task-dir", directory]) == 0
    assert "screening_only" in capsys.readouterr().out


def test_commit_failure_preserves_pending_and_previous_results(tmp_path, monkeypatch):
    store = create(tmp_path)
    save = store.save
    def fail_commit(state):
        if any(e["type"] == "tool_result" for e in state.events):
            raise OSError("disk full")
        save(state)
    monkeypatch.setattr(store, "save", fail_commit)
    with pytest.raises(OSError, match="disk full"):
        Harness(store).run(1)
    persisted = store.load()
    assert persisted.pending["tool"] == "generate"
    assert persisted.candidates == {}
    recovered = Harness(CheckpointStore(store.directory)).run(1)
    assert recovered.reason == "action_limit"
    assert len(recovered.candidates) == 3
    assert [e["action"]["tool"] for e in recovered.events if e["type"] == "tool_result"] == ["generate", "evaluate"]


def test_real_policy_passes_latest_requirements_and_tool_contracts(tmp_path, monkeypatch):
    from agents.harness.runtime import LLMPolicy
    from agents.harness.tools import default_registry
    import agents.llm
    store = create(tmp_path)
    state = store.load()
    state.steer("Use existing evidence first")
    response = action("generate", count=2, focus="EGFR")
    class Client:
        def chat_json(self, system, user):
            context = json.loads(user)
            assert context["instructions"][-1]["text"] == "Use existing evidence first"
            assert context["tools"][0]["name"] == "generate"
            assert context["steps_remaining"] == 20
            return response
    monkeypatch.setattr(agents.llm, "get_client", lambda *a, **k: Client())
    assert LLMPolicy().decide(state, default_registry()) == response


def test_planned_policy_hides_strategy_until_hypothesis_is_assessed(tmp_path):
    from agents.harness.runtime import LLMPolicy
    from agents.harness.tools import default_registry
    store = create(tmp_path)
    state = store.load()
    state.constraints.update(require_planned_edits=True, require_option_screening=True)
    state.candidates["c1"] = {
        "candidate_id": "c1", "smiles": "CCO", "evaluation_status": "screening_only"
    }
    state.hypotheses["planned_s1"] = {
        "hypothesis_id": "planned_s1", "status": "proposed", "revision": state.revision,
        "parent_id": "c1"
    }

    class Client:
        def chat_json(self, system, user):
            context = json.loads(user)
            names = {tool["name"] for tool in context["tools"]}
            assert "choose_strategy" not in names
            assert "propose_edits" in names
            return action("finish", candidate_ids=["c1"], summary="No screened option met the threshold")

    import agents.llm
    original = agents.llm.get_client
    agents.llm.get_client = lambda *a, **k: Client()
    try:
        assert LLMPolicy().decide(state, default_registry())["tool"] == "finish"
    finally:
        agents.llm.get_client = original


def test_available_actions_excludes_strategy_for_unqualified_screening(tmp_path):
    from agents.harness.tools import available_actions, default_registry
    store = create(tmp_path)
    state = store.load()
    state.constraints.update(require_planned_edits=True, require_option_screening=True)
    state.candidates["c1"] = {"candidate_id": "c1", "smiles": "CCO", "evaluation_status": "screening_only"}
    state.edit_proposals["p1"] = {"proposal_id": "p1", "revision": state.revision, "parent_id": "c1",
        "screening_protocol_id": "unused", "options": [
            {"precheck": {"passed": True}, "screening": {"evaluation_status": "screening_only",
             "effect_assessment": {"outcome": "tradeoff_exceeded"}}}]}
    from agents.harness import screening
    state.edit_proposals["p1"]["screening_protocol_id"] = screening.protocol(state)
    availability = available_actions(state)
    assert availability["stage"] == "screening_no_qualifying_option"
    assert "choose_strategy" not in availability["tools"]
    with pytest.raises(ValueError, match="allowed_tools"):
        default_registry().preflight(state, action("choose_strategy", hypothesis_id="h1",
            choice="switch_strategy", parent_id="c1", rationale="invalid"))


def test_registry_description_and_enforcement_share_available_actions(tmp_path):
    from agents.harness.tools import available_actions, default_registry
    state = create(tmp_path).load()
    state.constraints.update(require_planned_edits=True, allow_generation=False)
    registry = default_registry()
    availability = available_actions(state)
    assert availability == {"stage": "no_candidates", "tools": ["pause"],
                            "references": {"candidate_ids": []}}
    assert [item["name"] for item in registry.describe(state)] == ["pause"]
    with pytest.raises(ValueError, match="stage=no_candidates"):
        registry.preflight(state, action("history", limit=1), enforce_state_machine=True)


def test_strategy_decision_cannot_skip_directly_to_new_proposal(tmp_path):
    from agents.harness.tools import available_actions, default_registry
    state = create(tmp_path).load()
    state.constraints.update(require_planned_edits=True)
    state.candidates = {
        "c1": {"candidate_id": "c1", "smiles": "CCO", "evaluation_status": "screening_only"},
        "c2": {"candidate_id": "c2", "smiles": "CCCO", "parent_id": "c1",
               "evaluation_status": "screening_only"},
    }
    state.hypotheses["h1"] = {"hypothesis_id": "h1", "parent_id": "c1",
                                "child_id": "c2", "status": "assessed"}
    assert available_actions(state)["stage"] == "strategy_decision"
    option = {"edit": {"operation": "attach_fragment", "arguments": {
                  "atom_index": 0, "fragment_smiles": "C", "fragment_atom_index": 0}},
              "rationale": "different edit", "expected_benefit": "test",
              "allowed_cost": "limits apply", "expected_metric": "property_score",
              "expected_direction": "increase", "predictions": [{
                  "metric": "property_score", "direction": "increase", "min_change": .01}]}
    with pytest.raises(ValueError, match="stage=strategy_decision"):
        default_registry().preflight(state, action("propose_edits", parent_id="c1",
            options=[option, deepcopy(option)]), enforce_state_machine=True)


def test_terminal_state_exposes_and_accepts_no_model_tools(tmp_path):
    from agents.harness.tools import available_actions, default_registry
    state = create(tmp_path).load()
    state.status = "completed"
    assert available_actions(state)["tools"] == []
    assert default_registry().describe(state) == []
    with pytest.raises(ValueError, match="stage=terminal"):
        default_registry().preflight(state, action("pause", message="wait"),
                                     enforce_state_machine=True)


def test_transient_planner_retry_and_budget_accounted(tmp_path):
    store = create(tmp_path)
    calls = []
    class Flaky:
        def decide(self, state, registry):
            calls.append(state.model_calls_used)
            if len(calls) == 1:
                raise TimeoutError("temporary")
            return action("generate", count=1, focus="EGFR")
    state = Harness(store, Flaky(), sleep=lambda _: None).run(1)
    assert calls == [1, 2]
    assert state.model_calls_used == 3  # Two planner attempts, one generator.
    assert len(state.candidates) == 1
    assert any(e["type"] == "retry" for e in state.events)


def test_malformed_action_is_feedback_for_next_decision(tmp_path):
    store = create(tmp_path)
    policy = Policy([["bad JSON shape"], action("generate", count=1, focus="EGFR")])
    state = Harness(store, policy).run(2)
    assert "Action must contain" in policy.observed[1].events[-1]["error"]
    assert len(state.candidates) == 1


def test_safe_generator_retries_without_partial_candidate_commit(tmp_path, monkeypatch):
    import agents.generator
    store = create(tmp_path)
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ConnectionError("temporary")
        return {"smiles_list": ["CCO"], "model": "test", "rationale": "test"}
    monkeypatch.setattr(agents.generator, "generate_with_provider", generate)
    state = Harness(store, Policy([action("generate", count=1, focus="EGFR")]), sleep=lambda _: None).run(1)
    assert len(calls) == 2
    assert list(state.candidates) == ["c1"]
    assert state.model_calls_used == 3


def test_model_budget_blocks_generator_and_survives_resume(tmp_path):
    store = create(tmp_path, max_model_calls=1)
    state = Harness(store).run(3)
    assert state.reason == "model_call_budget_exhausted"
    assert not state.candidates
    assert state.model_calls_used == 1
    assert Harness(store).run(1).model_calls_used == 1


def test_evaluation_budget_blocks_before_tool(tmp_path, monkeypatch):
    store = create(tmp_path, max_evaluations=2)
    Harness(store).run(1)
    import agents.evaluator
    monkeypatch.setattr(agents.evaluator, "evaluate_candidates", lambda *a, **k: pytest.fail("must not execute"))
    state = Harness(store).run(1)
    assert state.reason == "evaluation_budget_exhausted"
    assert state.evaluations_used == 0


def test_live_instruction_discards_stale_decision(tmp_path):
    store = create(tmp_path)
    class Steering:
        calls = 0
        def decide(self, state, registry):
            self.calls += 1
            if self.calls == 1:
                store.submit_control("steer", "Do not generate; pause for clarification")
                return action("generate", count=1, focus="stale")
            assert state.instructions[-1]["text"].startswith("Do not generate")
            return action("pause", message="What constraint should I use?")
    state = Harness(store, Steering()).run(2)
    assert not state.candidates
    assert state.revision == 1
    assert state.reason == "agent_requested_input"
    assert len(state.processed_controls) == 1


@pytest.mark.parametrize("kind,expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_control_during_tool_commits_result_then_stops(tmp_path, kind, expected):
    store = create(tmp_path)
    registry = ToolRegistry()
    def handler(state, args, directory):
        store.submit_control(kind)
        state.candidates["c1"] = {"smiles": "CCO"}
        return {"ok": True}
    registry.register(Tool("work", "test", {}, handler))
    state = Harness(store, Policy([action("work")]), registry).run(4)
    assert state.status == expected
    assert "c1" in state.candidates
    assert state.pending is None
    assert len(state.processed_controls) == 1
    if kind == "cancel":
        assert Harness(store).run(2).steps_used == 1


def test_retry_failed_evaluation_preserves_error_and_successful_sibling(tmp_path, monkeypatch):
    store = create(tmp_path)
    state = Harness(store).run(2)
    original = deepcopy(state.candidates["c2"])
    state.candidates["c1"]["evaluation_status"] = "evaluation_error"
    state.candidates["c1"]["dock"] = {"error": "timeout"}
    store.save(state)
    import agents.evaluator
    def evaluate(candidates, *args, **kwargs):
        assert [c["candidate_id"] for c in candidates] == ["c1"]
        return [{**candidates[0], "evaluation_status": "screening_only", "property_score": 0.5}]
    monkeypatch.setattr(agents.evaluator, "evaluate_candidates", evaluate)
    result = Harness(store, Policy([action("retry_evaluation", candidate_ids=["c1"])])).run(1)
    assert result.candidates["c1"]["evaluation_attempts"][0]["dock"]["error"] == "timeout"
    assert result.candidates["c2"] == original
    assert result.evaluations_used == 4


def test_failed_evidence_cannot_finish(tmp_path):
    store = create(tmp_path)
    state = Harness(store).run(1)
    state.candidates["c1"]["evaluation_status"] = "evaluation_error"
    store.save(state)
    state = Harness(store, Policy([action("finish", candidate_ids=["c1"], summary="Done")])).run(1)
    assert state.final is None
    assert "successful evaluation" in state.events[-1]["error"]


def test_protocol_change_pauses_before_deciding(tmp_path):
    store = create(tmp_path)
    state = Harness(store).run(1)
    state.config["scoring"]["objective"]["version"] = "changed"
    store.save(state)
    result = Harness(store, Policy([])).run(1)
    assert result.reason == "evaluation_protocol_changed"
    assert result.steps_used == 1


def test_display_failure_does_not_repeat_tool(tmp_path):
    store = create(tmp_path)
    def broken_display(event):
        raise RuntimeError("display disconnected")
    state = Harness(store, on_event=broken_display).run(2)
    assert len(state.candidates) == 3
    assert state.evaluations_used == 3
    assert not any(e["type"] == "error" for e in state.events)


def test_idle_cancel_command_is_immediate(tmp_path, capsys):
    from agent_task import main
    store = create(tmp_path)
    assert main(["cancel", "--task-dir", str(store.directory)]) == 0
    assert store.load().status == "cancelled"
    assert "applied" in capsys.readouterr().out


def test_llm_policy_multistep_refine_and_history(tmp_path, monkeypatch):
    """Exercise the real planning adapter with offline model responses and real tools."""
    import agents.llm
    import agents.generator
    from agents.harness.runtime import LLMPolicy
    store = create(tmp_path)
    seen = []
    actions = iter([
        action("generate", count=1, focus="Explore"),
        action("evaluate", candidate_ids=["c1"]),
        action("refine", parent_id="c1", count=1, focus="Local change"),
        action("evaluate", candidate_ids=["c2"]),
        action("history", limit=4),
        action("compare", candidate_ids=["c1", "c2"]),
        action("finish", candidate_ids=["c2"], summary="Property screening only"),
    ])
    class Client:
        def chat_json(self, system, user):
            context = json.loads(user)
            seen.append(context)
            if len(seen) == 3:
                assert context["candidates"][0]["evaluation_status"] == "screening_only"
            return next(actions)
    monkeypatch.setattr(agents.llm, "get_client", lambda *a, **k: Client())
    molecules = iter(["CCO", "CCCO"])
    monkeypatch.setattr(agents.generator, "generate_with_provider", lambda *a, **k: {
        "smiles_list": [next(molecules)], "model": "offline", "rationale": "test"})
    state = Harness(store, LLMPolicy()).run(7)
    assert state.status == "completed"
    assert state.candidates["c2"]["parent_id"] == "c1"
    assert state.model_calls_used == 9
    assert state.evaluations_used == 2
    assert len(seen) == 7


def test_seeded_parent_verified_refine_compare_and_finish(tmp_path, monkeypatch):
    """The core paper workflow keeps a parent-child audit trail and enforces completion."""
    store = create(tmp_path)
    state = store.load()
    state.constraints = normalize_constraints({
        "require_verified_refinement": True,
        "preserve_scaffold": True,
        "max_changed_atoms": 4,
        "min_similarity": 0.3,
    })
    add_seed_candidates(state, ["CCOc1ccccc1"], source="test")
    store.save(state)
    import agents.generator
    monkeypatch.setattr(agents.generator, "generate_with_provider", lambda *a, **k: {
        "smiles_list": ["CCCOc1ccccc1"], "model": "offline", "rationale": "extend side chain"})
    policy = Policy([
        action("evaluate", candidate_ids=["c1"]),
        action("refine", parent_id="c1", count=1, focus="extend the ethyl side chain"),
        action("evaluate", candidate_ids=["c2"]),
        action("compare_parent_child", candidate_ids=["c2"]),
        action("finish", candidate_ids=["c2"], summary="Verified child selected"),
    ])
    result = Harness(store, policy).run(5)
    assert result.status == "completed"
    assert result.candidates["c2"]["modification_verified"] is True
    verification = result.candidates["c2"]["refinement_verification"]
    assert verification["semantic_change_verified"] is False
    comparison = next(e["result"] for e in result.events
                      if e.get("action", {}).get("tool") == "compare_parent_child")
    assert comparison["comparisons"][0]["parent_id"] == "c1"
    assert result.final["outcome"] == "goal_met"


def test_unverified_refinement_cannot_be_evaluated(tmp_path, monkeypatch):
    store = create(tmp_path)
    state = store.load()
    state.constraints = normalize_constraints({"require_verified_refinement": True,
                                               "preserve_scaffold": True})
    add_seed_candidates(state, ["c1ccccc1"], source="test")
    store.save(state)
    import agents.generator
    monkeypatch.setattr(agents.generator, "generate_with_provider", lambda *a, **k: {
        "smiles_list": ["CCN"], "model": "offline", "rationale": "unrelated"})
    result = Harness(store, Policy([
        action("refine", parent_id="c1", count=1, focus="local edit"),
        action("evaluate", candidate_ids=["c2"]),
    ])).run(2)
    assert result.candidates["c2"]["modification_verified"] is False
    assert "Unverified refinements" in result.events[-1]["error"]
    assert "evaluation_status" not in result.candidates["c2"]


def test_refinement_can_be_limited_to_specific_parent_atom_indices():
    allowed = normalize_constraints({"allowed_parent_atom_indices": [0], "preserve_scaffold": False})
    blocked = normalize_constraints({"allowed_parent_atom_indices": [2], "preserve_scaffold": False})
    assert verify_refinement("CCO", "CCCO", allowed)["passed"] is True
    result = verify_refinement("CCO", "CCCO", blocked)
    assert result["passed"] is False
    assert result["changed_parent_atom_indices"] == [0]
    assert "allowed_edit_site" in result["failures"]


def test_finish_pauses_when_structured_goal_is_unmet(tmp_path):
    store = create(tmp_path)
    state = Harness(store).run(2)
    state.constraints = normalize_constraints({"min_property_score": 1.0})
    store.save(state)
    candidate_id = next(iter(state.candidates))
    result = Harness(store, Policy([
        action("finish", candidate_ids=[candidate_id], summary="No candidate reached the threshold")
    ])).run(1)
    assert result.status == "paused"
    assert result.reason == "goal_not_met"
    assert result.final["outcome"] == "goal_not_met"
    assert "min_property_score" in result.final["goal_assessments"][candidate_id]["unmet"]


def test_all_deterministic_edit_operations_and_atom_depiction():
    parent = "CCOc1ccccc1"
    assert molecule_atom_table(parent)[2]["element"] == "O"
    cases = [
        ("attach_fragment", {"atom_index": 1, "fragment_smiles": "N", "fragment_atom_index": 0}),
        ("replace_substituent", {"atom_index": 2, "neighbor_atom_index": 1,
                                 "fragment_smiles": "N", "fragment_atom_index": 0}),
        ("remove_terminal_group", {"atom_index": 2, "neighbor_atom_index": 1}),
        ("replace_bioisostere", {"atom_index": 2, "neighbor_atom_index": 1,
                                  "fragment_smiles": "N", "fragment_atom_index": 0}),
        ("change_bond_order", {"atom_index": 0, "neighbor_atom_index": 1, "bond_order": "DOUBLE"}),
    ]
    products = []
    for operation, arguments in cases:
        child, record = apply_edit(parent, operation, **arguments)
        assert child != parent
        assert record["operation"] == operation
        products.append(child)
    assert len(set(products)) == 4  # substituent and bioisostere operations intentionally share graph mechanics.
    assert molecule_svg_data_url(parent, [2]).startswith("data:image/svg+xml;base64,")


def test_hypothesis_is_required_and_assessed_after_deterministic_edit(tmp_path):
    store = create(tmp_path)
    state = store.load()
    state.constraints = normalize_constraints({"require_verified_refinement": True,
                                               "allow_freeform_refine": False,
                                               "min_similarity": 0.1})
    add_seed_candidates(state, ["CCO"], source="test")
    store.save(state)
    hypothesis = action(
        "record_hypothesis", hypothesis_id="h1", parent_id="c1",
        rationale="Adding an amine may improve the property proxy",
        expected_metric="property_score", expected_direction="increase",
        allowed_tradeoff="A small hERG-risk increase is acceptable",
        next_if_supported="Evaluate a second small polar fragment",
        next_if_not_supported="Rollback and choose a different attachment site",
    )
    edit = action("attach_fragment", parent_id="c1", atom_index=1,
                  fragment_smiles="N", fragment_atom_index=0, hypothesis_id="h1")
    result = Harness(store, Policy([
        action("evaluate", candidate_ids=["c1"]), hypothesis, edit,
        action("evaluate", candidate_ids=["c2"]),
        action("compare_parent_child", candidate_ids=["c2"]),
    ])).run(5)
    assert result.candidates["c2"]["candidate_role"] == "deterministic_edit"
    assert result.candidates["c2"]["modification_verified"] is True
    assert result.hypotheses["h1"]["status"] == "assessed"
    assert result.hypotheses["h1"]["outcome"] == "tradeoff_exceeded"
    assert "herg_risk" in result.hypotheses["h1"]["violations"]
    assert result.hypotheses["h1"]["next_action"]


def test_deterministic_edit_rejects_missing_or_reused_hypothesis(tmp_path):
    store = create(tmp_path)
    state = store.load()
    add_seed_candidates(state, ["CCO"], source="test")
    state.candidates["c1"]["evaluation_status"] = "screening_only"
    store.save(state)
    edit = action("attach_fragment", parent_id="c1", atom_index=1,
                  fragment_smiles="N", fragment_atom_index=0, hypothesis_id="missing")
    first = Harness(store, Policy([edit])).run(1)
    assert "Record the hypothesis" in first.events[-1]["error"]


def test_rejected_deterministic_edit_marks_hypothesis_for_strategy_change(tmp_path):
    store = create(tmp_path)
    state = store.load()
    add_seed_candidates(state, ["CCOc1ccccc1"], source="test")
    state.candidates["c1"]["evaluation_status"] = "screening_only"
    store.save(state)
    hypothesis = action(
        "record_hypothesis", hypothesis_id="h_bad", parent_id="c1", rationale="Test wrong branch direction",
        expected_metric="property_score", expected_direction="increase", allowed_tradeoff="none",
        next_if_supported="continue", next_if_not_supported="reverse the core and branch indices")
    bad_edit = action("replace_substituent", parent_id="c1", atom_index=0, neighbor_atom_index=1,
                      fragment_smiles="CC", fragment_atom_index=0, hypothesis_id="h_bad")
    result = Harness(store, Policy([hypothesis, bad_edit])).run(2)
    assert result.candidates["c2"]["modification_verified"] is False
    assert result.hypotheses["h_bad"]["outcome"] == "edit_rejected"
    assert result.hypotheses["h_bad"]["next_action"] == "reverse the core and branch indices"


def test_unsafe_tool_is_not_automatically_retried(tmp_path):
    store = create(tmp_path)
    registry = ToolRegistry()
    calls = []
    def unsafe(*args):
        calls.append(1)
        raise TimeoutError("unknown external outcome")
    registry.register(Tool("unsafe", "test", {}, unsafe))
    state = Harness(store, Policy([action("unsafe")]), registry, sleep=lambda _: None).run(1)
    assert len(calls) == 1
    assert not any(e["type"] == "retry" for e in state.events)


def test_cancel_does_not_get_undone_by_pending_receipt(tmp_path, monkeypatch):
    store = create(tmp_path)
    save = store.save
    def fail_commit(state):
        if any(e["type"] == "tool_result" for e in state.events):
            raise OSError("disk full")
        save(state)
    monkeypatch.setattr(store, "save", fail_commit)
    with pytest.raises(OSError):
        Harness(store).run(1)
    fresh = CheckpointStore(store.directory)
    fresh.submit_control("cancel")
    Harness(fresh).apply_controls()
    assert Harness(fresh).run(1).status == "cancelled"


def test_retry_limit_stops_transient_loop(tmp_path):
    store = create(tmp_path)
    class Down:
        calls = 0
        def decide(self, state, registry):
            self.calls += 1
            raise ConnectionError("offline")
    policy = Down()
    state = Harness(store, policy, sleep=lambda _: None).run(1)
    assert policy.calls == 3
    assert state.model_calls_used == 3
    assert state.events[-1]["type"] == "error"


def test_no_progress_detects_alternating_actions(tmp_path):
    store = create(tmp_path)
    registry = ToolRegistry()
    for name in ("one", "two"):
        registry.register(Tool(name, "test", {}, lambda *a: {}))
    policy = Policy([action("one"), action("two")] * 5)
    state = Harness(store, policy, registry).run(10)
    assert state.reason == "no_progress"
    assert state.steps_used == 8


def test_cancel_unknown_interrupted_action_without_acknowledgement(tmp_path):
    store = create(tmp_path)
    registry = ToolRegistry()
    def interrupted(*args):
        raise KeyboardInterrupt
    registry.register(Tool("interrupt", "test", {}, interrupted))
    Harness(store, Policy([action("interrupt")]), registry).run(1)
    store.submit_control("cancel")
    assert Harness(store).run(1).status == "cancelled"
