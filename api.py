"""FastAPI service for persistent Harness tasks."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from threading import Lock
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field
import yaml

from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints
from db.repository import SQLiteRepository


class RunRequest(BaseModel):
    goal: str = Field(min_length=1)
    mock: bool = True
    dock: bool = False
    steps: int = Field(default=5, ge=1, le=100)
    max_steps: int = Field(default=20, ge=1, le=1000)
    max_model_calls: int = Field(default=40, ge=1, le=1000)
    max_evaluations: int = Field(default=100, ge=1, le=10000)
    seed_smiles: list[str] = Field(default_factory=list)
    constraints: dict = Field(default_factory=dict)


class ControlRequest(BaseModel):
    instruction: str | None = None


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject|request_changes)$")
    reason: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)


def create_app(task_root: str | Path = "runs/api", database: str | Path = "runs/api.sqlite3") -> FastAPI:
    root = Path(task_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(database)
    config_path = Path(__file__).resolve().parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    active = set()
    active_lock = Lock()
    app = FastAPI(title="AIDD Agent API", version="0.1.0")
    app.state.repository = repository
    app.state.single_writer_scope = "one FastAPI process"

    def store_for(task_id: str) -> CheckpointStore:
        task_dir = root / task_id
        if not task_dir.is_dir():
            raise HTTPException(status_code=404, detail="Unknown run")
        return CheckpointStore(task_dir, repository=repository)

    def summary(state: TaskState) -> dict:
        return {
            "task_id": state.task_id, "goal": state.goal, "status": state.status,
            "reason": state.reason, "revision": state.revision, "steps_used": state.steps_used,
            "max_steps": state.max_steps, "candidates": len(state.candidates),
            "model_calls_used": state.model_calls_used, "max_model_calls": state.max_model_calls,
            "evaluations_used": state.evaluations_used, "max_evaluations": state.max_evaluations,
            "protocol_id": state.protocol_id, "pending": state.pending, "final": state.final,
        }

    def execute(task_id: str, steps: int) -> None:
        with active_lock:
            if task_id in active:
                return
            active.add(task_id)
        try:
            Harness(store_for(task_id)).run(max_actions=steps)
        finally:
            with active_lock:
                active.discard(task_id)

    app.state.execute_run = execute

    @app.get("/health")
    def health():
        return {"status": "ok", "database": str(repository.path)}

    @app.post("/runs", status_code=202)
    def create_run(request: RunRequest, background: BackgroundTasks):
        task_id = uuid.uuid4().hex
        task_dir = root / task_id
        task_config = json.loads(json.dumps(config))
        receptor = task_config["target"]["receptor_pdbqt"]
        task_config["target"]["receptor_pdbqt"] = str((Path(__file__).resolve().parent / receptor).resolve())
        constraints = normalize_constraints(request.constraints, dock_enabled=request.dock)
        state = TaskState(task_id=task_id, goal=request.goal, config=task_config, mock=request.mock,
                          dock_enabled=request.dock, max_steps=request.max_steps,
                          max_model_calls=request.max_model_calls, max_evaluations=request.max_evaluations,
                          constraints=constraints)
        if request.seed_smiles:
            add_seed_candidates(state, request.seed_smiles, source="api")
        store = CheckpointStore(task_dir, repository=repository)
        with store.lock():
            store.save(state)
        background.add_task(execute, task_id, request.steps)
        return {"task_id": task_id, "status": "queued"}

    @app.get("/runs/{task_id}")
    def get_run(task_id: str):
        state = store_for(task_id).load_from_repository()
        return summary(state)

    @app.get("/runs/{task_id}/candidates")
    def get_candidates(task_id: str):
        state = store_for(task_id).load_from_repository()
        return {"task_id": task_id, "candidates": list(state.candidates.values())}

    @app.post("/runs/{task_id}/pause")
    def pause_run(task_id: str):
        return _queue_control(store_for(task_id), "pause")

    @app.post("/runs/{task_id}/cancel")
    def cancel_run(task_id: str):
        return _queue_control(store_for(task_id), "cancel")

    @app.post("/runs/{task_id}/resume", status_code=202)
    def resume_run(task_id: str, background: BackgroundTasks, request: ControlRequest | None = None):
        store = store_for(task_id)
        state = store.load_from_repository()
        if state.status in {"completed", "cancelled"}:
            raise HTTPException(status_code=409, detail="Terminal runs cannot be resumed")
        if request and request.instruction:
            store.submit_control("steer", request.instruction)
        background.add_task(execute, task_id, 5)
        return {"task_id": task_id, "status": "queued"}

    @app.post("/runs/{task_id}/approvals")
    def approve_run(task_id: str, request: ApprovalRequest):
        state = store_for(task_id).load_from_repository()
        key = f"approval:{state.revision}:{request.reviewer}"
        repository.save_approval(task_id, key, request.decision, request.model_dump())
        return {"task_id": task_id, "approval_key": key, "decision": request.decision}

    def _queue_control(store: CheckpointStore, kind: str):
        control = store.submit_control(kind)
        state = Harness(store).apply_controls()
        return {"status": "applied" if state and control["id"] in state.processed_controls else "queued",
                "control_id": control["id"]}

    return app


app = create_app()
