"""Loopback-only dashboard backed by the same persisted harness as the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from .presentation import candidate_detail, task_view
from .runtime import Harness
from .state import CheckpointStore, TaskBusyError, TaskState
from .molecule_ops import add_seed_candidates, normalize_constraints

ROOT = Path(__file__).resolve().parents[2]


def positive(value, name, upper):
    if type(value) is not int or not 1 <= value <= upper:
        raise ValueError(f"{name} 必须是 1–{upper} 的整数")
    return value


class Dashboard:
    def __init__(self, task_root, config_path):
        self.root = Path(task_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(config_path).resolve()
        self.processes = {}
        self.launch_lock = threading.Lock()

    def store(self, task):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task):
            raise ValueError("无效的任务名称")
        directory = (self.root / task).resolve()
        if not directory.is_relative_to(self.root):
            raise ValueError("任务路径超出工作目录")
        store = CheckpointStore(directory)
        if not store.path.is_file():
            raise ValueError("任务不存在")
        return store

    def active(self, task, store):
        process = self.processes.get(task)
        if process is not None and process.poll() is None:
            return True
        try:
            with store.lock():
                return False
        except TaskBusyError:
            return True

    def snapshot(self, task):
        store = self.store(task)
        active = self.active(task, store)
        state = store.load()
        view = task_view(state, active)
        view["name"] = task
        if active and state.status not in {"completed", "cancelled"}:
            view["status_label"] = "执行中"
        if state.pending and view["needs_recovery"]:
            call_id = state.pending.get("call_id", "")
            if re.fullmatch(r"[0-9a-f]{32}", call_id) and (store.directory / "receipts" / f"{call_id}.json").is_file():
                view["needs_recovery"] = False
                view["explanation"] = "工具结果回执已保存，继续时会先恢复结果。"
        view["queued_controls"] = [{"kind": c["kind"], "instruction": c.get("instruction"),
                                    "constraints": c.get("constraints")}
                                   for c in store.controls(state)]
        process = self.processes.get(task)
        code = process.poll() if process else None
        view["worker_error"] = "执行进程异常退出；已有结果仍保留。请检查任务目录中的 worker.log。" if code not in (None, 0) else None
        if state.status == "running" and not active:
            view["status_label"] = "执行已停止"
            if not state.pending or view["needs_recovery"]:
                view["explanation"] = "执行进程已退出，已有结果保存在检查点中。"
        return view

    def listing(self):
        result = []
        for path in sorted(self.root.iterdir(), key=lambda p: p.name, reverse=True):
            if not (path / "task.json").is_file() or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", path.name):
                continue
            try:
                view = self.snapshot(path.name)
                result.append({k: view[k] for k in ("name", "goal", "status_label", "mock", "active")})
            except (ValueError, OSError, TypeError, KeyError):
                continue
        return result

    def candidate_detail(self, task, candidate_id):
        if not re.fullmatch(r"c[0-9]+", candidate_id):
            raise ValueError("无效的候选编号")
        return candidate_detail(self.store(task).load(), candidate_id)

    def create(self, data):
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 5000:
            raise ValueError("请输入 1–5000 字的任务目标")
        for flag in ("mock", "dock"):
            if type(data.get(flag, flag == "mock")) is not bool:
                raise ValueError("运行模式必须是布尔值")
        steps = positive(data.get("steps", 2), "本次步数", 100)
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        # Match the repository-root convention used by loop.py/config.yaml.
        receptor = Path(config["target"]["receptor_pdbqt"])
        config["target"]["receptor_pdbqt"] = str((ROOT / receptor).resolve())
        seeds = data.get("seed_smiles", [])
        if not isinstance(seeds, list) or len(seeds) > 20:
            raise ValueError("母体 SMILES 必须是最多 20 项的列表")
        raw_constraints = data.get("constraints", {})
        if not isinstance(raw_constraints, dict):
            raise ValueError("结构化约束必须是对象")
        if seeds and "require_verified_refinement" not in raw_constraints:
            raw_constraints["require_verified_refinement"] = True
        if seeds:
            raw_constraints.setdefault("require_meaningful_improvement", True)
            raw_constraints.setdefault("require_planned_edits", True)
            raw_constraints.setdefault("require_option_screening", True)
        constraints = normalize_constraints(raw_constraints, dock_enabled=data.get("dock", False))
        state = TaskState(goal=goal.strip(), config=config, mock=data.get("mock", True),
                          dock_enabled=data.get("dock", False),
                          max_steps=positive(data.get("max_steps", 20), "总步数", 1000),
                          max_model_calls=positive(data.get("max_model_calls", 40), "模型预算", 2000),
                          max_evaluations=positive(data.get("max_evaluations", 100), "评估预算", 10000),
                          constraints=constraints)
        if seeds:
            add_seed_candidates(state, seeds, source="dashboard")
        task = "task_" + uuid4().hex[:12]
        store = CheckpointStore(self.root / task)
        with store.lock():
            store.save(state)
        self.launch(task, steps)
        return {"name": task}

    def launch(self, task, steps=2, acknowledge=False):
        positive(steps, "本次步数", 100)
        if type(acknowledge) is not bool:
            raise ValueError("恢复确认必须是布尔值")
        with self.launch_lock:
            store = self.store(task)
            if self.active(task, store):
                raise ValueError("任务已经在执行")
            view = self.snapshot(task)
            if not view["can_resume"]:
                raise ValueError("任务当前不能继续，请查看停止原因")
            if view["needs_recovery"] and not acknowledge:
                raise ValueError("上次工具调用结果不确定，请先勾选恢复确认")
            command = [sys.executable, str(ROOT / "agent_task.py"), "resume", "--task-dir", str(store.directory), "--steps", str(steps)]
            if acknowledge:
                command.append("--ack-interrupted")
            with (store.directory / "worker.log").open("ab") as log:
                self.processes[task] = subprocess.Popen(
                    command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return {"status": "started"}

    def control(self, task, kind, instruction=None, constraints=None):
        store = self.store(task)
        if isinstance(instruction, str) and len(instruction) > 5000:
            raise ValueError("追加要求不能超过 5000 字")
        if kind == "constraints":
            current = store.load()
            normalize_constraints({**normalize_constraints(current.constraints, dock_enabled=current.dock_enabled),
                                   **(constraints or {})})
        control = store.submit_control(kind, instruction, constraints)
        updated = Harness(store).apply_controls()
        applied = updated is not None and control["id"] in updated.processed_controls
        return {"status": "applied" if applied else "queued"}


def make_server(dashboard, port=8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, data, mime="application/json; charset=utf-8"):
            body = data.encode("utf-8") if isinstance(data, str) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def authorized(self):
            return self.allowed_host() and secrets.compare_digest(self.headers.get("X-Task-Token", ""), token)

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.allowed_host():
                return self.respond(403, {"error": "请使用控制台启动时显示的本机地址"})
            if path == "/":
                html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
                return self.respond(200, html.replace("__TASK_TOKEN__", token), "text/html; charset=utf-8")
            if not self.authorized():
                return self.respond(403, {"error": "请刷新控制台后重试"})
            try:
                if path == "/api/tasks":
                    return self.respond(200, {"tasks": dashboard.listing()})
                detail = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/candidates/(c[0-9]+)", path)
                if detail:
                    return self.respond(200, dashboard.candidate_detail(*detail.groups()))
                if path.startswith("/api/tasks/"):
                    return self.respond(200, dashboard.snapshot(path.removeprefix("/api/tasks/")))
                self.respond(404, {"error": "页面不存在"})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})

        def do_POST(self):
            if not self.authorized():
                return self.respond(403, {"error": "请刷新控制台后重试"})
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                return self.respond(403, {"error": "请求来源不匹配"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    raise ValueError("请求大小无效")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("请求必须是对象")
                path = urlsplit(self.path).path
                if path == "/api/tasks":
                    result = dashboard.create(data)
                else:
                    match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/([a-z]+)", path)
                    if not match:
                        return self.respond(404, {"error": "操作不存在"})
                    task, operation = match.groups()
                    if operation == "resume":
                        result = dashboard.launch(task, data.get("steps", 2), data.get("acknowledge", False))
                    elif operation in {"steer", "pause", "cancel", "constraints"}:
                        result = dashboard.control(task, operation, data.get("instruction"), data.get("constraints"))
                    else:
                        return self.respond(404, {"error": "操作不存在"})
                self.respond(200, result)
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
