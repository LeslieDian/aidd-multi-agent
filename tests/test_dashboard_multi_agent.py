"""tests/test_dashboard_multi_agent.py - Tests for dashboard multi-agent integration.

Covers:
- _is_multi_agent(config) helper
- _read_multi_agent_log(task_dir) helper
- Dashboard.create() accepts multi_agent flag and writes flag file
- Dashboard.create() rejects multi_agent=true when config has it disabled
- snapshot() attaches multi_agent_log.json when present
- launch() picks the multi-agent worker command when flag file present
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest
import yaml

from agents.harness.dashboard import (
    Dashboard,
    _is_multi_agent,
    _read_multi_agent_log,
    make_server,
)


@pytest.fixture
def cfg_with_ma():
    """config.yaml-like dict with multi_agent enabled (2 generators)."""
    return {
        "target": {
            "name": "EGFR",
            "receptor_pdbqt": "data/prepared/1M17_v1.pdbqt",
        },
        "loop": {
            "multi_agent": {
                "enabled": True,
                "generators": [
                    {"name": "A1_qed", "provider": "MiniMax",
                     "prompt_role": "qed"},
                    {"name": "A2_synth", "provider": "qwen_aliyun",
                     "prompt_role": "synth"},
                ],
                "judges": [],
                "router": {"enabled": False},
                "debate": {"enabled": False},
            },
        },
        "llm": {"providers": {"MiniMax": {}, "qwen_aliyun": {}}},
    }


@pytest.fixture
def cfg_without_ma():
    return {
        "target": {
            "name": "EGFR",
            "receptor_pdbqt": "data/prepared/1M17_v1.pdbqt",
        },
        "loop": {"multi_agent": {"enabled": False, "generators": []}},
    }


# ============================================================
# _is_multi_agent
# ============================================================

class TestIsMultiAgent:
    def test_enabled(self, cfg_with_ma):
        assert _is_multi_agent(cfg_with_ma) is True

    def test_disabled(self, cfg_without_ma):
        assert _is_multi_agent(cfg_without_ma) is False

    def test_missing_block(self):
        assert _is_multi_agent({}) is False

    def test_missing_loop(self):
        assert _is_multi_agent({"target": {}}) is False


# ============================================================
# _read_multi_agent_log
# ============================================================

class TestReadMultiAgentLog:
    def test_no_file(self, tmp_path):
        assert _read_multi_agent_log(tmp_path) is None

    def test_valid_file(self, tmp_path):
        payload = {"runs": [{"r": 0}], "last_updated": 1.0}
        (tmp_path / "multi_agent_log.json").write_text(
            json.dumps(payload), encoding="utf-8",
        )
        assert _read_multi_agent_log(tmp_path) == payload

    def test_invalid_json(self, tmp_path):
        (tmp_path / "multi_agent_log.json").write_text(
            "not json", encoding="utf-8",
        )
        assert _read_multi_agent_log(tmp_path) is None


# ============================================================
# Dashboard.create() multi_agent handling
# ============================================================

class TestDashboardCreate:
    def _make_dashboard(self, tmp_path, cfg_content):
        # Write a tiny config.yaml.
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg_content), encoding="utf-8")
        return Dashboard(tmp_path / "runs", cfg_path)

    def _patch_yaml_load(self, monkeypatch, cfg_content):
        def _fake_yaml_load(path, *args, **kwargs):
            return yaml.safe_load(yaml.safe_dump(cfg_content))
        monkeypatch.setattr("yaml.safe_load", _fake_yaml_load)

    def test_rejects_multi_agent_when_config_disabled(self, tmp_path, monkeypatch,
                                                    cfg_without_ma):
        dash = self._make_dashboard(tmp_path, cfg_without_ma)
        monkeypatch.setattr("yaml.safe_load",
                            lambda *a, **k: cfg_without_ma)
        with pytest.raises(ValueError, match="multi_agent"):
            dash.create({
                "goal": "test",
                "mock": True,
                "dock": False,
                "multi_agent": True,
            })

    def test_accepts_multi_agent_when_config_enabled(self, tmp_path, monkeypatch,
                                                   cfg_with_ma):
        dash = self._make_dashboard(tmp_path, cfg_with_ma)
        # Patch yaml.safe_load to return our test config.
        monkeypatch.setattr("yaml.safe_load",
                            lambda *a, **k: cfg_with_ma)
        # Replace launch() with a no-op so we don't actually start subprocess.
        monkeypatch.setattr(Dashboard, "launch", lambda self, task, steps: {"status": "noop"})
        result = dash.create({
            "goal": "test",
            "mock": True,
            "dock": False,
            "multi_agent": True,
        })
        assert "name" in result
        task_dir = tmp_path / "runs" / result["name"]
        flag = task_dir / "multi_agent.flag"
        assert flag.is_file()
        assert flag.read_text(encoding="utf-8").strip() == "enabled"

    def test_no_flag_when_multi_agent_false(self, tmp_path, monkeypatch,
                                           cfg_with_ma):
        dash = self._make_dashboard(tmp_path, cfg_with_ma)
        monkeypatch.setattr("yaml.safe_load",
                            lambda *a, **k: cfg_with_ma)
        monkeypatch.setattr(Dashboard, "launch", lambda self, task, steps: {"status": "noop"})
        result = dash.create({
            "goal": "test",
            "mock": True,
            "dock": False,
            "multi_agent": False,
        })
        task_dir = tmp_path / "runs" / result["name"]
        flag = task_dir / "multi_agent.flag"
        assert not flag.is_file()


# ============================================================
# snapshot() attaches multi_agent log
# ============================================================

class TestSnapshotAttachesLog:
    def test_snapshot_includes_multi_agent(self, tmp_path, monkeypatch,
                                          cfg_with_ma):
        dash = Dashboard(tmp_path / "runs", tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(cfg_with_ma), encoding="utf-8",
        )
        # Create a fake task with a fake multi_agent_log.json.
        from agents.harness.state import CheckpointStore, TaskState
        from agents.harness.molecule_ops import normalize_constraints
        state = TaskState(
            goal="test", config=cfg_with_ma, mock=True, dock_enabled=False,
            max_steps=10, max_model_calls=20, max_evaluations=100,
            constraints=normalize_constraints({}, dock_enabled=False),
        )
        store = CheckpointStore(tmp_path / "runs" / "task_test")
        store.save(state)
        (tmp_path / "runs" / "task_test" / "multi_agent_log.json").write_text(
            json.dumps({"runs": [{"round": 0}], "last_updated": 1.0}),
            encoding="utf-8",
        )
        view = dash.snapshot("task_test")
        assert "multi_agent" in view
        assert view["multi_agent"]["runs"][0]["round"] == 0


# ============================================================
# launch() picks multi-agent worker command
# ============================================================

class TestLaunchWorker:
    def test_launch_uses_multi_agent_command(self, tmp_path, monkeypatch,
                                            cfg_with_ma):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg_with_ma), encoding="utf-8")
        dash = Dashboard(tmp_path / "runs", cfg_path)

        from agents.harness.state import CheckpointStore, TaskState
        from agents.harness.molecule_ops import normalize_constraints
        state = TaskState(
            goal="test", config=cfg_with_ma, mock=True, dock_enabled=False,
            max_steps=10, max_model_calls=20, max_evaluations=100,
            constraints=normalize_constraints({}, dock_enabled=False),
        )
        store = CheckpointStore(tmp_path / "runs" / "task_test")
        store.save(state)
        (tmp_path / "runs" / "task_test" / "multi_agent.flag").write_text(
            "enabled\n", encoding="utf-8",
        )

        # Capture subprocess.Popen calls.
        captured = {}
        class FakePopen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                captured["kw"] = kw

            def poll(self):
                return 0

        monkeypatch.setattr("subprocess.Popen", FakePopen)
        dash.launch("task_test", steps=2)

        # The multi-agent worker should be scripts/run_multi_agent_for_dashboard.py
        assert any("run_multi_agent_for_dashboard" in str(c) for c in captured["cmd"])

    def test_launch_uses_legacy_command_when_no_flag(self, tmp_path, monkeypatch,
                                                   cfg_with_ma):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg_with_ma), encoding="utf-8")
        dash = Dashboard(tmp_path / "runs", cfg_path)

        from agents.harness.state import CheckpointStore, TaskState
        from agents.harness.molecule_ops import normalize_constraints
        state = TaskState(
            goal="test", config=cfg_with_ma, mock=True, dock_enabled=False,
            max_steps=10, max_model_calls=20, max_evaluations=100,
            constraints=normalize_constraints({}, dock_enabled=False),
        )
        store = CheckpointStore(tmp_path / "runs" / "task_test")
        store.save(state)

        captured = {}
        class FakePopen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd

            def poll(self):
                return 0

        monkeypatch.setattr("subprocess.Popen", FakePopen)
        dash.launch("task_test", steps=2)

        # Legacy: agent_task.py resume
        assert any("agent_task.py" in str(c) for c in captured["cmd"])
        assert "resume" in captured["cmd"]