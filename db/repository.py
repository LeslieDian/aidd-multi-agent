"""SQLite persistence for Harness task state and auditable facts."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock

from agents.harness.state import TaskState


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SQLiteRepository:
    """Local repository with idempotent facts and exact TaskState recovery."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                task_id TEXT PRIMARY KEY, protocol_id TEXT, status TEXT NOT NULL,
                revision INTEGER NOT NULL, state_json TEXT NOT NULL, state_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES runs(task_id) ON DELETE CASCADE,
                smiles TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                PRIMARY KEY(task_id, candidate_id)
            );
            CREATE TABLE IF NOT EXISTS rounds (
                task_id TEXT NOT NULL REFERENCES runs(task_id) ON DELETE CASCADE,
                round_no INTEGER NOT NULL,
                protocol_id TEXT,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                PRIMARY KEY(task_id, round_no)
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                evaluation_key TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES runs(task_id) ON DELETE CASCADE,
                candidate_id TEXT NOT NULL, protocol_id TEXT, status TEXT NOT NULL,
                payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                PRIMARY KEY(task_id, evaluation_key)
            );
            CREATE TABLE IF NOT EXISTS agent_events (
                event_key TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES runs(task_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                PRIMARY KEY(task_id, event_key)
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_key TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES runs(task_id) ON DELETE CASCADE,
                uri TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, metadata_json TEXT NOT NULL,
                PRIMARY KEY(task_id, artifact_key)
            );
            CREATE TABLE IF NOT EXISTS human_approvals (
                approval_key TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES runs(task_id) ON DELETE CASCADE,
                decision TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(task_id, approval_key)
            );
            """
        )
        self.connection.commit()

    def close(self):
        self.connection.close()

    def save_state(self, state: TaskState) -> None:
        payload = asdict(state)
        state_json = _json(payload)
        state_sha256 = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO runs(task_id, protocol_id, status, revision, state_json, state_sha256)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET protocol_id=excluded.protocol_id,
                   status=excluded.status, revision=excluded.revision, state_json=excluded.state_json,
                   state_sha256=excluded.state_sha256""",
                (state.task_id, state.protocol_id, state.status, state.revision, state_json, state_sha256),
            )
            for index, event in enumerate(state.events):
                event_key = event.get("call_id") or event.get("id") or f"index:{index}"
                event_json = _json(event)
                self.connection.execute(
                    """INSERT INTO agent_events(event_key, task_id, event_type, payload_json, payload_sha256)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id, event_key) DO NOTHING""",
                    (event_key, state.task_id, event.get("type", "unknown"), event_json, _hash(event)),
                )
            for candidate_id, candidate in state.candidates.items():
                candidate_json = _json(candidate)
                self.connection.execute(
                    """INSERT INTO candidates(candidate_id, task_id, smiles, payload_json, payload_sha256)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id, candidate_id) DO UPDATE SET
                       smiles=excluded.smiles, payload_json=excluded.payload_json,
                       payload_sha256=excluded.payload_sha256""",
                    (candidate_id, state.task_id, candidate.get("smiles", ""), candidate_json, _hash(candidate)),
                )
                if candidate.get("evaluation_status"):
                    self._save_evaluation(state, candidate_id, candidate)
            self.save_round(
                state.task_id,
                state.steps_used,
                state.protocol_id,
                {"status": state.status, "reason": state.reason, "revision": state.revision},
            )

    def save_round(self, task_id: str, round_no: int, protocol_id: str | None, payload: dict) -> None:
        payload_json = _json(payload)
        with self._lock, self.connection:
            values = (protocol_id, payload.get("status", "unknown"), payload_json, _hash(payload))
            self.connection.execute(
                """INSERT INTO rounds(task_id, round_no, protocol_id, status, payload_json, payload_sha256)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, round_no) DO UPDATE SET
                   protocol_id=excluded.protocol_id, status=excluded.status,
                   payload_json=excluded.payload_json, payload_sha256=excluded.payload_sha256""",
                (task_id, round_no, *values),
            )

    def get_evaluation(self, task_id: str, candidate_id: str, protocol_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT protocol_id, payload_json FROM evaluations
                   WHERE task_id=? AND candidate_id=? ORDER BY rowid DESC LIMIT 1""",
                (task_id, candidate_id),
            ).fetchone()
        if row is None:
            return None
        if row["protocol_id"] != protocol_id:
            raise ValueError("Evaluation protocol changed; refusing to reuse persisted evaluation")
        return json.loads(row["payload_json"])

    def _save_evaluation(self, state, candidate_id: str, candidate: dict) -> None:
        evaluation_key = f"{candidate_id}:{candidate.get('protocol_id', state.protocol_id or 'current')}"
        candidate_json = _json(candidate)
        self.connection.execute(
            """INSERT INTO evaluations(evaluation_key, task_id, candidate_id, protocol_id, status,
               payload_json, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id, evaluation_key) DO UPDATE SET status=excluded.status,
               payload_json=excluded.payload_json, payload_sha256=excluded.payload_sha256""",
            (evaluation_key, state.task_id, candidate_id, candidate.get("protocol_id", state.protocol_id),
             candidate.get("evaluation_status", "unknown"), candidate_json, _hash(candidate)),
        )

    def load_state(self, task_id: str) -> TaskState:
        with self._lock:
            row = self.connection.execute("SELECT state_json, state_sha256 FROM runs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        if hashlib.sha256(row["state_json"].encode("utf-8")).hexdigest() != row["state_sha256"]:
            raise ValueError("Persisted TaskState hash mismatch")
        return TaskState(**json.loads(row["state_json"]))

    def record_artifact(self, task_id: str, artifact_key: str, path: str | Path, metadata: dict | None = None) -> None:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        values = (artifact_key, task_id, str(file_path), _file_hash(file_path), file_path.stat().st_size, _json(metadata or {}))
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT uri, sha256, size_bytes, metadata_json FROM artifacts WHERE task_id=? AND artifact_key=?",
                (task_id, artifact_key),
            ).fetchone()
            if existing and tuple(existing) != values[2:]:
                raise ValueError("Conflicting immutable artifact")
            self.connection.execute(
                """INSERT INTO artifacts(artifact_key, task_id, uri, sha256, size_bytes, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, artifact_key) DO NOTHING""", values)

    def verify_artifact(self, task_id: str, artifact_key: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT uri, sha256 FROM artifacts WHERE task_id=? AND artifact_key=?", (task_id, artifact_key)
            ).fetchone()
        return bool(row and Path(row["uri"]).is_file() and _file_hash(Path(row["uri"])) == row["sha256"])

    def save_approval(self, task_id: str, approval_key: str, decision: str, payload: dict) -> None:
        payload_json = _json(payload)
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT decision, payload_json FROM human_approvals WHERE task_id=? AND approval_key=?",
                (task_id, approval_key),
            ).fetchone()
            if existing and tuple(existing) != (decision, payload_json):
                raise ValueError("Conflicting immutable approval")
            self.connection.execute(
                """INSERT INTO human_approvals(approval_key, task_id, decision, payload_json)
                   VALUES (?, ?, ?, ?) ON CONFLICT(task_id, approval_key) DO NOTHING""",
                (approval_key, task_id, decision, payload_json),
            )

    def count(self, table: str, task_id: str) -> int:
        allowed = {"runs", "rounds", "candidates", "evaluations", "agent_events", "artifacts", "human_approvals"}
        if table not in allowed:
            raise ValueError("Unknown repository table")
        with self._lock:
            return self.connection.execute(f"SELECT count(*) FROM {table} WHERE task_id=?", (task_id,)).fetchone()[0]