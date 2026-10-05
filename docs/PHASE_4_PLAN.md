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

> 实际落地节奏：Phase 4.1 / 4.2 早已完成。Phase 4.3 的 RAG 改写为 calibrated ADMET / EVIDENCE 校准 / 立体化学（详见 README §15 / docs/PROJECT_HANDBOOK.md 第 41-42 章）。从 Phase 4.4 起下文描述实际落地。

---

### Phase 4.4 — 校准 + 可视化 + 立体化学（2026-09-27）✅ 已完成

**目标**：把"agent 是否自信过头"变成可测量；让"agent 学到什么"对人类可见；从 2D 升级到 3D 感知

- [x] `agents/agent_metrics.py`：第 4 类 EVIDENCE 规则 + 预测误差度量
- [x] `tools/calibrated_admet.py`：6 端点校准 ADMET（与 calibrated_hERG 同思路）
- [x] `scripts/visualize_memory.py`：4 类规则占比 + 校准散点 + 跨 run 趋势
- [x] `tools/validate_mol.py`：立体化学感知（R/S / cis-trans）
- [x] `scripts/audit_pool_vs_random.py`：把 `P(random beats agent)` 变成可重复审计

**验收**：
- 391 passed / 1 skipped（含 17 个新测试）
- README §15 timeline 上 2026-09-27 标 ✅

详细设计取舍见 [docs/PROJECT_HANDBOOK.md 第 41-42 章](PROJECT_HANDBOOK.md)。

---

### Phase 4.5 — Selection Operator Bridge（PARENTS 块，2026-09-27）✅ 已完成

**动机**（来自 [docs/REVIEW_MINIMAX_ADVICE_20260917.md](REVIEW_MINIMAX_ADVICE_20260917.md) Priority A-3）：

> 单 agent 的搜索 = "带脚手架的重采样"。证据：随机抽样胜过 agent 的概率 72.7%（60 个真实轮次 + 4000 次重采样）。加 token / 加轮次都不能修，必须给 generator 一个**显式的选择算子 + 结构起点**。

**目标**：把"evaluator 选出的 top-k safety-gated parents"显式注入 generator prompt，让搜索成为化学空间 walk。

- [x] `tools/mutate.py`：BRICSBuild + atom_subst + terminal_swap + parents 块（4 类化学算子）
- [x] `loop.py::_select_safety_pareto_parents`：排序键 `(safety_gate_pass, -pareto_rank, composite)`
- [x] `agents/generator.py`：USER_PROMPT_TEMPLATE 加 `__PARENTS__` 占位符
- [x] `format_parents_block(parents, k=3)`：稳定文本片段
- [x] `config.yaml::loop.parents_block_enabled: true` / `parents_k: 3`

**验收**：
- 所有 generator 共享同一组结构起点（heterogeneity 之外的另一个"shared anchor"）
- `parents_block_enabled` 不进 `protocol_id` 派生，可合法 cutoff 做 baseline 对照
- `tests/test_mutate.py` 通过（含 flaky BRICSBuild 抑制）

详细实现见 [docs/PROJECT_HANDBOOK.md 第 43 章](PROJECT_HANDBOOK.md)。

---

### Phase 4.6 — Multi-Agent 完整实施（2026-09-29 ~ 2026-09-30）✅ 已完成

**动机**：单 LLM 重复同温度 + 同 prompt 跑 N 遍不能扩展搜索，必须给 generator 结构上独立的伙伴（不同模型 / 不同 prompt_role / 不同温度 / 不同 few-shot）+ 多裁判投票 + 对抗辩论 + 专家路由。

**目标**：5 角色 + 13 阶段分批落地。

**13 阶段分批**：

| 阶段 | 范围 | 主要 commit |
|---|---|---|
| 1-3 | README + config + 协调核心（`agents/multi_agent.py`） | `954f318` 前置 |
| 4-5 | prompt 模板 + 对抗辩论（`debate.py`）+ 多 model provider 占位 | 同上 |
| 6 | API 验证 + 真实端到端 run | `954f318` |
| 7 | end-to-end wiring（per-gen focus + evaluate + multi-judge + debate） | 同上 |
| 8 | 完整 loop 跑通 + README 更新 | 同上 |
| 9 | Dashboard multi-agent 接入 | `4eb7310` |
| 10 | Generator re-call inside debate round | 同上 |
| 11 | GLM short-prompt path | 同上（GLM 后续退役） |
| 12 | A/B ablation runner | `66c5f2a` |
| 13 | Marketplace / N-of-N 投票 | `2ae0de6` |

**5 角色合约**：

| 角色 | 数量 | 个性 | 异构轴强制 |
|---|---|---|---|
| A 异构生成器 | ≥ 2 | 不同 model / prompt_role / temperature / few-shot | 至少一轴不同 |
| J 多裁判 | ≥ 2 | property / docking / synthesis 等独立视角 | 不同视角定义 |
| C 对抗批评家 | 1 | 多轮 push-back + evidence_id 强制 | ACCEPT 即终止 |
| E 评估器 | 1 | RDKit + calibrated 端点（非 LLM） | n/a |
| R 专家路由 | 1 | 按 state 选专家 prompt | n/a |

**验收**：
- A/B ablation runner（`scripts/run_phase4_6_ablation.py`）跑通，n=1 已落地（Phase 4.8 follow-up 需要 n≥3）
- 532 passed / 1 skipped（commit `f4aa534` 时基线）
- Chainlit 集成（commit `f4aa534`）：bridge 层 100% 单测覆盖

详细实现见 [docs/PROJECT_HANDBOOK.md 第 44 章](PROJECT_HANDBOOK.md)。

---

### Phase 4.7 — multi-agent 与 loop.py 安全网对齐（2026-10-04）✅ 已完成

**动机**：Phase 4.6 的 `run_multi_agent_loop` 真实运行**绕开了** `loop.py` 早已集成的几道安全网（WorkingMemory / FailedLigandSet / LoopController / manifest.json / EvaluationCache / DockingCache）。两条路径 A/B 不可比（cache 缺失偏移计时成本，不是算法差异）。

**目标**：把 multi-agent 路径拉齐到与 `loop.py` 等价的安全网。

**10 项改动**：

| 项 | 修复前 | 修复后 |
|---|---|---|
| `memory.compress_for_generator()` 进 generator prompt | 空字符串 | 真实策略链 + best-so-far |
| `failed_set.format_for_prompt()` 进 generator prompt | 空字符串 | 已失败 SMILES 列表 |
| 每轮 `memory.add_round()` + `failed_set.add_failed_many()` | 没接 | 与 `loop.py` 等价 |
| `LoopController.should_stop()` 前后检查 | 没接 | 跑 `safe_vina` 信号 |
| `manifest.json` 写出 | 没写 | schema_version=2 |
| `EvaluationCache` / `DockingCache` 复用 | 故意不接 | 接上 |
| `call_multi_generators` 并行 | 顺序（4 gen × 10-30s） | `ThreadPoolExecutor(max_workers=len(gens))` |
| `RoundFingerprint.from_history(progress_signal=...)` | 只看 all-candidate Vina | `safe_vina` 时看 safety-gated Vina |
| `judge_*` 模板（property/docking/synthesis） | 没有 | 加入；judge 不再误用 generator 模板 |
| `render()` fallback 路径 | 静默吞内容 | `log.warning(...)` |

**验收**：
- 560 passed / 1 skipped（commit `4409105`；+28 测试 vs Phase 4.6 末段）
- `agents/__init__.py::__version__` 0.4.6 → 0.4.7
- 与 `loop.run_loop()` 共享 protocol_id / cache / memory / controller（10 维对照见 [docs/PROJECT_HANDBOOK.md 第 45 章](PROJECT_HANDBOOK.md) §45.4）

**与 README 一致性**：
- README §1.3 列了同一组 10 行 before/after
- README §7 完整流程按这条改动后的 `run_multi_agent_loop` 源码写
- README §6.11 history 标 Phase 4.7 / 4.7.1（Chainlit 集成）

---

### Phase 4.8 候选（按 ROI 排序）

| # | 候选 | 工作量 | 价值 |
|---|---|---|---|
| 1 | n≥3 A/B 验证 multi-agent vs single-agent（真实 LLM judge） | ~5.5 min/run × 2 = 11 min | 科学价值最高 |
| 2 | 合并 `loop.py::run_loop` 与 `loop_multi_agent.py::run_multi_agent_loop` 为统一入口 | 1-2 天 | 架构整理 |
| 3 | deepseek-v3 / kimi-k3 真正接入 | 半天（拿到 API key 后） | 异构性增强 |
| 4 | 接入 `judge_*` 模板到 `judge_round`（需要重写解析） | 半天 | 5 角色合约真正闭环 |
| 5 | MiniMax-M3 judge 路径在 MockLLMClient 下的 confidence 占位修复 | 小改动 | A/B 验证可信度 |
| 6 | GLM 退役后 `prompt_mode: short` 真实用户案例 | 新 key + 半天 | reasoning-heavy 模型兜底 |

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

## 13. 当前状态与下一步

**当前**：Phase 4.1 / 4.2 / 4.3 / 4.4 / 4.5 / 4.6 / 4.7 **全部 ✅ 已完成**。对应 README §15 时间线：

| 时间 | Phase | 描述 |
|---|---|---|
| 2026-09-13 | 0 | 环境准备 + RDKit / Vina / MiniMax 验证 |
| 2026-09-13 | 1 | 工具封装（validate_mol / admet_score / dock_score / diversity） |
| 2026-09-15 | 1.5 | 第一阶段可信度修复（PHASE_1_CREDIBILITY.md） |
| 2026-09-17 | 2 | 2-Agent MVP 闭环（generator → evaluator） |
| 2026-09-18 | 2.5 | Persistent Harness（agent_task.py / Dashboard）+ RuleStore 4 类 |
| 2026-09-19 | 3 | 多臂 A/B/C 消融（benchmarks/real_ablation_v3_20260915） |
| 2026-09-19 | 3.5 | Confirmatory 实验（do_not_approve 决策） |
| 2026-09-20 | v4 | 修复 transport + 真实端到端 multi-agent |
| 2026-09-27 | 4.4 | 记忆校准 + 内存可视化 + 立体化学 + ADMET 扩展 |
| 2026-09-27 | 4.5 | Selection Operator Bridge（PARENTS 块） |
| 2026-09-29 | 4.6 | **完整 Multi-Agent 实施**（13 stages） |
| 2026-09-30 | 4.6.1 | Chainlit 对话式前端 + GLM 退役 + DeepSeek 接入 + Marketplace 接入 |
| 2026-10-04 | **4.7** | **multi-agent 与 loop.py 安全网对齐**（commit `4409105`） |

**下一步**：Phase 4.8 候选清单见 §6 Phase 4.8 表格。最关键的两项：

1. **n≥3 A/B 验证 multi-agent vs single-agent**：cache + manifest + safe_vina 都已铺好，直接跑真 LLM 重复实验
2. **把 `judge_*` 模板接进 `judge_round`**：5 角色合约真正闭环

---

Last reviewed: 2026-10-04 (Phase 4.7 commit 4409105)
Design philosophy: 单 Agent + 模块化（Phase 4 评审通过）+ Multi-Agent 协作（Phase 4.6 实施，Phase 4.7 拉齐安全网）
Next review: Phase 4.8 实施后