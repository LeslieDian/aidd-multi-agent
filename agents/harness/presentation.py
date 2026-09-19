"""User-facing projections. Never expose provider configuration or credentials."""

TOOLS = {
    "evaluate_options": "计入预算的备选性质评估",
    "propose_edits": "提出备选方案并预检查", "select_edit": "选择修改方案", "execute_selected_edit": "执行已选方案",
    "choose_strategy": "选择后续策略",
    "import_candidates": "导入母体", "generate": "探索候选", "refine": "优化指定候选", "evaluate": "评估候选",
    "compare": "比较候选", "compare_parent_child": "比较父子候选", "history": "查询历史", "retry_evaluation": "重试失败评估",
    "record_hypothesis": "记录优化假设", "attach_fragment": "连接片段",
    "replace_substituent": "替换取代基", "remove_terminal_group": "移除末端基团",
    "replace_bioisostere": "替换生物电子等排体", "change_bond_order": "改变键级",
    "pause": "等待你的补充", "finish": "整理最终结果",
}
STATUSES = {"paused": "已暂停", "running": "执行中", "completed": "已完成", "cancelled": "已取消"}
REASONS = {
    "created": "任务已创建，可以开始执行。", "action_limit": "本次执行步数已用完，可继续。",
    "step_budget_exhausted": "任务总步数已用完。", "model_call_budget_exhausted": "模型调用预算已用完。",
    "evaluation_budget_exhausted": "候选评估预算不足。", "user_paused": "已按你的要求暂停。",
    "user_cancelled": "任务已取消，结果已保留。", "user_interrupt": "执行被中断，已保存进度。",
    "consecutive_errors": "连续出现执行错误，请查看时间线后处理。",
    "repeated_action": "检测到重复动作，已暂停。", "no_progress": "连续多步没有候选变化，已暂停。",
    "evaluation_protocol_changed": "评估环境或协议发生变化，不能继续复用原有结果。",
    "interrupted_action_requires_acknowledgement": "上次工具调用结果不确定，请检查后再恢复。",
    "agent_requested_input": "智能体需要你补充信息。", "agent_finished": "结果已整理完成。",
    "goal_not_met": "候选尚未满足结构化完成条件；已有证据已保留，可调整约束后继续。",
}
EVALUATIONS = {"complete": "完整评估", "screening_only": "性质初筛", "evaluation_error": "评估失败",
               "invalid_structure": "结构无效", "screened_out": "未进入完整评估"}


def action_reason(action):
    reason = action.get("reason", "")
    if reason.startswith("Offline demo: "):
        return "离线演示步骤：" + TOOLS.get(action.get("tool"), action.get("tool", "")) + "。此处使用预设动作，不调用模型。"
    return reason


def event_view(event, index):
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    kind = event.get("type")
    title = TOOLS.get(action.get("tool"), action.get("tool", "执行记录"))
    result = event.get("result") or {}
    detail = ""
    if kind == "tool_result":
        if "added_ids" in result:
            detail = "新增候选：" + ("、".join(result["added_ids"]) or "无；候选可能重复或结构无效")
        elif "statuses" in result:
            detail = "；".join(f"{cid}：{EVALUATIONS.get(status, status)}" for cid, status in result["statuses"].items())
        elif "candidates" in result:
            detail = f"已比较 {len(result['candidates'])} 个候选。"
        elif "comparisons" in result:
            passed = sum(row.get("goal_assessment", {}).get("passed", False) for row in result["comparisons"])
            detail = f"已比较 {len(result['comparisons'])} 组父子候选，其中 {passed} 个子候选满足完成条件。"
        elif "hypothesis" in result:
            detail = "已保存假设：" + result.get("hypothesis_id", "")
        elif "edit_record" in result:
            detail = "已执行确定性编辑：" + result["edit_record"].get("operation", "")
        else:
            detail = result.get("message") or result.get("summary") or "已完成。"
    elif kind in {"error", "retry"}:
        title = "执行失败" if kind == "error" else "临时故障，正在重试"
        detail = event.get("error", "")
    elif kind == "instruction":
        title, detail = "你的新要求", event.get("text", "")
    elif kind == "seed_import":
        title, detail = "导入母体", "已导入：" + "、".join(event.get("candidate_ids", []))
    elif kind == "constraint_update":
        title, detail = "执行约束已更新", "；".join(f"{key} = {value}" for key, value in event.get("changes", {}).items())
    elif kind == "control":
        title = {"steer": "新要求已接收", "pause": "暂停指令已生效", "cancel": "取消指令已生效"}.get(event.get("kind"), "用户指令")
    elif kind == "decision_discarded":
        title, detail = "重新规划", "收到新要求，已放弃过时的决策。"
    elif kind == "interrupted_action":
        title, detail = "中断记录", "上次调用结果不确定，已记录并继续规划。"
    return {"index": index, "kind": kind, "title": title, "detail": detail,
            "reason": action_reason(action), "arguments": action.get("arguments", {}),
            "revision": event.get("revision")}


def task_view(state, active=False):
    from copy import deepcopy
    from .evidence import revalidate
    state = deepcopy(state)
    revalidate(state)
    from .molecule_ops import candidate_goal_assessment, evidence_delta, normalize_constraints
    constraints = normalize_constraints(state.constraints, dock_enabled=state.dock_enabled)
    pending = state.pending
    current = {"title": TOOLS.get(pending["tool"], pending["tool"]),
               "reason": action_reason(pending), "arguments": pending.get("arguments", {})} if pending else None
    if not current and active:
        current = {"title": "正在决定下一步", "reason": "根据任务目标、你的要求和已有结果选择动作。", "arguments": {}}
    candidates = []
    for cid, candidate in state.candidates.items():
        parent = state.candidates.get(candidate.get("parent_id"))
        delta = evidence_delta(parent, candidate) if parent and "evaluation_status" in parent and "evaluation_status" in candidate else None
        candidates.append({"id": cid, "smiles": candidate.get("smiles"),
                           "parent_id": candidate.get("parent_id"),
                           "role": candidate.get("candidate_role"),
                           "modification_verified": candidate.get("current_validation", {}).get("passed", candidate.get("modification_verified")),
                           "verification": candidate.get("current_validation", candidate.get("refinement_verification")),
                           "current_improvement": candidate.get("current_improvement"),
                           "evidence_delta": delta,
                           "goal_assessment": candidate_goal_assessment(candidate, constraints),
                           "status": EVALUATIONS.get(candidate.get("evaluation_status"), "待评估"),
                           "property_score": candidate.get("property_score"),
                           "composite_score": candidate.get("composite_score"),
                           "vina": (candidate.get("dock") or {}).get("score"),
                           "safety_gate_pass": candidate.get("safety_gate_pass")})
    budget_reasons = {"step_budget_exhausted", "model_call_budget_exhausted", "evaluation_budget_exhausted", "evaluation_protocol_changed"}
    terminal = state.status in {"completed", "cancelled"}
    last_message = next((e.get("result", {}).get("message") for e in reversed(state.events)
                         if e.get("type") == "tool_result" and e.get("action", {}).get("tool") == "pause"), None)
    final = dict(state.final) if state.final else None
    if final:
        summary = final.get("summary", "")
        if state.mock and summary.startswith("Offline demonstration completed;"):
            summary = "离线演示完成，已保留候选及评估结果。"
            if state.instructions:
                summary += "\n收到的要求：" + "；".join(i["text"] for i in state.instructions)
        final["summary"] = "最终候选：" + "、".join(final.get("candidate_ids", [])) + "\n\n" + summary
        final["limitations"] = ("离线演示未调用真实模型。" if state.mock else "") + "ADMET 为启发式代理；Vina 分数不等于实验亲和力。" + ("本任务仅完成性质初筛。" if not state.dock_enabled else "")
    return {"task_id": state.task_id, "goal": state.goal, "status": state.status,
            "status_label": STATUSES.get(state.status, state.status), "reason": state.reason,
            "explanation": REASONS.get(state.reason, state.reason), "active": active,
            "needs_recovery": bool(pending and not active and not terminal),
            "can_resume": not active and not terminal and state.reason not in budget_reasons,
            "mock": state.mock, "dock_enabled": state.dock_enabled, "revision": state.revision,
            "constraints": constraints, "constraint_history": state.constraint_history,
            "current": current, "question": last_message if state.reason == "agent_requested_input" else None,
            "budgets": [
                {"label": "决策步数", "used": state.steps_used, "limit": state.max_steps},
                {"label": "模型调用", "used": state.model_calls_used, "limit": state.max_model_calls},
                {"label": "评估（含备选）", "used": state.evaluations_used, "limit": state.max_evaluations}],
            "instructions": state.instructions, "candidates": candidates,
            "hypotheses": list(state.hypotheses.values()), "strategies": state.strategies,
            "edit_proposals": list(state.edit_proposals.values()), "edit_selections": list(state.edit_selections.values()),
            "events": [event_view(e, i + 1) for i, e in enumerate(state.events)], "final": final}


def candidate_detail(state, candidate_id):
    from .attribution import breakdown, compare_breakdown, check_predictions
    from copy import deepcopy
    from .evidence import revalidate
    state = deepcopy(state)
    revalidate(state)
    from .editor import molecule_svg_data_url
    from .molecule_ops import evidence_delta
    if candidate_id not in state.candidates:
        raise ValueError("候选不存在")
    candidate = state.candidates[candidate_id]
    parent = state.candidates.get(candidate.get("parent_id"))
    verification = candidate.get("current_validation") or candidate.get("refinement_verification") or {}
    child_highlights = verification.get("changed_child_atom_indices", [])
    parent_highlights = verification.get("changed_parent_atom_indices", [])
    return {
        "candidate_id": candidate_id,
        "smiles": candidate["smiles"],
        "image": molecule_svg_data_url(candidate["smiles"], child_highlights),
        "highlight_atoms": child_highlights,
        "parent": ({"candidate_id": parent["candidate_id"], "smiles": parent["smiles"],
                    "image": molecule_svg_data_url(parent["smiles"], parent_highlights),
                    "highlight_atoms": parent_highlights} if parent else None),
        "edit_record": candidate.get("edit_record"),
        "verification": candidate.get("refinement_verification"),
        "current_validation": candidate.get("current_validation"),
        "validation_history": candidate.get("validation_history", []),
        "current_improvement": candidate.get("current_improvement"),
        "evidence_delta": evidence_delta(parent, candidate)
        if parent and "evaluation_status" in parent and "evaluation_status" in candidate else None,
        "hypothesis": state.hypotheses.get(candidate.get("hypothesis_id")),
        "property_attribution": breakdown(candidate, state.config["scoring"]),
        "attribution_delta": compare_breakdown(parent, candidate, state.config["scoring"]) if parent else None,
        "prediction_checks": check_predictions(state.hypotheses.get(candidate.get("hypothesis_id"), {}).get("predictions", []), parent, candidate) if parent else [],
    }
