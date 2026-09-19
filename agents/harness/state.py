"""Versioned task checkpoints and a process-scoped single-writer lock."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4


class TaskBusyError(RuntimeError):
    pass


@dataclass
class TaskState:
    goal: str
    config: dict
    mock: bool = False
    dock_enabled: bool = False
    max_steps: int = 20
    task_id: str = field(default_factory=lambda: uuid4().hex)
    schema_version: int = 1
    persistence_version: int = 0
    status: str = "paused"
    reason: str = "created"
    revision: int = 0
    instructions: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    pending: dict | None = None
    steps_used: int = 0
    consecutive_errors: int = 0
    final: dict | None = None
    max_model_calls: int = 40
    model_calls_used: int = 0
    max_evaluations: int = 100
    evaluations_used: int = 0
    idle_actions: int = 0
    protocol_id: str | None = None
    processed_controls: list = field(default_factory=list)
    constraints: dict = field(default_factory=dict)
    constraint_history: list = field(default_factory=list)
    hypotheses: dict = field(default_factory=dict)
    strategies: list = field(default_factory=list)
    final_history: list = field(default_factory=list)
    edit_proposals: dict = field(default_factory=dict)
    edit_selections: dict = field(default_factory=dict)
    option_screenings: dict = field(default_factory=dict)

    def steer(self, instruction: str) -> None:
        if not instruction.strip():
            raise ValueError("Instruction must not be empty")
        if self.status in {"completed", "cancelled"}:
            raise ValueError("Terminal tasks cannot be resumed; create a new task")
        self.revision += 1
        self.instructions.append({"revision": self.revision, "text": instruction.strip()})
        self.events.append({"type": "instruction", **self.instructions[-1]})
        if self.final:
            self.final_history.append(self.final)
            self.final = None

    def update_constraints(self, changes: dict) -> None:
        from .molecule_ops import normalize_constraints
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Constraint changes must be a nonempty object")
        current = normalize_constraints(self.constraints, dock_enabled=self.dock_enabled)
        updated = normalize_constraints({**current, **changes})
        changed = {key: value for key, value in updated.items() if current.get(key) != value}
        if not changed:
            return
        old_docking = self.dock_enabled
        self.constraints = updated
        self.dock_enabled = updated["docking_allowed"]
        # An explicit user constraint change starts a new evaluation protocol.
        # Existing candidate evidence stays immutable and carries its own protocol_id.
        if old_docking != self.dock_enabled:
            self.protocol_id = None
        self.revision += 1
        record = {"revision": self.revision, "changes": changed}
        self.constraint_history.append(record)
        self.events.append({"type": "constraint_update", **record})
        if self.final:
            self.final_history.append(self.final)
            self.final = None
        from .evidence import revalidate
        revalidate(self)


class CheckpointStore:
    def __init__(self, directory, repository=None):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "task.json"
        self.repository = repository

    def load(self) -> TaskState:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported checkpoint version")
        state = TaskState(**data)
        if self.repository is not None and not self.repository.state_matches(state):
            raise ValueError("JSON checkpoint and SQLite repository are inconsistent")
        return state

    def save(self, state: TaskState) -> None:
        state.persistence_version += 1
        self._write(self.path, asdict(state))
        if self.repository is not None:
            self.repository.save_state(state)

    def load_from_repository(self, task_id: str | None = None) -> TaskState:
        if self.repository is None:
            raise ValueError("No repository configured")
        return self.repository.load_state(task_id or self.load().task_id)

    def _write(self, path, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        fd, name = tempfile.mkstemp(prefix=".task-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows readers can briefly deny replacement even though the writer
            # lock is held. Retry only this atomic commit, never the tool itself.
            for attempt in range(8):
                try:
                    os.replace(name, path)
                    break
                except PermissionError:
                    if os.name != "nt" or attempt == 7:
                        raise
                    time.sleep(min(0.01 * 2 ** attempt, 0.1))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def receipt(self, call_id, state):
        """Persist the complete post-tool state before committing the main checkpoint."""
        self._write(self.directory / "receipts" / f"{call_id}.json", asdict(state))

    def recover(self, state):
        if not state.pending:
            return state
        call_id = state.pending["call_id"]
        # IDs are internal UUIDs, never model-controlled paths.
        if not isinstance(call_id, str) or len(call_id) != 32 or any(c not in "0123456789abcdef" for c in call_id):
            raise ValueError("Invalid pending call ID")
        path = self.directory / "receipts" / f"{call_id}.json"
        if not path.exists():
            return state
        recovered = TaskState(**json.loads(path.read_text(encoding="utf-8")))
        if (recovered.task_id != state.task_id or recovered.revision != state.revision
                or recovered.steps_used != state.steps_used or recovered.pending is not None
                or not any(e.get("call_id") == call_id and e.get("type") == "tool_result"
                           for e in recovered.events)):
            raise ValueError("Receipt does not match the pending checkpoint")
        self.save(recovered)
        return recovered

    def submit_control(self, kind, instruction=None, constraints=None):
        if kind not in {"pause", "cancel", "steer", "constraints"}:
            raise ValueError("Unknown control")
        if kind == "steer" and (not isinstance(instruction, str) or not instruction.strip()):
            raise ValueError("Steering requires a nonempty instruction")
        if kind == "constraints" and (not isinstance(constraints, dict) or not constraints):
            raise ValueError("Constraint update requires a nonempty object")
        state = self.load()
        if state.status in {"completed", "cancelled"}:
            raise ValueError("Task is already terminal")
        from time import time_ns
        control = {"id": uuid4().hex, "kind": kind, "instruction": instruction,
                   "constraints": constraints,
                   "task_id": state.task_id, "created_ns": time_ns()}
        self._write(self.directory / "controls" / f"{control['created_ns']}-{control['id']}.json", control)
        return control

    def controls(self, state):
        for path in sorted((self.directory / "controls").glob("*.json")):
            control = json.loads(path.read_text(encoding="utf-8"))
            if control["id"] not in state.processed_controls:
                if control["task_id"] != state.task_id:
                    raise ValueError("Control belongs to a different task")
                yield control

    @contextmanager
    def lock(self):
        """OS releases this lock even if the worker crashes."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".writer.lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise TaskBusyError("Task is already running in another process") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
