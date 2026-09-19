import json

import pytest

from agents.harness.state import TaskState
from agents.harness.state import CheckpointStore
from agents.harness import Harness
from db.repository import SQLiteRepository


def test_repository_round_trip_and_idempotent_facts(tmp_path):
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    state = TaskState(goal="screen", config={}, task_id="task-1")
    state.candidates["c1"] = {
        "smiles": "CCO",
        "evaluation_status": "screening_only",
        "protocol_id": "p1",
    }
    state.events.append({"type": "tool_result", "call_id": "call-1", "result": {"ok": True}})
    repo.save_state(state)
    repo.save_state(state)
    assert repo.load_state("task-1") == state
    assert repo.count("runs", "task-1") == 1
    assert repo.count("candidates", "task-1") == 1
    assert repo.count("agent_events", "task-1") == 1
    assert repo.count("evaluations", "task-1") == 1


def test_repository_rejects_corrupted_snapshot(tmp_path):
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    repo.save_state(TaskState(goal="screen", config={}, task_id="task-2"))
    repo.connection.execute(
        "UPDATE runs SET state_json=? WHERE task_id=?",
        (json.dumps({"bad": True}), "task-2"),
    )
    repo.connection.commit()
    with pytest.raises(ValueError, match="hash mismatch"):
        repo.load_state("task-2")


def test_repository_rejects_changed_artifact(tmp_path):
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    repo.save_state(TaskState(goal="screen", config={}, task_id="task-3"))
    artifact = tmp_path / "summary.json"
    artifact.write_text('{"ok": true}', encoding="utf-8")
    repo.record_artifact("task-3", "summary", artifact)
    assert repo.verify_artifact("task-3", "summary")
    artifact.write_text('{"ok": false}', encoding="utf-8")
    assert not repo.verify_artifact("task-3", "summary")


def test_checkpoint_store_can_dual_write_and_restore(tmp_path):
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    store = CheckpointStore(tmp_path / "task", repository=repo)
    state = TaskState(goal="screen", config={}, task_id="task-4")
    store.save(state)
    restored = store.load_from_repository()
    assert restored == state
    assert repo.count("rounds", "task-4") == 1


def test_repository_refuses_evaluation_from_different_protocol(tmp_path):
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    state = TaskState(goal="screen", config={}, task_id="task-5", protocol_id="protocol-a")
    state.candidates["c1"] = {
        "smiles": "CCO", "evaluation_status": "complete", "protocol_id": "protocol-a",
        "property_score": 0.5,
    }
    repo.save_state(state)
    assert repo.get_evaluation("task-5", "c1", "protocol-a")["property_score"] == 0.5
    assert repo.get_evaluation("task-5", "c1", "protocol-b") is None


def test_current_evaluation_view_recovers_after_error_and_preserves_success(tmp_path):
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    state = TaskState(goal="screen", config={}, task_id="task-8")
    state.candidates["c1"] = {"candidate_id": "c1", "smiles": "CCO",
                                "protocol_id": "protocol-a",
                                "evaluation_status": "evaluation_error"}
    repo.save_state(state)
    repo.record_evaluation_attempt("task-8", "c1", "protocol-a", "evaluation_error", {"attempt": 1})
    assert repo.get_evaluation("task-8", "c1", "protocol-a") is None
    success = {"candidate_id": "c1", "evaluation_status": "complete", "protocol_id": "protocol-a", "value": 1}
    state.candidates["c1"] = {"candidate_id": "c1", "smiles": "CCO", **success}
    repo.save_state(state)
    repo.record_evaluation_attempt("task-8", "c1", "protocol-a", "complete", success)
    assert repo.get_evaluation("task-8", "c1", "protocol-a")["value"] == 1
    state.candidates["c1"] = {"candidate_id": "c1", "smiles": "CCO",
                                "protocol_id": "protocol-a",
                                "evaluation_status": "evaluation_error"}
    repo.save_state(state)
    repo.record_evaluation_attempt("task-8", "c1", "protocol-a", "evaluation_error", {"attempt": 3})
    assert repo.get_evaluation("task-8", "c1", "protocol-a")["value"] == 1
    history = repo.evaluation_history("task-8", "c1", "protocol-a")
    assert [row["status"] for row in history] == ["evaluation_error", "complete", "evaluation_error"]
    assert repo.count("evaluation_attempts", "task-8") == 3
    repo.close()


def test_protocol_change_re_evaluates_and_current_protocol_reuses(tmp_path, monkeypatch):
    import yaml
    from pathlib import Path
    import agents.evaluator

    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    store = CheckpointStore(tmp_path / "task", repository=repo)
    state = TaskState(goal="screen", config=config, task_id="task-7", mock=True)
    state.candidates["c1"] = {"candidate_id": "c1", "smiles": "CCO"}
    store.save(state)
    from tools.provenance import digest, evaluation_protocol

    def fake(candidates, scoring, target, *, dock_enabled, **kwargs):
        protocol = digest(evaluation_protocol(target, scoring, dock_enabled))
        return [{**c, "evaluation_status": "complete", "protocol_id": protocol, "property_score": 0.5} for c in candidates]
    monkeypatch.setattr(agents.evaluator, "evaluate_candidates", fake)
    policy = type("P", (), {"decide": lambda self, state, registry: {
        "tool": "evaluate", "arguments": {"candidate_ids": ["c1"]}, "reason": "protocol a"
    }})()
    Harness(store, policy=policy).run(1)
    first = store.load()
    first.config["scoring"]["objective"]["precision"] = 5
    first.protocol_id = None
    store.save(first)
    calls = []
    def changed(candidates, scoring, target, *, dock_enabled, **kwargs):
        calls.append(1)
        protocol = digest(evaluation_protocol(target, scoring, dock_enabled))
        return [{**candidates[0], "evaluation_status": "complete", "protocol_id": protocol, "property_score": 0.6}]
    monkeypatch.setattr(agents.evaluator, "evaluate_candidates", changed)
    Harness(store, policy=type("P", (), {"decide": lambda self, state, registry: {
        "tool": "evaluate", "arguments": {"candidate_ids": ["c1"]}, "reason": "protocol b"
    }})()).run(1)
    assert calls == [1]
    before = len(calls)
    Harness(store, policy=type("P", (), {"decide": lambda self, state, registry: {
        "tool": "evaluate", "arguments": {"candidate_ids": ["c1"]}, "reason": "reuse b"
    }})()).run(1)
    assert len(calls) == before
    assert len(repo.evaluation_history("task-7", "c1")) >= 2


def test_harness_replaces_candidate_evidence_from_old_protocol(tmp_path, monkeypatch):
    import yaml
    from pathlib import Path
    from agents.harness.tools import default_registry

    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    store = CheckpointStore(tmp_path / "task", repository=repo)
    state = TaskState(goal="screen", config=config, task_id="task-6", protocol_id="new")
    state.candidates["c1"] = {"candidate_id": "c1", "smiles": "CCO", "evaluation_status": "complete", "protocol_id": "old"}
    store.save(state)
    monkeypatch.setattr("agents.evaluator.evaluate_candidates", lambda candidates, *args, **kwargs: [
        {**candidates[0], "evaluation_status": "complete", "protocol_id": "new", "property_score": 0.6}
    ])
    default_registry().execute(state, {
        "tool": "evaluate", "arguments": {"candidate_ids": ["c1"]}, "reason": "recheck"
    }, tmp_path)
    assert state.candidates["c1"]["property_score"] == 0.6


def test_harness_writes_tool_events_candidates_evaluations_and_rounds(tmp_path):
    import yaml
    from pathlib import Path

    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))

    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    store = CheckpointStore(tmp_path / "task", repository=repo)
    store.save(TaskState(goal="Find EGFR candidates", config=config, mock=True))
    state = Harness(store).run(2)
    assert state.steps_used == 2
    assert repo.count("agent_events", state.task_id) >= 2
    assert repo.count("candidates", state.task_id) == 3
    assert repo.count("evaluations", state.task_id) == 3
    assert repo.count("rounds", state.task_id) >= 1
