"""Dashboard presentation, task controls, HTTP boundaries and process invocation."""
import http.client
import json
from pathlib import Path
import re
import socket
import threading

import pytest
import yaml

from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.dashboard import Dashboard, make_server, ROOT
from agents.harness.presentation import task_view

ORIGINAL_CONNECT = socket.socket.connect


def setup_task(tmp_path):
    dashboard = Dashboard(tmp_path / "runs", ROOT / "config.yaml")
    store = CheckpointStore(dashboard.root / "demo")
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    store.save(TaskState(goal="离线验收", config=config, mock=True))
    return dashboard, store


def test_projection_uses_real_evidence_without_config(tmp_path):
    dashboard, store = setup_task(tmp_path)
    Harness(store).run(2)
    view = dashboard.snapshot("demo")
    assert view["status_label"] == "已暂停"
    assert len(view["candidates"]) == 3
    assert view["candidates"][0]["status"] == "性质初筛"
    assert view["candidates"][0]["vina"] is None
    assert view["events"][0]["title"] == "探索候选"
    assert "api_key_env" not in json.dumps(view)
    assert "config" not in view
    assert view["can_resume"]


def test_candidate_detail_contains_indexed_svg_without_provider_config(tmp_path):
    dashboard, store = setup_task(tmp_path)
    Harness(store).run(2)
    detail = dashboard.candidate_detail("demo", "c1")
    assert detail["image"].startswith("data:image/svg+xml;base64,")
    assert detail["smiles"]
    assert "hypothesis" in detail
    assert "config" not in detail


def test_dashboard_steer_resume_cancel(tmp_path):
    dashboard, store = setup_task(tmp_path)
    state = Harness(store).run(2)
    original = state.candidates
    assert dashboard.control("demo", "steer", "先比较已有结果")["status"] == "applied"
    assert dashboard.snapshot("demo")["revision"] == 1
    state = Harness(store).run(1)
    assert state.candidates == original
    dashboard.control("demo", "cancel")
    assert dashboard.snapshot("demo")["status"] == "cancelled"
    with pytest.raises(ValueError):
        dashboard.launch("demo")


def test_running_worker_queues_controls(tmp_path):
    dashboard, store = setup_task(tmp_path)
    with store.lock():
        assert dashboard.control("demo", "pause")["status"] == "queued"
        view = dashboard.snapshot("demo")
        assert view["active"]
        assert view["queued_controls"][0]["kind"] == "pause"
    Harness(store).apply_controls()
    assert dashboard.snapshot("demo")["queued_controls"] == []


def test_structured_constraint_control_is_versioned_and_changes_future_protocol(tmp_path):
    dashboard, store = setup_task(tmp_path)
    state = store.load()
    state.dock_enabled = True
    state.protocol_id = "old-protocol"
    store.save(state)
    result = dashboard.control("demo", "constraints", constraints={
        "docking_allowed": False, "require_safety_gate": True, "max_changed_atoms": 5})
    assert result["status"] == "applied"
    updated = store.load()
    assert not updated.dock_enabled
    assert updated.protocol_id is None
    assert updated.revision == 1
    assert updated.constraint_history[-1]["changes"]["max_changed_atoms"] == 5
    assert dashboard.snapshot("demo")["constraints"]["require_safety_gate"] is True


def test_launch_uses_same_cli_no_shell_and_rejects_duplicate(tmp_path, monkeypatch):
    dashboard, store = setup_task(tmp_path)
    calls = []
    class Process:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
        def poll(self):
            return None
    monkeypatch.setattr("agents.harness.dashboard.subprocess.Popen", Process)
    dashboard.launch("demo", 3)
    command, options = calls[0]
    assert "agent_task.py" in command[1]
    assert command[-2:] == ["--steps", "3"]
    assert not options.get("shell")
    assert options["cwd"] == ROOT
    with pytest.raises(ValueError, match="已经在执行"):
        dashboard.launch("demo")


def test_task_creation_and_limits(tmp_path, monkeypatch):
    dashboard = Dashboard(tmp_path / "runs", ROOT / "config.yaml")
    launched = []
    monkeypatch.setattr(dashboard, "launch", lambda name, steps: launched.append((name, steps)))
    result = dashboard.create({"goal": "EGFR screening", "mock": True, "steps": 2})
    state = dashboard.store(result["name"]).load()
    assert state.mock and not state.dock_enabled
    assert Path(state.config["target"]["receptor_pdbqt"]).is_absolute()
    assert launched == [(result["name"], 2)]
    seeded = dashboard.create({"goal": "优化母体", "mock": True, "steps": 2,
                               "seed_smiles": ["CCOc1ccccc1"],
                               "constraints": {"max_changed_atoms": 4}})
    seeded_state = dashboard.store(seeded["name"]).load()
    assert seeded_state.candidates["c1"]["candidate_role"] == "seed"
    assert seeded_state.constraints["require_verified_refinement"] is True
    for data in ({"goal": ""}, {"goal": "test", "mock": "false"}, {"goal": "test", "steps": 0}):
        with pytest.raises(ValueError):
            dashboard.create(data)


@pytest.mark.parametrize("name", ["../other", "..", "demo/../../x", "C:\\other", "%2e%2e"])
def test_paths_are_confined(tmp_path, name):
    dashboard, store = setup_task(tmp_path)
    with pytest.raises(ValueError):
        dashboard.store(name)


def test_terminal_and_interrupted_display(tmp_path):
    dashboard, store = setup_task(tmp_path)
    state = store.load()
    state.status = "running"
    state.pending = {"call_id": "a" * 32, "tool": "generate", "reason": "探索不同骨架"}
    store.save(state)
    view = dashboard.snapshot("demo")
    assert view["status_label"] == "执行已停止"
    assert view["needs_recovery"]
    assert view["current"]["reason"] == "探索不同骨架"
    with pytest.raises(ValueError, match="结果不确定"):
        dashboard.launch("demo")
    state.status = "cancelled"
    assert not task_view(state)["can_resume"]


def test_http_serves_ui_protects_controls_and_rejects_bad_payload(tmp_path, monkeypatch):
    dashboard, store = setup_task(tmp_path)
    Harness(store).run(1)
    # Only this test permits loopback sockets; external network remains prohibited.
    def connect_local(sock, address):
        assert address[0] == "127.0.0.1"
        return ORIGINAL_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect_local)
    server = make_server(dashboard, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def request(method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()
    try:
        status, html = request("GET", "/")
        assert status == 200
        assert "任务控制台" in html
        token = re.search(r"const token='([^']+)'", html).group(1)
        headers = {"X-Task-Token": token}
        assert request("GET", "/api/tasks")[0] == 403
        assert request("GET", "/api/tasks", headers=headers)[0] == 200
        detail_status, detail_body = request("GET", "/api/tasks/demo/candidates/c1", headers=headers)
        assert detail_status == 200
        assert json.loads(detail_body)["image"].startswith("data:image/svg+xml;base64,")
        assert request("POST", "/api/tasks/demo/cancel", {}, headers={**headers, "Origin": "https://example.com"})[0] == 403
        assert request("GET", "/", headers={"Host": "example.com"})[0] == 403
        assert request("POST", "/api/tasks", [], headers)[0] == 400
        assert request("POST", "/api/tasks/demo/steer", {"instruction": "先比较"}, headers)[0] == 200
        assert store.load().instructions[-1]["text"] == "先比较"
        assert request("POST", "/api/tasks/demo/cancel", {}, headers)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_worker_process_completes_offline_flow(tmp_path):
    dashboard = Dashboard(tmp_path / "runs", ROOT / "config.yaml")
    result = dashboard.create({"goal": "筛选 EGFR 候选", "mock": True, "steps": 2})
    name = result["name"]
    process = dashboard.processes[name]
    try:
        assert process.wait(timeout=30) == 0
        first = dashboard.snapshot(name)
        assert first["status"] == "paused"
        assert len(first["candidates"]) == 3
        assert not first["active"]
        dashboard.control(name, "steer", "优先比较已有候选")
        dashboard.launch(name, 2)
        process = dashboard.processes[name]
        assert process.wait(timeout=30) == 0
        final = dashboard.snapshot(name)
        assert final["status"] == "completed"
        assert final["revision"] == 1
        assert final["candidates"] == first["candidates"]
        assert "优先比较已有候选" in final["final"]["model_explanation"]["text"]
        assert final["final"]["model_explanation"]["verified"] is False
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_snapshot_reads_checkpoint_after_worker_liveness(tmp_path, monkeypatch):
    dashboard, store = setup_task(tmp_path)
    def worker_finished(task, checkpoint):
        state = checkpoint.load()
        state.status, state.reason = "paused", "action_limit"
        checkpoint.save(state)
        return False
    monkeypatch.setattr(dashboard, "active", worker_finished)
    assert dashboard.snapshot("demo")["explanation"] == "本次执行步数已用完，可继续。"


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows sharing semantics')
def test_windows_checkpoint_retries_commit_only(tmp_path, monkeypatch):
    import agents.harness.state as module
    dashboard, store = setup_task(tmp_path)
    original = module.os.replace
    attempts = []
    def replace(source, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("reader still open")
        return original(source, target)
    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    state = store.load()
    state.steer("retain results")
    store.save(state)
    assert len(attempts) == 3
    assert store.load().revision == 1
    assert list(store.directory.glob('.task-*.tmp')) == []
