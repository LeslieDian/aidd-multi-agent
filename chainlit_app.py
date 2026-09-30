"""Chainlit chat front-end for the AIDD Harness.

This file is a UI shell only. The decision/execution logic lives in
``agents/harness/runtime.py``; the events flow through
``agents/harness/chainlit_bridge.py``. The bridge is the only place where
the Harness talks to Chainlit, so swapping front-ends does not touch the
agent core.

Run with:
    chainlit run chainlit_app.py --host 127.0.0.1 --port 8000

Conversation model
------------------
Each Chainlit session (one browser tab) holds **one active task** at a
time. Chat messages default to ``Harness.run(instruction=...)`` on the
active task. Slash commands switch / list / create / control tasks.

Commands recognised in any chat message:
    /tasks                 list every task under ``runs/``
    /switch <task_id>      make the named task the active one
    /start <goal...>       create a new mock-mode task and make it active
    /start! <goal...>      same, but with real LLM (needs API key)
    /status                show the active task's status card
    /pause                 request pause at the next checkpoint
    /cancel                request cancellation at the next checkpoint
    /candidates [n]        list the top ``n`` candidates (default 5)
    /candidate <id>        render the detail card for one candidate
    /help                  print the command list above

Anything else is forwarded as a steering instruction.

Security
--------
The service binds to 127.0.0.1 only; we never expose it externally and we
never persist provider keys or model responses in Chainlit's database.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from chainlit import AskActionMessage, ChatProfile, action, cl

from agents.harness import CheckpointStore, Harness, MockPolicy
from agents.harness.chainlit_bridge import (
    ChainlitBridge,
    list_tasks as discover_tasks,
)

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
RUNS_ROOT = PROJECT_ROOT / "runs"
MAX_CONCURRENT_STEPS = 5  # one user message triggers at most this many decisions
HUMAN_CHECK_EVERY = 3     # ask the user to confirm direction every N steps

_PROFILE = ChatProfile(
    name="AIDD 持久任务",
    icon="https://em-content.zunicode.net/svg/129440.svg",
    markdown_starters=[
        {"label": "列出任务", "message": "/tasks"},
        {"label": "新建任务", "message": "/start 设计针对 EGFR 的口服候选，目标 logP<3、TPSA>75"},
        {"label": "查看状态", "message": "/status"},
    ],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    """Read ``config.yaml`` once per process; CLI flags override path."""
    override = os.environ.get("AIDD_CONFIG")
    path = Path(override) if override else CONFIG_PATH
    if not path.exists():
        return {"llm": {"providers": {}, "generators": []}, "scoring": {}, "target": {}}
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _build_policy(mock: bool, config: dict) -> Any:
    """Pick the right policy for the requested mode.

    Mock mode always works without keys. Real mode constructs an
    ``LLMPolicy`` via the harness' factory so retries and evidence stay
    consistent with the CLI / dashboard paths.
    """
    if mock:
        return MockPolicy()
    from agents.harness.runtime import LLMPolicy
    return LLMPolicy()


def _format_status_card(view: dict) -> str:
    """Render the task-view snapshot as Markdown for the chat."""
    if not view:
        return "_尚未选中任务_"
    budget_lines = "\n".join(
        f"- {row['label']}: {row['used']}/{row['limit']}"
        for row in view.get("budgets", [])
    )
    candidates = view.get("candidates", [])
    top = sorted(
        (c for c in candidates if c.get("composite_score") is not None),
        key=lambda row: row.get("composite_score", -1e9),
        reverse=True,
    )[:5]
    cand_lines = "\n".join(
        f"- `{row['id']}` {row.get('smiles', '?')} · 复合分 "
        f"{row.get('composite_score', '-'):.3f} · Vina {row.get('vina', '-'):.2f}"
        if isinstance(row.get("composite_score"), (int, float))
        else f"- `{row['id']}` {row.get('smiles', '?')} · 待评估"
        for row in top
    ) or "_尚无评分候选_"
    return (
        f"**目标** {view.get('goal', '?')}\n"
        f"**状态** {view.get('status_label', view.get('status', '?'))} · "
        f"{view.get('explanation', view.get('reason', ''))}\n"
        f"**任务 ID** `{view.get('task_id', '?')}`\n"
        f"**预算**\n{budget_lines}\n"
        f"**候选（Top {len(top)}）**\n{cand_lines}"
    )


def _candidate_svg(smiles: str, highlight_atoms: list[int] | None = None) -> str:
    """Return a data URL for the candidate's 2D depiction."""
    from agents.harness.editor import molecule_svg_data_url
    return molecule_svg_data_url(smiles, highlight_atoms or [])


# ---------------------------------------------------------------------------
# Chainlit lifecycle
# ---------------------------------------------------------------------------
@cl.set_chat_profiles
async def _profiles() -> list[ChatProfile]:
    return [_PROFILE]


@cl.on_chat_start
async def _on_chat_start() -> None:
    """Greet the user and prompt for the first action."""
    cl.user_session.set("current_task_id", None)
    cl.user_session.set("bridge", None)
    await cl.Message(
        content=(
            "欢迎使用 AIDD 持久任务助手。\n\n"
            "可用命令：\n"
            "- `/tasks` 列出所有任务\n"
            "- `/start <目标>` 创建一个新的离线演示任务\n"
            "- `/start! <目标>` 使用真实模型（需要配置 MiniMax/DeepSeek 密钥）\n"
            "- `/switch <任务ID>` 切换当前任务\n"
            "- `/status` 查看当前任务状态\n"
            "- `/pause`、`/cancel`、`/resume` 控制当前任务\n"
            "- `/candidates [n]`、`/candidate <id>` 查看候选\n\n"
            "普通文本消息会作为新的需求发送给当前任务。"
        ),
        author="AIDD",
    ).send()


@cl.on_message
async def _on_message(message: cl.Message) -> None:
    """Dispatch slash commands; everything else is a steering instruction."""
    text = (message.content or "").strip()
    if not text:
        return
    head = text.split(maxsplit=1)[0].lower()
    if head in {"/help", "help", "?"}:
        await _show_help()
    elif head == "/tasks":
        await _cmd_tasks()
    elif head == "/switch":
        await _cmd_switch(text)
    elif head == "/start":
        await _cmd_start(text, mock=True)
    elif head == "/start!":
        await _cmd_start(text, mock=False)
    elif head == "/status":
        await _cmd_status()
    elif head == "/pause":
        await _cmd_control("pause")
    elif head == "/cancel":
        await _cmd_control("cancel")
    elif head == "/resume":
        await _cmd_resume(message)
    elif head == "/candidates":
        await _cmd_candidates(text)
    elif head == "/candidate":
        await _cmd_candidate(text)
    else:
        await _steer(text)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def _show_help() -> None:
    await cl.Message(content=_HELP_TEXT, author="AIDD").send()


async def _cmd_tasks() -> None:
    rows = discover_tasks(RUNS_ROOT)
    if not rows:
        await cl.Message(content="`runs/` 下没有任务。用 `/start <目标>` 创建第一个。", author="AIDD").send()
        return
    body = "\n".join(
        f"- `{row['task_id'][:8]}` {row['status_label']} · 步数 {row['steps_used']}/{row['max_steps']} · "
        f"{'模拟' if row['mock'] else '真实'} · 目标：{row['goal'][:60]}"
        for row in rows
    )
    await cl.Message(content=f"**可用任务**\n{body}", author="AIDD").send()


async def _cmd_switch(text: str) -> None:
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await cl.Message(content="用法：`/switch <任务ID>`", author="AIDD").send()
        return
    needle = parts[1].strip()
    for row in discover_tasks(RUNS_ROOT):
        if row["task_id"].startswith(needle):
            cl.user_session.set("current_task_id", row["task_id"])
            cl.user_session.set("bridge", None)
            await cl.Message(content=f"已切换到任务 `{row['task_id'][:8]}`。", author="AIDD").send()
            return
    await cl.Message(content=f"找不到任务 `{needle}`。先 `/tasks` 看一下。", author="AIDD").send()


async def _cmd_start(text: str, *, mock: bool) -> None:
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await cl.Message(content="用法：`/start <目标>`", author="AIDD").send()
        return
    goal = parts[1].strip()
    config = _load_config()
    target_dir = RUNS_ROOT / f"chat_{os.getpid()}_{int.asyncio}"
    target_dir = RUNS_ROOT / f"chat_{os.getpid()}"
    state_factory = _build_state_factory(goal, config=config, mock=mock, target_dir=target_dir)
    state = state_factory()
    cl.user_session.set("current_task_id", state.task_id)
    cl.user_session.set("bridge", None)
    mode = "离线演示" if mock else "真实模型"
    await cl.Message(content=f"已创建 {mode} 任务 `{state.task_id[:8]}`。发送任意文本开始执行。", author="AIDD").send()


def _build_state_factory(goal: str, *, config: dict, mock: bool, target_dir: Path):
    """Return a callable that creates a fresh ``TaskState`` checkpoint."""
    def _factory() -> Any:
        from agents.harness import TaskState
        from agents.harness.molecule_ops import normalize_constraints
        receptor = config.get("target", {}).get("receptor_pdbqt")
        if receptor:
            config["target"]["receptor_pdbqt"] = str(Path(receptor).resolve())
        constraints = normalize_constraints({}, dock_enabled=False)
        state = TaskState(goal=goal, config=config, mock=mock, dock_enabled=False,
                          max_steps=20, max_model_calls=40, max_evaluations=100,
                          constraints=constraints)
        CheckpointStore(target_dir).save(state)
        return state
    return _factory


async def _cmd_status() -> None:
    bridge = await _ensure_bridge()
    if bridge is None:
        return
    await cl.Message(content=_format_status_card(bridge.snapshot()), author="AIDD").send()


async def _cmd_control(kind: str) -> None:
    task_id = cl.user_session.get("current_task_id")
    if not task_id:
        await cl.Message(content="先 `/start` 或 `/switch` 选中任务。", author="AIDD").send()
        return
    store = await _resolve_store(task_id)
    if store is None:
        return
    store.submit_control(kind)
    await cl.Message(content=f"已提交 `{kind}` 指令；任务在下一个检查点生效。", author="AIDD").send()


async def _cmd_resume(message: cl.Message) -> None:
    bridge = await _ensure_bridge()
    if bridge is None or bridge.harness is None:
        return
    instruction = (message.content or "").split(maxsplit=1)
    instruction_text = instruction[1].strip() if len(instruction) > 1 else ""
    ack = bool(getattr(message, "metadata", {}).get("ack_interrupted"))
    await _run_harness(bridge, instruction=instruction_text, acknowledge_interrupted=ack)


async def _cmd_candidates(text: str) -> None:
    bridge = await _ensure_bridge()
    if bridge is None:
        return
    parts = text.split(maxsplit=1)
    try:
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
    except ValueError:
        n = 5
    candidates = sorted(
        (c for c in bridge.snapshot().get("candidates", [])
         if c.get("composite_score") is not None),
        key=lambda row: row.get("composite_score", -1e9),
        reverse=True,
    )[:n]
    if not candidates:
        await cl.Message(content="尚未有评分候选。", author="AIDD").send()
        return
    body_lines = []
    for row in candidates:
        svg = _candidate_svg(row.get("smiles", ""))
        body_lines.append(
            f"- `{row['id']}` 复合分 {row['composite_score']:.3f} · "
            f"Vina {row.get('vina', '-'):.2f} · 状态 {row.get('status', '?')}"
        )
        await cl.Message(
            content=f"`{row['id']}` · {row.get('smiles', '?')}",
            elements=[cl.Image(name="mol", display="inline",
                               url=svg if svg.startswith("data:") else f"data:image/svg+xml;base64,{svg.split(',', 1)[-1]}")],
            author="AIDD",
        ).send()
    await cl.Message(content="\n".join(body_lines), author="AIDD").send()


async def _cmd_candidate(text: str) -> None:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await cl.Message(content="用法：`/candidate <id>`", author="AIDD").send()
        return
    needle = parts[1].strip()
    bridge = await _ensure_bridge()
    if bridge is None:
        return
    for cid in bridge.snapshot().get("candidates", []):
        if cid["id"].startswith(needle):
            try:
                detail = bridge.candidate_detail(cid["id"])
            except Exception as exc:
                await cl.Message(content=f"无法读取候选 `{cid['id']}`：{exc}", author="AIDD").send()
                return
            svg = detail.get("image", "")
            elements = []
            if svg:
                elements.append(cl.Image(name="child", display="inline",
                                         url=svg if svg.startswith("data:") else f"data:image/svg+xml;base64,{svg.split(',', 1)[-1]}"))
            if detail.get("parent") and detail["parent"].get("image"):
                p_svg = detail["parent"]["image"]
                elements.append(cl.Image(name="parent", display="inline",
                                         url=p_svg if p_svg.startswith("data:") else f"data:image/svg+xml;base64,{p_svg.split(',', 1)[-1]}"))
            attribution = detail.get("property_attribution") or {}
            attr_lines = "\n".join(f"- {k}: {v}" for k, v in attribution.items())
            text_block = (
                f"`{cid['id']}` · {detail.get('smiles', '?')}\n"
                f"**属性归因**\n{attr_lines or '_无_'}"
            )
            await cl.Message(content=text_block, elements=elements, author="AIDD").send()
            return
    await cl.Message(content=f"当前任务里没有候选 `{needle}`。", author="AIDD").send()


async def _steer(instruction: str) -> None:
    bridge = await _ensure_bridge()
    if bridge is None or bridge.harness is None:
        return
    await _run_harness(bridge, instruction=instruction, acknowledge_interrupted=False)


# ---------------------------------------------------------------------------
# Bridge plumbing
# ---------------------------------------------------------------------------
async def _ensure_bridge() -> ChainlitBridge | None:
    """Lazy-build a ChainlitBridge for the currently active task."""
    bridge: ChainlitBridge | None = cl.user_session.get("bridge")
    task_id: str | None = cl.user_session.get("current_task_id")
    if not task_id:
        await cl.Message(content="先 `/start` 或 `/switch` 选中任务。", author="AIDD").send()
        return None
    if bridge is not None and bridge.task_state is not None and bridge.task_state.task_id == task_id:
        return bridge
    store = await _resolve_store(task_id)
    if store is None:
        return None
    state = store.load()
    bridge = ChainlitBridge(store=store)
    bridge.task_state = state
    bridge.last_view = bridge.snapshot()
    policy = _build_policy(state.mock, state.config)
    harness = bridge.build_harness(policy=policy, max_steps=state.max_steps)
    cl.user_session.set("bridge", bridge)
    return bridge


async def _resolve_store(task_id: str) -> CheckpointStore | None:
    """Find the on-disk task directory for ``task_id`` and return its store."""
    if not RUNS_ROOT.exists():
        await cl.Message(content=f"找不到 `{RUNS_ROOT}`。", author="AIDD").send()
        return None
    for row in discover_tasks(RUNS_ROOT):
        if row["task_id"] == task_id:
            return CheckpointStore(row["task_dir"])
    await cl.Message(content=f"任务 `{task_id[:8]}` 的 checkpoint 已丢失。", author="AIDD").send()
    return None


async def _run_harness(bridge: ChainlitBridge, *, instruction: str,
                       acknowledge_interrupted: bool) -> None:
    """Drive ``Harness.run`` off the event loop and stream steps back."""
    if bridge.harness is None:
        return
    bridge.task_state = bridge.store.load()
    # The Harness itself is sync; push it onto a worker thread so the
    # Chainlit websocket stays responsive while the agent is running.
    result: dict[str, Any] = {"state": None, "error": None}
    def _worker() -> None:
        try:
            state = bridge.harness.run(  # type: ignore[union-attr]
                max_actions=MAX_CONCURRENT_STEPS,
                instruction=instruction or None,
                acknowledge_interrupted=acknowledge_interrupted,
            )
            result["state"] = state
        except Exception as exc:  # surface the failure to the chat
            result["error"] = exc
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _worker)
    if result["error"] is not None:
        await cl.Message(content=f"执行出错：`{result['error']}`", author="AIDD").send()
        return
    state = result["state"]
    if state is None:
        return
    bridge.task_state = state
    bridge.last_view = bridge.snapshot()
    await cl.Message(content=_format_status_card(bridge.last_view), author="AIDD").send()
    if state.status not in {"completed", "cancelled"} and state.steps_used % HUMAN_CHECK_EVERY == 0:
        await _ask_to_continue()


async def _ask_to_continue() -> None:
    """Let the user steer / pause / stop after a batch of decisions."""
    res = await AskActionMessage(
        content="任务还在运行。要继续吗？",
        actions=[
            action(name="continue", label="再跑 5 步", payload={"value": "continue"}),
            action(name="pause", label="暂停", payload={"value": "pause"}),
            action(name="cancel", label="取消任务", payload={"value": "cancel"}),
        ],
    ).send()
    if res is None:
        return
    choice = (res.get("payload") or {}).get("value") if isinstance(res, dict) else None
    if choice == "pause":
        await _cmd_control("pause")
    elif choice == "cancel":
        await _cmd_control("cancel")
    else:
        bridge = cl.user_session.get("bridge")
        if bridge is not None:
            await _run_harness(bridge, instruction="", acknowledge_interrupted=False)


# ---------------------------------------------------------------------------
# Static text
# ---------------------------------------------------------------------------
_HELP_TEXT = """\
**可用命令**
- `/tasks` 列出所有任务
- `/switch <任务ID>` 切换当前任务
- `/start <目标>` 创建离线演示任务
- `/start! <目标>` 创建真实任务（需密钥）
- `/status` 查看状态
- `/pause` / `/cancel` 控制任务
- `/candidates [n]` 列出 Top N 候选
- `/candidate <id>` 查看候选详情与结构

普通文本消息会作为新的需求（steer）发送给当前任务。
"""


# Register the Chainlit entry point. ``cl.run`` is the standard hook so
# ``chainlit run chainlit_app.py`` works without extra config.
cl.run(__file__)
