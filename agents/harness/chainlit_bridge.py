"""Bridge between the Harness event stream and a Chainlit chat session.

The Bridge is intentionally Chainlit-agnostic for everything except the final
``emit_*`` callbacks, so the same Harness can be replayed in tests, scripts
or a different UI without touching this module. The Chainlit-specific bits
live behind ``on_step`` / ``on_message`` / ``on_image`` callables injected
by ``chainlit_app.py``.

Public surface
--------------
- :class:`ChainlitBridge` — owns the live ``Harness`` instance and forwards
  every event to the registered UI callbacks.
- :func:`list_tasks` — discover tasks under a root by looking for
  ``task.json`` files. Matches ``agent_dashboard.py``'s discovery rule.
- :func:`load_task_state` — convenience wrapper around
  :class:`agents.harness.CheckpointStore`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import CheckpointStore, Harness, TaskState
from .presentation import candidate_detail, event_view, task_view

log = logging.getLogger(__name__)


# Callable signatures (all may be sync or async; the bridge awaits them).
OnStep = Callable[[dict], Awaitable[None] | None]
OnMessage = Callable[[str], Awaitable[None] | None]
OnImage = Callable[[str, str, str | None], Awaitable[None] | None]
OnStatus = Callable[[dict], Awaitable[None] | None]


@dataclass
class ChainlitBridge:
    """Owns one Harness instance and forwards events to a Chainlit session.

    The bridge does not run the Harness — the caller (``chainlit_app.py``)
    drives ``Harness.run()`` and the bridge only consumes the events that
    flow through ``Harness.on_event``. That separation keeps the Harness
    pure: any UI (Chainlit today, Streamlit tomorrow, a CLI dump) just
    registers a different ``on_*`` callback set.
    """

    store: CheckpointStore
    on_step: OnStep | None = None
    on_message: OnMessage | None = None
    on_image: OnImage | None = None
    on_status: OnStatus | None = None
    harness: Harness | None = None
    task_state: TaskState | None = None
    last_view: dict | None = None
    pending_action: dict | None = field(default=None)

    # ------------------------------------------------------------------
    # Harness construction
    # ------------------------------------------------------------------
    def attach(self, harness: Harness) -> None:
        """Bind an externally-owned Harness (created by the caller)."""
        self.harness = harness
        # The Harness invokes ``self.on_event(event)`` synchronously after
        # every step; we wrap it so Chainlit receives an awaitable.
        harness.on_event = self._on_harness_event  # type: ignore[assignment]

    def build_harness(self, *, policy: Any, registry: Any | None = None,
                      max_steps: int | None = None) -> Harness:
        """Construct a Harness using the bridge as the event sink."""
        state = self.store.load()
        if max_steps is not None and max_steps > 0:
            state.max_steps = max_steps
        self.task_state = state
        harness = Harness(self.store, policy=policy, registry=registry,
                          on_event=self._sync_emit, sleep=_async_sleep)
        self.attach(harness)
        return harness

    # ------------------------------------------------------------------
    # Event forwarding
    # ------------------------------------------------------------------
    def _sync_emit(self, event: dict) -> None:
        """Synchronous entry point used by Harness; schedules async emit."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._on_harness_event(event))
        except RuntimeError:
            # No loop (e.g. unit test). Skip; the bridge is reactive only.
            log.debug("chainlit_bridge: no loop, dropping event %s", event.get("type"))

    async def _on_harness_event(self, event: dict) -> None:
        """Translate a single Harness event into UI callbacks."""
        kind = event.get("type")
        if kind == "action":
            self.pending_action = event.get("action") or {}
        elif kind in {"tool_result", "decision"}:
            self.pending_action = None
        # Refresh the cached view after every event so a UI poll always
        # sees the latest projection.
        if self.task_state is not None:
            try:
                self.last_view = task_view(self.task_state, active=True)
            except Exception:
                log.exception("chainlit_bridge: task_view failed")
        if self.on_status and self.last_view is not None:
            await _maybe_await(self.on_status, self.last_view)
        if self.on_step:
            await _maybe_await(self.on_step, event)
        if self.on_message:
            text = _event_to_text(event)
            if text:
                await _maybe_await(self.on_message, text)
        if self.on_image:
            image = _event_to_image(event)
            if image is not None:
                label, data_url, caption = image
                await _maybe_await(self.on_image, label, data_url, caption)

    # ------------------------------------------------------------------
    # Public read API (used by chainlit_app.py to render snapshots)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Return the latest user-facing projection of the task state.

        Falls back to loading the task from the store on first call when
        the caller never wired ``task_state`` explicitly. This keeps
        the bridge usable as a read-only inspector without forcing
        ``chainlit_app.py`` to call ``load_task_state`` up front.
        """
        if self.last_view is not None:
            return self.last_view
        if self.task_state is None:
            self.task_state = self.store.load()
        if self.task_state is None:
            return {}
        return task_view(self.task_state, active=False)

    def candidate_detail(self, candidate_id: str) -> dict:
        """Return the rich detail (including SVG data URL) for one candidate."""
        if self.task_state is None:
            raise ValueError("No task loaded")
        return candidate_detail(self.task_state, candidate_id)


async def _maybe_await(callback: Callable[..., Any], *args: Any) -> None:
    """Call a sync or async callable; never let it break the bridge."""
    try:
        result = callback(*args)
    except Exception:
        log.exception("chainlit_bridge callback failed")
        return
    if asyncio.iscoroutine(result):
        try:
            await result
        except Exception:
            log.exception("chainlit_bridge async callback failed")


async def _async_sleep(seconds: float) -> None:
    """Replacement for ``time.sleep`` so the harness stays non-blocking."""
    if seconds > 0:
        await asyncio.sleep(seconds)


# ----------------------------------------------------------------------
# Discovery helpers (mirror agent_dashboard.py)
# ----------------------------------------------------------------------
def list_tasks(root: str | Path) -> list[dict]:
    """Discover tasks at ``root`` or under ``root``.

    Supports two layouts:

    1. ``root / task.json`` — ``root`` itself is the task directory.
    2. ``root / <task_name> / task.json`` — each subdirectory is a task.

    Results are de-duplicated (case 1 wins if both apply) and sorted by
    ``created`` mtime, newest first.
    """
    root_path = Path(root)
    if not root_path.exists():
        return []

    candidates: dict[str, Path] = {}

    # Case 1: root is a task_dir.
    if (root_path / "task.json").exists():
        candidates[str(root_path)] = root_path

    # Case 2: each subdirectory that holds a task.json.
    try:
        children = list(root_path.iterdir())
    except OSError as exc:
        log.warning("chainlit_bridge: iterdir(%s) failed: %s", root_path, exc)
        children = []
    for child in children:
        if not child.is_dir():
            continue
        if not (child / "task.json").exists():
            continue
        candidates.setdefault(str(child), child)

    found: list[dict] = []
    for task_dir in candidates.values():
        try:
            state = CheckpointStore(task_dir).load()
        except Exception as exc:
            log.warning("chainlit_bridge: failed to load %s: %s", task_dir, exc)
            continue
        found.append({
            "task_id": state.task_id,
            "task_dir": str(task_dir),
            "goal": state.goal,
            "status": state.status,
            "status_label": _STATUS_LABEL.get(state.status, state.status),
            "reason": state.reason,
            "mock": state.mock,
            "steps_used": state.steps_used,
            "max_steps": state.max_steps,
            "created": task_dir.stat().st_mtime,
            "final": state.final is not None,
        })
    found.sort(key=lambda row: row["created"], reverse=True)
    return found


def load_task_state(task_dir: str | Path) -> TaskState:
    """Convenience wrapper used by the CLI and tests."""
    return CheckpointStore(task_dir).load()


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------
_STATUS_LABEL = {
    "paused": "已暂停",
    "running": "执行中",
    "completed": "已完成",
    "cancelled": "已取消",
}


def _event_to_text(event: dict) -> str | None:
    """Best-effort text projection of an event for the chat feed."""
    try:
        view = event_view(event, index=0)
    except Exception:
        return None
    title = view.get("title") or ""
    detail = view.get("detail") or ""
    reason = view.get("reason") or ""
    parts = [p for p in (title, detail, reason) if p and p != "执行记录"]
    if not parts:
        return None
    return " · ".join(parts)


def _event_to_image(event: dict) -> tuple[str, str, str | None] | None:
    """Return a ``(label, data_url, caption)`` tuple when the event carries
    a molecule structure (e.g. a ``refine`` tool result with one child).

    Today the Harness does not embed rendered molecules in its events; we
    surface this hook so a future ``tool_result`` schema can drop in 2-D
    depictions without changing the bridge contract.
    """
    payload = event.get("result") or {}
    smiles = payload.get("smiles")
    if not isinstance(smiles, str):
        return None
    from .editor import molecule_svg_data_url  # local import to avoid rdkit cost
    return ("候选结构", molecule_svg_data_url(smiles, []), payload.get("caption"))
