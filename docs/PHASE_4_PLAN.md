# Phase 4 设计方案：Reflective Single-Agent with Modular Memory

> **目标**：把当前 AIDD 项目从"固定循环脚本"升级为真正符合 Agentic AI 范式的系统。
>
> **设计哲学**：单 Agent + 模块化（不是 Multi-Agent Orchestrator）。AIDD 是一个**有界循环任务**，不需要并行 Worker 流——需要的是一个能"学习、有边界、能反思"的有状态 Agent。

---

## 1. 架构总览

```
┌────────────────────────────────────────────────────────────┐
│                    HITL Checkpoint                          │
│                  (人工闸门 - 3 个检查点)                      │
│                          ↑                                   │
│                  LoopController                              │
│             (显式终止条件 - max/budget/patience)             │
│                          ↑                                   │
│   ┌──────────────┬──────────────┬──────────────┐            │
│   │              │              │              │            │
│ Working      Self-         Planning      ChemRAG            │
│ Memory     Reflection    (多步策略)    (检索式,             │
│ (短期上下文) (Judge 反思)  Phase 4.3     Phase 4.3)         │
│   │              │              │              │            │
│   └──────────────┴──────┬───────┴──────────────┘            │
│                         │                                    │
│                  Agent Main Loop                             │
│                  (generate→evaluate→reflect)                │
│                         │                                    │
│   ┌─────────────────────┼─────────────────────┐              │
│   │                     │                     │              │
│ Failed Ligand        Tool Use              持久化            │
│ Set (Phase 1)         (RDKit/Vina)        (memory/)         │
│ (失败配体去重)                                              │
└────────────────────────────────────────────────────────────┘
```

---

## 2. 6 个核心模块（按 Phase 排序）

### 2.1 Working Memory — 短期上下文

| 项目 | 说明 |
|---|---|
| 职责 | 单次 session 的最近 N 轮上下文 + 最佳候选 |
| 调用频率 | **每轮调用**（最高频） |
| Phase | **4.1**（最高 ROI） |
| 接口 | `add_round(candidates, scores, focus)` / `compress_history() -> str` |
| 数据结构 | `recent_rounds[5]` + `strategy_chain[5]` + `best_so_far` |
| 失败模式 | 上下文太长 → 自动截断到最近 3 轮 |

### 2.2 Failed Ligand Set — 失败配体去重（最高 ROI）

| 项目 | 说明 |
|---|---|
| 职责 | 跨 session 记录跑过且失败的 SMILES |
| 调用频率 | **每次生成都查**（强制过滤）|
| Phase | **4.1**（朴素 set 去重，零成本）|
| 接口 | `add_failed(smi, reason)` / `filter_smiles(cands) -> list` |
| 数据结构 | `set[canonical_smiles]` + JSON 持久化 |
| 阈值 | composite_score < 0.5 或 vina > -2.5 算失败（可调） |
| 注入 prompt | `"DO NOT propose these (already failed, low Vina): {list}"` |

### 2.3 LoopController — 显式终止条件

| 项目 | 说明 |
|---|---|
| 职责 | 决定循环何时停 |
| 调用频率 | 每轮检查 |
| Phase | **4.1**（必须最先做，否则跑飞）|
| **3 个停止条件** | `max_rounds` (5) / `token_budget` (50000) / `judge_convergence_patience` (2) |
| 接口 | `should_stop(state) -> tuple[bool, str]` |
| 归属 | 独立子模块，**不塞进 main loop** |

### 2.4 HITL Checkpoint — 人工闸门

| 项目 | 说明 |
|---|---|
| 职责 | 在 3 个关键决策点暂停让人类确认 |
| 调用频率 | 启动前 / 突破阈值时 / 闭环结束 |
| Phase | **4.1**（合规问题，不是工程偏好）|
| **3 个检查点** | (1) 启动循环前 (2) Vina 突破阈值时 (3) 选择合成候选时 |
| 接口 | `pause_for_approval(context) -> bool` (y/n) |
| 实现 | CLI 交互（`--require-human-approval` flag）|
| **不做** | LLM 决定合成候选——这是合规红线 |

### 2.5 Self-Reflection Judge — 评估自己的建议

| 项目 | 说明 |
|---|---|
| 职责 | Judge C 评估上一轮自己的建议是否有效 |
| 调用频率 | 每轮 1 次 |
| Phase | **4.2** |
| 接口 | `reflect(prev_focus, cands, scores) -> ReflectionResult` |
| 输出 | `next_focus` + `reflection` + `confidence` (0-1) |
| Meta-metric | 跟踪 "judge 建议被下一轮 generator 采纳率" |

### 2.6 Planning — 多步策略（可选）

| 项目 | 说明 |
|---|---|
| 职责 | 启动时生成 3-5 步子任务，每 N 轮更新 |
| 调用频率 | 1 次/任务 |
| Phase | **4.3** |
| 接口 | `make_plan(target, history) -> list[SubTask]` |
| 输出 | `subtasks[3-5]` |
| 验证 | Plan 在 5 轮内是否完成 |

### 2.7 ChemRAG — 检索式（非规则式！）

| 项目 | 说明 |
|---|---|
| 职责 | 从真实数据库检索类似已知药物 + 药效团 |
| 调用频率 | 按需（仅 similarity > 0.7 才注入）|
| Phase | **4.3**（**最低优先级**，可不做）|
| 数据源 | PubChem REST API（免费）|
| 输出前缀 | `"⚠ Heuristic hint (not authoritative):"`（**强制**）|
| **不做** | 任何决策——只做 prompt augmentation 候选 |

---

## 3. 优先级热力图

| 模块 | 频率 | Phase | ROI |
|---|---|---|---|
| Working Memory | **4 次/任务** | 4.1 | 🔴 极高 |
| Failed Ligand Set | 每次生成 | 4.1 | 🔴 极高 |
| LoopController | 1 次/轮 | 4.1 | 🟠 高（边界）|
| HITL Checkpoint | 3 次/任务 | 4.1 | 🟠 高（合规）|
| Self-Reflection | 1 次/轮 | 4.2 | 🟡 中 |
| Planning | 1 次/任务 | 4.3 | 🟡 中 |
| ChemRAG | 按需 | 4.3 | 🟢 低（风险高）|

---

## 4. 终止条件（LoopController 的硬规则）

```python
class LoopController:
    def should_stop(self, state) -> tuple[bool, str]:
        if state.round >= self.max_rounds:
            return True, "max_rounds_reached"
        if state.tokens_used >= self.token_budget:
            return True, "token_budget_exceeded"
        if state.rounds_without_vina_improvement >= self.patience:
            return True, "judge_convergence_no_improvement"
        if state.hitl_veto:
            return True, "human_veto"
        return False, ""
```

**配置** (`config.yaml`)：

```yaml
loop:
  max_rounds: 5
  token_budget: 50000
  judge_convergence_patience: 2
  hitl:
    require_approval: true
    approval_points: ["start", "vina_breakthrough", "candidate_selection"]
```

---

## 5. HITL 检查点（合规边界）

| 触发 | 时机 | 提示用户 |
|---|---|---|
| **启动前** | loop 开始时 | "即将启动 5 轮 × 5 候选的迭代优化，确认？[y/n]" |
| **Vina 突破** | best_vina < 上一轮 best - 0.3 | "本轮发现 Vina -3.5（突破），继续还是停止？[y/n]" |
| **候选选择** | 闭环结束时 | "Top 3 候选如下，是否要加入合成白名单？[y/n each]" |

**禁止** LLM 直接说"这个分子值得合成"——必须人工确认。

---

## 6. 三阶段 Rollout

### Phase 4.1 — Foundation（1.5 天）

**目标**：Agent 有边界 + 能学习

- [ ] `agents/loop_controller.py`：3 个停止条件
- [ ] `agents/failed_set.py`：失败配体去重 + 持久化
- [ ] `agents/working_memory.py`：压缩历史 + 注入 generator
- [ ] `agents/hitl.py`：3 个检查点
- [ ] `memory/failed_ligands.json`：持久化文件
- [ ] `tests/test_phase4_1.py`：对照实验

**验收**：
- 跑 5 轮 A/B 对照：A=当前 loop / B=新 loop
- B 的 generator 输出**重复 SMILES 数 < A**
- B 的失败配体被重新生成次数 = 0
- 设 max_rounds=2 提前停，正确触发

### Phase 4.2 — Self-Reflection（1 天）

**目标**：Judge 评估自己的建议

- [ ] `agents/judge.py` 升级：输出 `reflection` + `confidence`
- [ ] meta-metric：judge 建议采纳率跟踪
- [ ] `tests/test_reflection.py`：reflection 质量评估

**验收**：
- Reflection 文本示例至少 10 条，人工评 80%+ 准确
- 建议采纳率 ≥ 60%

### Phase 4.3 — Long-term + RAG（2 天，可选）

**目标**：跨 session 记忆 + 检索增强

- [ ] `agents/planner.py`：多步 plan
- [ ] `agents/chem_rag.py`：PubChem REST，⚠ 前缀强制
- [ ] embeddings-based 失败配体检索（Phase 4.1 set 升级版）
- [ ] `memory/long_term_kb.json`

**验收**：
- ChemRAG 检索命中率 > 50%
- 所有输出带 "⚠ Heuristic" 前缀

---

## 7. 目录结构

```
aidd-multi-agent/
├── agents/
│   ├── llm.py                  # 已有
│   ├── generator.py            # 增强：注入 working_memory + failed_set
│   ├── evaluator.py            # 已有
│   ├── judge.py                # 增强：reflection 输出
│   ├── working_memory.py       # NEW
│   ├── failed_set.py           # NEW
│   ├── loop_controller.py      # NEW
│   ├── hitl.py                 # NEW
│   ├── planner.py              # NEW (4.3)
│   └── chem_rag.py             # NEW (4.3)
├── memory/
│   ├── failed_ligands.json     # NEW: 持久化失败 SMILES
│   └── best_molecules.json     # NEW: 持久化历史最佳
├── docs/
│   └── PHASE_4_PLAN.md         # 本文档
└── tests/
    └── test_phase4_1.py        # NEW
```

---

## 8. 与旧方案的关键差异（评审记录）

### ✅ 评审通过的部分
1. **单 Agent + 模块化**（不是 Orchestrator-Workers）：AIDD 是有界循环任务
2. **Working Memory 最高优先级**（4 次/任务）：迭代优化高频需求
3. **三阶段 rollout 节奏**：先 Working Memory → Reflection → Long-term
4. **Self-Reflection 独立 Judge**：对应 Reflexion 模式

### ⚠️ 评审修改的部分

| 原方案 | 评审修改 |
|---|---|
| ChemQ&A 用硬编码规则 | ❌ → **检索式 + ⚠ Heuristic 前缀**，降到 Phase 4.3 |
| Long-term Memory 推 Phase 4 | 半改：**失败配体朴素 set 提到 Phase 4.1**（零成本高 ROI）|
| 终止条件隐式（max_rounds: 5 默认）| ❌ → **显式 LoopController，3 个停止条件** |
| 无人工介入 | ❌ → **新增 HITL Checkpoint，3 个检查点** |

---

## 9. 工作量估算

| Phase | 时间 | 代码量 |
|---|---|---|
| 4.1 | 1.5 天 | ~600 行 |
| 4.2 | 1 天 | ~200 行 |
| 4.3 | 2 天 | ~800 行 |
| **总计** | **4.5 天** | ~1600 行 |

---

## 10. 验收矩阵

| 指标 | Phase 4.1 | Phase 4.2 | Phase 4.3 |
|---|---|---|---|
| 重复 SMILES 数 | ↓ 80%+ | 维持 | 维持 |
| 失败配体重生成 | = 0 | = 0 | = 0 |
| 终止条件触发 | 100% | 100% | 100% |
| HITL 触发 | 3/任务 | 维持 | 维持 |
| Reflection 准确率 | n/a | ≥ 80% | 维持 |
| Judge 建议采纳率 | n/a | ≥ 60% | ≥ 70% |
| ChemRAG 命中率 | n/a | n/a | ≥ 50% |

---

## 11. 风险与备选

| 风险 | 影响 | 备选 |
|---|---|---|
| Working Memory 注入过长 prompt | generator 截断 | 自动 truncate 到最近 3 轮 |
| Failed Ligand Set 误判失败 | generator 被束缚 | 阈值可调 + 失败带"reason"分类 |
| HITL 频繁打断 | 用户疲劳 | approval_points 可配置 |
| ChemRAG 误注权威 | 决策风险 | 强制前缀 + confidence 显示 |

---

## 12. 立即开始（Phase 4.1）

### Week 1 — Day 1
- 上午：`LoopController` + `FailedLigandSet`
- 下午：`WorkingMemory` + `HITL Checkpoint`

### Week 1 — Day 2
- 上午：集成到 `loop.py`，跑 5 轮 A/B 对照
- 下午：写 `test_phase4_1.py` 验收

**今日可完成** Phase 4.1 全部（1.5 天压缩）。

---

Last reviewed: 2026-09-13
Design philosophy: 单 Agent + 模块化（评审通过）
Next review: Phase 4.1 完成后