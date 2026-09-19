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
    with pytest.raises(ValueError, match="protocol changed"):
        repo.get_evaluation("task-5", "c1", "protocol-b")


def test_harness_refuses_candidate_evidence_from_old_protocol(tmp_path):
    import yaml
    from pathlib import Path
    from agents.harness.tools import default_registry

    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    repo = SQLiteRepository(tmp_path / "aidd.sqlite3")
    store = CheckpointStore(tmp_path / "task", repository=repo)
    state = TaskState(goal="screen", config=config, task_id="task-6", protocol_id="new")
    state.candidates["c1"] = {"smiles": "CCO", "evaluation_status": "complete", "protocol_id": "old"}
    store.save(state)
    with pytest.raises(ValueError, match="protocol changed"):
        default_registry().execute(state, {
            "tool": "evaluate", "arguments": {"candidate_ids": ["c1"]}, "reason": "recheck"
        }, tmp_path)


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
