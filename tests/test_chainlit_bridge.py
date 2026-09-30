"""Tests for the Chainlit bridge. We exercise only the UI-agnostic surface —
the actual Chainlit socket layer is verified by an integration test that is
out of scope for this module.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.chainlit_bridge import (
    ChainlitBridge,
    _event_to_text,
    list_tasks,
    load_task_state,
)


def _create_task(root: Path, *, goal: str = "Test goal", mock: bool = True) -> Path:
    """Treat ``root`` as the task directory itself (not its parent).

    Earlier versions created ``root / task_alpha / task.json`` which
    did not match ``list_tasks``'s single-level discovery rule. After
    ``5f08f4c`` the helper now writes the task.json straight into
    ``root``.
    """
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    store = CheckpointStore(root)
    store.save(TaskState(goal=goal, config=config, mock=mock, dock_enabled=False, max_steps=4))
    return root


def test_list_tasks_discovers_checkpoints(tmp_path: Path) -> None:
    _create_task(tmp_path)
    rows = list_tasks(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "paused"
    assert row["status_label"] == "已暂停"
    assert row["goal"] == "Test goal"
    assert row["mock"] is True


def test_list_tasks_sorts_newest_first(tmp_path: Path) -> None:
    first = _create_task(tmp_path / "a", goal="older")
    second = _create_task(tmp_path / "b", goal="newer")
    # Force the second one to look newer by bumping mtime.
    import os, time
    os.utime(second, (time.time() + 60, time.time() + 60))
    rows = list_tasks(tmp_path)
    assert [r["goal"] for r in rows] == ["newer", "older"]


def test_list_tasks_returns_empty_when_root_missing(tmp_path: Path) -> None:
    assert list_tasks(tmp_path / "missing") == []


def test_load_task_state_returns_task_state(tmp_path: Path) -> None:
    task_dir = _create_task(tmp_path)
    state = load_task_state(task_dir)
    assert isinstance(state, TaskState)
    assert state.goal == "Test goal"


def test_bridge_snapshot_before_attach(tmp_path: Path) -> None:
    task_dir = _create_task(tmp_path)
    bridge = ChainlitBridge(store=CheckpointStore(task_dir))
    view = bridge.snapshot()
    assert view["status"] == "paused"
    assert view["status_label"] == "已暂停"


def test_bridge_attach_binds_on_event(tmp_path: Path) -> None:
    task_dir = _create_task(tmp_path)
    store = CheckpointStore(task_dir)
    bridge = ChainlitBridge(store=store)
    harness = Harness(store)
    bridge.attach(harness)
    assert harness.on_event is not None
    # Sync emit outside an event loop must not raise (it logs a debug message).
    harness.on_event({"type": "decision", "action": {"tool": "evaluate"}})


def test_bridge_records_callbacks_when_event_loops(tmp_path: Path) -> None:
    import asyncio

    task_dir = _create_task(tmp_path)
    store = CheckpointStore(task_dir)
    state = store.load()
    seen_steps: list[dict] = []
    seen_messages: list[str] = []

    async def main() -> None:
        bridge = ChainlitBridge(
            store=store,
            on_step=lambda e: seen_steps.append(e),
            on_message=lambda m: seen_messages.append(m),
        )
        bridge.task_state = state
        harness = Harness(store)
        bridge.attach(harness)
        # Drive a couple of events through the harness directly.
        await bridge._on_harness_event({"type": "action", "action": {"tool": "evaluate",
                                                                       "arguments": {"candidate_ids": ["c1"]},
                                                                       "reason": "test"}})
        await bridge._on_harness_event({"type": "tool_result", "action": {"tool": "evaluate"},
                                        "result": {"added_ids": ["c1"]}})

    asyncio.run(main())
    assert len(seen_steps) == 2
    # _event_to_text uses the Chinese TOOLS labels; "evaluate" maps to
    # "评估候选". Accept either spelling.
    assert any("evaluate" in m or "评估候选" in m for m in seen_messages)


def test_event_to_text_for_instruction(tmp_path: Path) -> None:
    text = _event_to_text({"type": "instruction", "text": "Try a morpholine"})
    assert text == "你的新要求 · Try a morpholine"


def test_event_to_text_for_unknown_event_returns_none() -> None:
    # An event without a recognised ``type`` should not raise, and should
    # still return ``None`` (the bridge uses ``None`` to skip the message).
    assert _event_to_text({"type": "no_such_kind"}) is None


def test_bridge_candidate_detail_raises_for_missing_id(tmp_path: Path) -> None:
    task_dir = _create_task(tmp_path)
    bridge = ChainlitBridge(store=CheckpointStore(task_dir))
    bridge.task_state = bridge.store.load()
    with pytest.raises(ValueError):
        bridge.candidate_detail("does-not-exist")
