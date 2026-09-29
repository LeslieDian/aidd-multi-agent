# aidd-multi-agent

> **Multi-Agent Iterative Loop for AI-Driven Drug Design**
>
> 异构生成器 + 多裁判投票 + 对抗辩论 + 专家路由 + Selection Operator Bridge
>
> 5 个独立智能体角色并行协作，针对 EGFR（PDB 1M17）跑真实的分子优化闭环。
> 不是 AutoGPT/ReAct 单 agent 循环；每轮并行 3 个模型、按 prompt_role 区分立场，
> 多裁判独立投票，对抗辩论 push-back，专家路由按短板动态切 prompt。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Status](https://img.shields.io/badge/Status-Phase%204.6%20Multi--Agent-blue)]()
[![Tools Version](https://img.shields.io/badge/tools-0.4.6-blue)]()

| 指标 | 值 |
|---|---|
| 离线测试 | **515 passed, 1 skipped**（最后 commit `66c5f2a`） |
| Generator | MiniMax-M3 + GLM-5.3-flash + Qwen-3.8-flash（全部 live 验证） |
| 端到端 multi-agent | 519.9 s / 1 round / 3 generators（live 跑通） |
| Selection operator | PARENTS block（Phase 4.5） |
| Adversarial debate | generator ↔ critic 多轮 push-back + evidence_id 强校验 |

---

## 目录

1. [项目是什么](#1-项目是什么)
2. [快速开始](#2-快速开始)
3. [项目结构](#3-项目结构)
4. [架构概览](#4-架构概览)
5. [关键设计原则](#5-关键设计原则)
6. [Multi-Agent 设计节](#6-multi-agent-设计节)
7. [API 验证状态](#7-api-验证状态)
8. [Local Dashboard / API](#8-local-dashboard--api)
9. [受控实验矩阵](#9-受控实验矩阵)
10. [测试基线](#10-测试基线)
11. [真实跑通证据](#11-真实跑通证据)
12. [路线图](#12-路线图)
13. [历史时间线](#13-历史时间线)
14. [许可](#14-许可)

---

## 1. 项目是什么

`aidd-multi-agent` 是一个**有状态的多 agent 协作分子优化闭环**，针对酪氨酸激酶 **EGFR（PDB: 1M17）** 跑真实的化学空间搜索。整个系统不是 AutoGPT/ReAct 风格的"单 LLM 循环 + 工具调用"，而是**真正并行 5 个智能体角色**：

```
A. 异构生成器（≥ 2 个）     — 不同 model / 不同 prompt_role / 不同 temperature 并行提案
J. 多裁判（≥ 2 个）         — property / docking / synthesis 等独立视角投票
C. 对抗批评家              — generator ↔ critic 多轮 push-back，必须给 evidence_id
E. 评估器                  — RDKit + Vina + calibrated ADMET（非 LLM）
R. 专家路由                — 按短板动态切 prompt（property_weak / vina_weak / SA）
```

外加 Phase 4.5 的 **Selection Operator Bridge（PARENTS 块）**：把 evaluator 选出的 top-k safety-gated parents（SMILES + vina + hERG + weakness）显式注入 generator prompt，让搜索成为"化学空间中的 walk"而不是 LLM re-roll 自己的训练分布。

### 1.1 一句话定义

> 真 multi-agent 协作：每轮并行 3 个模型（不同 prompt 立场），3 个独立视角的裁判投票，对抗辩论 push-back，专家路由动态切 prompt。所有 agent 调用、评估、聚合、辩论、路由都是**实测可跑的代码**，不是设计文档。

### 1.2 为什么不是单 agent

按 [docs/REVIEW_MINIMAX_ADVICE_20260917.md](docs/REVIEW_MINIMAX_ADVICE_20260917.md) Priority A-3 审计：

| 指标 | 单 agent 历史观察 | 含义 |
|---|---:|---|
| `P(随机抽样胜过智能体)` | 65.8% | 从候选池随机抽 8 个有 65.8% 概率胜过 agent |
| `P(当轮最佳 = 全局最佳)` | 70% | 多数轮次都在重新发现前几轮的同一个最佳 |
| `P(连续轮最佳 Tanimoto > 0.6)` | 40.6% | 多数相邻轮产物彼此不接近 |

这些数字**不能**靠"加更多 LLM token"或"加更多轮次"改进，只能靠"给 generator 一个显式的 selection operator + 多视角"。这是 PARENTS 块 + 多 agent 协作的设计依据。

---

## 2. 快速开始

### 2.1 环境准备

```bash
# 推荐用 conda
conda create -n aidd python=3.10 -y
conda activate aidd
pip install -r requirements.txt

# Vina 需要单独装（详见 docs/setup.md）
conda install -c conda-forge vina   # 或从 GitHub release 下载二进制
```

### 2.2 配置 API 密钥

```bash
cp .env.example .env
# 编辑 .env 填入 key：
#   MiniMax_API_KEY           (MiniMax Token Plan 1, 默认主用)
#   MiniMax_API_KEY_SECONDARY (MiniMax Token Plan 2, 429 fallback)
#   GLM_API_KEY               (Volcengine Coding Plan)
#   QWEN_API_KEY              (Alibaba Cloud Token Plan)
```

### 2.3 验证连接（必做）

```bash
python scripts/check_all_api_keys.py --out runs/samples/probe.json
# 期望：MiniMax / MiniMax-secondary / GLM / Qwen 4 个都 OK
```

### 2.4 第一次 multi-agent smoke

```bash
python scripts/smoke_multi_agent.py
# 期望：
#   multi_agent.on : True
#   n_generators   : 3
#   n_judges       : 3
#   router.enabled : True
#   Smoke OK.
```

### 2.5 真实跑

```bash
# A. 真 multi-agent 1 round（端到端）
python scripts/run_multi_agent_round.py --n-per-generator 2 \
    --out runs/samples/multi_agent_round_20260929.json

# B. 真 multi-agent 多 round（router → evaluate → multi-judge → debate → 下一轮）
python scripts/run_full_multi_agent.py --n-per-generator 1 --max-rounds 2 \
    --out runs/samples/multi_agent_full_20260929.json

# C. A/B 验证 multi-agent vs single-agent
python scripts/run_phase4_6_ablation.py --n-per-generator 1 --max-rounds 1 \
    --out runs/samples/phase4_6_ablation.json

# D. MiniMax 连通性（前置门槛）
python scripts/check_minimax_connectivity.py
```

### 2.6 本地 Dashboard（可选）

```bash
python agent_dashboard.py
# 打开 http://127.0.0.1:8765
# 在创建任务页面勾选 "multi-agent" 即可启用 multi-agent 模式
```

### 2.7 全量回归

```bash
python -m pytest -q
# 期望：515 passed, 1 skipped（含 1 个 flaky mutate 测试）
```

---

## 3. 项目结构

```
aidd-multi-agent/
├── README.md                  # 本文件
├── PLAN.md                    # 早期 5 阶段路线图（已 100% 完成，保留作为历史）
├── LICENSE                    # MIT
├── .env / .env.example        # API 密钥（gitignored）
├── requirements.txt
├── config.yaml                # 靶点 / 模型 / 评分阈值 / multi-agent 配置
│
├── agent_dashboard.py         # 本地 Web 控制台（127.0.0.1:8765）
├── agent_task.py              # CLI 入口：start / resume / pause / steer / constraints
├── api.py                     # FastAPI 入口（127.0.0.1:8766）
├── loop.py                    # Legacy single-agent 主循环
├── loop_multi_agent.py        # ★ Multi-agent 主入口（Phase 4.6 阶段 7+）
│
├── tools/                     # 确定性 / RDKit 工具层（Phase 4.5 起含 mutate）
│   ├── validate_mol.py        # SMILES 合法性 + Lipinski + SA + 立体化学
│   ├── admet_score.py         # ADMET 多目标打分（含 calibrated ADMET）
│   ├── calibrated_herg.py     # 多特征 logistic hERG 代理
│   ├── calibrated_admet.py    # 多端点校准 ADMET（Phase 4.4）
│   ├── dock_score.py          # AutoDock Vina 对接打分
│   ├── diversity.py           # Bemis-Murcko 骨架多样性
│   ├── mutate.py              # ★ Phase 4.5：selection operator (PARENTS / brics / atom_subst / terminal_swap)
│   ├── calibration_probe.py   # EVIDENCE 校准探针
│   ├── evaluation_cache.py / docking_cache.py / provenance.py / references.py
│   └── __init__.py            # __version__ = "0.4.6"
│
├── agents/                    # 智能体核心
│   ├── generator.py           # LLM 单步生成 + PARENTS block + SHORT prompt mode (Phase 4.6 阶段 11)
│   ├── evaluator.py           # 评估、帕累托、Pareto key
│   ├── judge.py               # 评审（multi-judge 兼容）
│   ├── llm.py                 # OpenAI 兼容客户端 + key swap fallback
│   ├── working_memory.py      # WorkingMemory（Phase 4.1）
│   ├── failed_set.py          # FailedLigandSet（Phase 4.1）
│   ├── rule_memory.py         # RuleStore 4 类（NEG/POS/CTX/EVIDENCE，Phase 4.4）
│   ├── agent_metrics.py       # 行为指标（Phase 4.3）
│   ├── loop_controller.py     # 显式终止（Phase 4.1）
│   ├── hitl.py                # Human-in-the-Loop 闸门
│   ├── redaction.py           # 凭据脱敏
│   ├── multi_agent.py         # ★ Phase 4.6：heterogeneity + dedup+聚合 + multi-judge vote + debate trigger
│   ├── router.py              # ★ Phase 4.6：RoundFingerprint + 专家路由规则
│   ├── debate.py              # ★ Phase 4.6：generator ↔ critic 多轮 push-back
│   ├── marketplace.py         # ★ Phase 4.6 阶段 13：trader budget + N-of-N 投票
│   ├── prompts/               # ★ Phase 4.6：prompt 模板注册（qed/vina/synth + 4 expert）
│   ├── __init__.py            # __version__ = "0.4.6"
│   └── harness/               # Persistent Harness（dashboard 的真正调度层）
│       ├── runtime.py / state.py / schema.py / tools.py
│       ├── editor.py / molecule_ops.py / planning.py
│       ├── attribution.py / evidence.py / screening.py
│       ├── reliability.py     # ClientScope / 重试 / 错误分类
│       └── dashboard.html / dashboard.py / presentation.py
│
├── db/                        # Repository（SQLite + 校验 + 索引）
├── experiments/               # ExperimentRunner + 矩阵 + 报告
│   ├── matrix.yaml / matrix_v4.yaml / confirmatory_matrix*.yaml / p3_signal_matrix.yaml
│   ├── contract.py / reporting.py / cross_version_compare.py
│   ├── profiles.yaml
│   └── ...
├── scripts/                   # 运行入口（CLI / runner / audit / calibration / multi-agent）
│   ├── run_benchmark.py / run_experiments.py / check_minimax_connectivity.py
│   ├── compare_2d_policies.py / compare_experiments.py / visualize_memory.py
│   ├── audit_pool_vs_random.py / validate_*.py / prepare_receptor.py
│   ├── check_all_api_keys.py                # ★ Phase 4.6：API 多 provider 探测
│   ├── smoke_multi_agent.py                # ★ Phase 4.6：offline smoke
│   ├── run_multi_agent_round.py            # ★ Phase 4.6：1 round 真实 multi-agent
│   ├── run_full_multi_agent.py             # ★ Phase 4.6：多 round router/eval/judge/debate
│   ├── run_phase4_6_ablation.py            # ★ Phase 4.6：single vs multi A/B 验证
│   ├── run_multi_agent_for_dashboard.py    # ★ Phase 4.6：dashboard worker
│   └── _build_*.py / _smoke_*.py / ...
│
├── benchmarks/                # 已运行实验产物（local, git ignored）
├── runs/                      # 当前会话运行目录（local, git ignored）
├── memory/                    # WorkingMemory / FailedLigandSet / RuleStore 持久化（local）
├── tests/                     # 单元测试（515 passed, 1 skipped）
└── docs/                      # 设计文档
    ├── PROJECT_HANDBOOK.md    # ★ 完整设计手册（chapter 41-42 是 Phase 4.5/4.6）
    ├── PHASE_4_PLAN.md         # Phase 4 设计方案
    ├── REVIEW_MINIMAX_ADVICE_20260917.md  # 触发 multi-agent 改造的关键审查
    ├── EXPERIMENT_MATRIX.md
    └── ...
```

---

## 4. 架构概览

### 4.1 流程图（multi-agent 模式）

```
┌────────────────────────────────────────────────────────────────────────┐
│                    Loop Orchestrator (loop_multi_agent.py)             │
│                                                                        │
│   for round in 1..N (LoopController 终止):                            │
│     ┌──────────────────────────────────────────────────────────────┐   │
│     │ A. 多生成器并行 (每轮)                                       │   │
│     │   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐           │   │
│     │   │  A1_qed      │  │  A2_vina     │  │  A3_synth    │           │   │
│     │   │  MiniMax-M3  │  │ glm-5.3-fls  │  │ qwen3.8-fls  │           │   │
│     │   │  prompt=qed  │  │ prompt=vina  │  │ prompt=synth │           │   │
│     │   │  temp=0.7    │  │  temp=1.0    │  │  temp=0.5    │           │   │
│     │   │  PARENTS ★   │  │  PARENTS ★   │  │  PARENTS ★   │           │   │
│     │   └──────┬──────┘  └──────┬──────┘  └──────┬──────┘           │   │
│     │          │ SMILES        │ SMILES        │ SMILES            │   │
│     │          ▼               ▼               ▼                    │   │
│     │   ┌──────────────────────────────────────────────────┐        │   │
│     │   │  Aggregation (Stage 7)                          │        │   │
│     │   │  - dedup by canonical SMILES                    │        │   │
│     │   │  - diversity floor Tanimoto > 0.7 → drop       │        │   │
│     │   │  - vote (per-generator confidence × weight)    │        │   │
│     │   │  - top_n cap                                    │        │   │
│     │   └─────────────────────┬────────────────────────────┘        │   │
│     └─────────────────────────┼─────────────────────────────────────┘   │
│                               ▼                                          │
│     ┌──────────────────────────────────────────────────────────────┐   │
│     │ B. 评估 (Stage 7)                                            │   │
│     │   - RDKit descriptors + ADMET calibrated_hERG calibrated_ADMET │   │
│     │   - safety_gate_pass (hERG + logP + Vina if available)       │   │
│     │   - aggregate_round outputs enriched + summary                  │   │
│     └─────────────────────┬────────────────────────────────────────┘   │
│                           ▼                                              │
│     ┌──────────────────────────────────────────────────────────────┐   │
│     │ C. 多裁判投票 (Stage 7)                                      │   │
│     │   J1_property    J2_docking    J3_synthesis                     │   │
│     │   score_p        score_d       score_s                          │   │
│     │   softmax(α_p·score_p + α_d·score_d + α_s·score_s) → next_round_focus │   │
│     │   weights default (0.4, 0.4, 0.2), configurable.              │   │
│     └─────────────────────┬────────────────────────────────────────┘   │
│                           ▼                                              │
│     ┌──────────────────────────────────────────────────────────────┐   │
│     │ D. 专家路由 (Stage 7)                                        │   │
│     │   if property_weak:        activate prompt_qed_expert          │   │
│     │   if vina_weak:            activate prompt_vina_expert         │   │
│     │   if sa_difficult:         activate prompt_sa_expert           │   │
│     │   else:                    activate prompt_exploit_expert       │   │
│     │   RoundFingerprint.from_history(property/vina/sa history)     │   │
│     └─────────────────────┬────────────────────────────────────────┘   │
│                           ▼                                              │
│     ┌──────────────────────────────────────────────────────────────┐   │
│     │ E. 对抗辩论触发 (Stage 7/10)                                  │   │
│     │   if max(judge_score) - median(judge_score) > 0.15            │   │
│     │      OR any judge confidence < 0.30:                          │   │
│     │     1. Pick highest-confidence judge's rationale as critic    │   │
│     │     2. debate_recall_generators(...) → inject as focus         │   │
│     │     3. Re-aggregate + re-evaluate (debate-revised pool)        │   │
│     │     4. Fold critique into next round's focus                    │   │
│     └─────────────────────┬────────────────────────────────────────┘   │
│                           │                                              │
│                           └→ 下一轮（router 用 best_property/vina 更新）   │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.2 角色矩阵

| 角色 | 个性 | 模型 / 配置（默认） | 实现位置 |
|---|---|---|---|
| **A1_qed** | property / ADMET / QED / SA / hERG | `MiniMax-M3` + `prompt_qed` + temp=0.7 | `agents/generator.py` |
| **A2_vina** | binding / scaffold / docking | `glm-5.3-flash`（short prompt）+ `prompt_vina` + temp=1.0 | `agents/generator.py` |
| **A3_synth** | synthesis / SA / commercial | `qwen3.8-flash` + `prompt_synth` + temp=0.5 | `agents/generator.py` |
| **J1_property** | property / QED / SA / hERG | `judge_MiniMax` + `prompt_judge_property` + temp=0.3 | `agents/judge.py` |
| **J2_docking** | Vina / binding mode / pose | `glm-5.3-flash` + `prompt_judge_docking` + temp=0.3 | `agents/judge.py` |
| **J3_synthesis** | synthesis / SA / commercial | `qwen3.8-flash` + `prompt_judge_synthesis` + temp=0.3 | `agents/judge.py` |
| **C (Critic)** | 多轮 push-back，必须给 evidence_id | `MiniMax-M3` + `debate_prompt` + temp=0.4 | `agents/debate.py` |
| **E (Evaluator)** | RDKit / Vina / calibrated ADMET（非 LLM） | n/a | `tools/evaluate_*` |
| **R (Router)** | 按当前状态选专家 prompt | 纯规则 | `agents/router.py` |

### 4.3 横向模块（与 agent 并列）

| 模块 | 职责 | 实现位置 |
|---|---|---|
| **WorkingMemory** | 短期上下文（最近 N 轮 + best-so-far） | `agents/working_memory.py` |
| **FailedLigandSet** | 跨 session 强制过滤已失败 SMILES | `agents/failed_set.py` |
| **RuleStore (4 类)** | NEGATIVE / POSITIVE / CONTEXT / EVIDENCE 规则 | `agents/rule_memory.py` |
| **LoopController** | 显式终止（max_rounds / token_budget / patience） | `agents/loop_controller.py` |
| **Persistent Harness** | 工具注册、状态机、原子检查点、暂停恢复、用户干预 | `agents/harness/runtime.py` |
| **ExperimentRunner** | 多臂 A/B/C + confirmatory + futility 早停 | `scripts/run_benchmark.py` |
| **Repository** | SQLite / 检查点 / 收据 / 评估缓存 | `db/repository.py` |
| **Dashboard / API** | 本地 Web UI（仅 127.0.0.1） | `agent_dashboard.py` / `api.py` |
| **Calibration** | EVIDENCE 规则的预测误差度量 | `agents/agent_metrics.py` + `tools/calibration_probe.py` |
| **Visualization** | 4 类规则占比 / 校准散点 / 跨 run 趋势 | `scripts/visualize_memory.py` |

### 4.4 Selection Operator Bridge（PARENTS 块）

每轮评估后，`_select_safety_pareto_parents()` 从历史 enriched pool 中选出 top-k `safety_gate_pass is True` 的候选，排序键为 `candidate_priority_key`（Pareto rank → composite score → 新候选优先）。然后 `format_parents_block()` 渲染为稳定文本片段：

```text
Local parents to mutate around (do NOT copy verbatim; make small
structural changes such as swapping a terminal group, replacing
an aromatic H with F/Cl, or rearranging BRICS fragments):

[PARENT 1] smiles="COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN1CCCCC1"
            vina=-9.50 hERG=0.18 weakness="low solubility"
[PARENT 2] smiles="..."
```

注入 generator 的 user prompt，让所有 generator 共享同一组显式结构起点。

---

## 5. 关键设计原则

1. **多角色，多模型，多 prompt 立场**：同一 round 内并行 A1/A2/A3 三个 generator（不同 model / 不同 prompt_role），强制结构性多样性而非"同温度 / 同 prompt 跑三遍"。
2. **多裁判独立投票**：J1/J2/J3 按 property / docking / synthesis 三个独立视角加权投票，不允许单一 LLM 视角垄断下一轮 focus。
3. **对抗辩论兜底**：judges 分歧大或 confidence 低时，generator ↔ critic 多轮 push-back，必须给出 evidence_id 才算 accept。
4. **专家路由按状态动态切换**：监测当前 round 短板（property 弱 / vina 弱 / SA 难），对应激活 `prompt_qed_expert` / `prompt_vina_expert` / `prompt_sa_expert` / `prompt_exploit_expert`。
5. **Aggregator 强制多样性 + 去重 + 投票**：canonical SMILES 去重；批内 Tanimoto > 0.7 的产物只留一个；per-generator confidence × judge 加权 = 最终 ranking。
6. **Selection Operator Bridge (PARENTS 块)**：把 evaluator 选出的 top-k safety-gated parents 注入**所有**生成器 prompt，让 multi-agent 共享同一组显式结构起点（[docs/REVIEW_MINIMAX_ADVICE_20260917.md](docs/REVIEW_MINIMAX_ADVICE_20260917.md) Priority A-3 实证：`P(随机胜过智能体)` ≈ 65.8% → PARENTS 块让搜索成为化学空间 walk 而不是 LLM re-roll）。
7. **LoopController 显式终止**：`max_rounds` / `token_budget` / `judge_convergence_patience` 三条独立预算；不再硬编码"≤ 6 轮"。
8. **Persistent Harness 是调度总线**：工具注册、状态机、原子检查点、暂停恢复、用户干预都在 `agents/harness/`；multi-agent 在它之上运行。
9. **Evaluator 严格走 RDKit / Vina / calibrated 端点**：性质计算非 LLM；ADMET 是多端点校准（含 calibrated_hERG / calibrated_ADMET）。
10. **诚实度量**：`safe_vina` / `safe_composite` 进度信号 + `random_gate` 防止"随机抽样胜过 multi-agent"时仍发布对比。
11. **GLM-5.3-flash 走 short-prompt 模式**：`prompt_mode: short` 触发极简 SYSTEM_PROMPT（无 references、无 SAR），避免 reasoning_tokens 烧光 max_tokens（实测 4091/4096）。
12. **不宣称 multi-agent 优于 baseline 的稳定结论**：没有 confirmatory 三条门控（`efficacy_supported ∧ stable_improvement ∧ safety_noninferior`）同时成立，不把任何 multi-agent 配置写进默认。

---

## 6. Multi-Agent 设计节

### 6.1 为什么必须是 multi-agent

单 LLM 循环在同一温度 + 同 prompt 下跑 N 遍，并不能扩展搜索 —— 它只是重复自身的 training distribution。"多角度"必须来自**结构上独立**的智能体（不同 prompt 立场 / 不同模型 / 不同温度 / 不同 few-shot 示例）。多裁判投票减少单一视角偏差。对抗辩论防止 generator "糊弄" critic。

### 6.2 五个角色的合约（必须满足，否则不算 multi-agent）

#### A：异构生成器（≥ 2 个，并行）

每个 generator 必须**至少在一个轴上**与其它 generator 不同（不同时视为伪多 agent）：

| 差异轴 | 例子 |
|---|---|
| 模型 | `MiniMax-M3` vs `glm-5.3-flash` vs `qwen3.8-flash` |
| Prompt 立场 | `prompt_qed` 偏 ADMET / `prompt_vina` 偏 docking / `prompt_synth` 偏合成可行性 |
| 温度 | temp=0.7 vs temp=1.0 vs temp=0.5 |
| Few-shot | 不同示例集 |

`config.yaml::generators[]` 列表是预留的扩展位（详见 6.6 配置层）。

#### J：多裁判投票（≥ 2 个，独立视角）

- **J1（property）**：评估 property_score / QED / SA / hERG
- **J2（docking）**：评估 Vina / binding / pose
- **J3（synthesis）**：评估合成路线 / SA / 商业可得性
- 投票产出 `next_round_focus`，加权和投票结果以 `multi_judge_vote.json` 落盘

加权公式（`experiments/reporting.py::_multi_judge_decision`）：

```text
next_focus = argmax_i  (α_p · score_p_i + α_d · score_d_i + α_s · score_s_i)
weights (α_p, α_d, α_s) 默认 (0.4, 0.4, 0.2)，可调。
```

#### C：对抗批评家（多轮 push-back）

- **触发条件**：judges 之间分歧大（max−median > 0.15）或任一 judge confidence < 0.30
- **流程**：generator 提案 → critic 反驳 + 要求 evidence ID → generator 修正 → 循环 K 次或 critic 接受
- **必须给出 evidence ID**（`h:<hypothesis_id>` 或 `p:<proposal_id>:<option_index>`），不允许"加油式反驳"
- **ACCEPT 关键词**立即终止辩论

#### E：评估器（非 LLM）

- RDKit 描述符、AutoDock Vina、calibrated_hERG、calibrated_ADMET
- 不允许 LLM 自由输出结构化性质评分

#### R：专家路由（按状态激活）

- `property_weak=True` → 激活 `prompt_qed_expert`
- `vina_weak=True` → 激活 `prompt_vina_expert`
- `sa_difficult=True` → 激活 `prompt_sa_expert`
- 全部 OK → 激活 `prompt_exploit_expert`（专注微调）

`RoundFingerprint.from_history()` 派生（默认 window=2）：
- property_weak: best has not improved over window
- vina_weak: best safe_vina has not decreased over window（Vina 越小越好）
- sa_difficult: any SA score > 4.0 threshold

### 6.3 Aggregation 层合约

```text
输入：A1.candidates + A2.candidates + ... + PARENTS_block
输出：aggregated_candidates (去重 + 多样性 + 投票排名)

1. canonical SMILES 去重（统一 RDKit canonical）
2. 多样性过滤：批内 Tanimoto > 0.7 时，保留 judge 加权得分高的
3. 投票排名：per_generator_confidence × judge_weighted_score = final_score
4. 截断到 N (config: aggregation.top_n) 进入 evaluate 阶段
```

`aggregate_candidates()` 输出 `(aggregated, stats)`，`stats` 含 `n_input / n_after_dedup / n_dropped_diversity / n_kept / per_generator_count`。

### 6.4 对抗辩论触发条件

```python
should_debate, reason = should_enter_debate(
    judge_verdicts,
    disagreement_threshold=debate_cfg["disagreement_threshold"],  # default 0.15
    confidence_floor=debate_cfg["confidence_floor"],            # default 0.30
)
# Triggers debate if EITHER:
#   - any judge has score < confidence_floor, OR
#   - max(judge scores) - median(judge scores) >= disagreement_threshold
```

辩论触发后，`debate_recall_generators()` 把 highest-confidence judge's rationale 作为 critic push-back 注入新一轮 generator prompt；返回的候选合并到原池后**重新聚合 + 重新评估**，最终 rank 可包含辩论修订。`debate_recall_generators()` 在 generator 失败时返回 `{}`，主循环优雅降级不崩溃。

### 6.5 GLM-5.3-flash 的 Short-Prompt 路径

实测发现 glm-5.3-flash 在长 prompt + json_mode 下把所有 max_tokens（4096）都用于 reasoning，`content=""`。
**修复方案**：`prompt_mode: short` 触发 `SHORT_SYSTEM_PROMPT`（~300 字，无 references、无 SAR），让 GLM 有足够 tokens 产出 JSON 输出。实测 3-generator multi-agent 1 round 端到端跑通（519.9 秒）。

### 6.6 配置层（`config.yaml`）

```yaml
loop:
  generators:
    - name: A1_qed
      provider: MiniMax
      model: MiniMax-M3
      prompt_role: qed           # 加载 agents/prompts/qed.j2
      temperature: 0.7
      weight: 1.0
    - name: A2_vina
      provider: glm_volcengine_coding
      model: glm-5.3-flash
      prompt_role: vina
      temperature: 1.0
      weight: 1.0
    - name: A3_synth
      provider: qwen_aliyun
      model: qwen3.8-flash
      prompt_role: synth
      temperature: 0.5
      weight: 1.0

  judges:
    - name: J1_property
      provider: judge_MiniMax
      prompt_role: judge_property
      temperature: 0.3
      weight: 1.0
    # + J2_docking / J3_synthesis 类似

  router:
    enabled: true
    experts:
      property_weak: prompt_qed_expert
      vina_weak:     prompt_vina_expert
      sa_difficult:  prompt_sa_expert
      ok:            prompt_exploit_expert

  debate:
    enabled: true
    max_rounds: 3
    disagreement_threshold: 0.15
    confidence_floor: 0.30
    require_evidence_id: true

  aggregation:
    dedup: canonical
    diversity_floor: 0.7   # Tanimoto > 0.7 → drop lower-ranked
    top_n: 12

  parents_block_enabled: true   # Phase 4.5 selection operator
  parents_k: 3
```

### 6.7 实测如何判断是不是真 multi-agent

| 检查 | 通过条件 |
|---|---|
| 至少 2 个 generator 并行调用同一 round | log 显示 `[multi_agent] round N: A1+A2+A3 parallel` |
| 至少 2 个 judge 独立投票 | `multi_judge_vote.json` 含 ≥ 2 个 judge_scores |
| Generator 与 critic 至少 1 次 push-back | log 含 `[debate] round X: debate_round=N` |
| 不同 generator 的 prompt 立场不同 | `config.yaml::generators[*].prompt_role` 至少 2 个不同值 |
| （可选）不同模型 | `config.yaml::generators[*].model` 至少 2 个不同值 |
| Aggregation 强制多样性 | log 含 `[aggregator] dropped X candidates due to Tanimoto > 0.7` |

### 6.8 不能说的（multi-agent 边界）

1. **不能**声称 multi-agent 已证明优于 single-agent —— A/B 真实跑（`scripts/run_phase4_6_ablation.py`）做了对比，但 judges confidence 在 MockLLMClient 下都是 0，真实 confidence 比较需要更长时间的真实 LLM judge 调用。
2. **不能**把"同一个 LLM 跑 N 遍"当成 multi-agent —— 必须是模型 / prompt / 温度 / few-shot 至少一轴不同。
3. **不能**让多裁判共享同一个 system prompt —— 三个 judge 必须各有独立的视角定义。
4. **不能**让 aggregation 退化为简单拼接 —— 必须有多样性过滤 + 投票加权，否则多样性被均值化。
5. **不能**用 multi-agent 配置绕过 confirmatory 门控 —— `efficacy_supported ∧ stable_improvement ∧ safety_noninferior` 仍适用。
6. 部署多模型增加 token 成本（每个 generator 平均 ~2-3K tokens / round）。如果 token 预算紧张，至少用 **同模型 + 多 prompt 立场** 作为最低门槛。

### 6.9 Marketplace / N-of-N 投票（Phase 4.6 阶段 13）

`agents/marketplace.py` 提供两个互补机制：

#### `allocate_budgets(candidates, total_budget, ...)`（trader 模型）

每个候选按 softmax(score) 分配下一轮 generator 调用预算。`min_weight=0.05` 防止低分候选被饿死；`sum(allocated) == total_budget` 严格守恒。

```python
allocations = allocate_budgets([
    {"smiles": "CCO", "score": 0.9},
    {"smiles": "CCN", "score": 0.7},
], total_budget=10)
# → [BudgetAllocation(smiles='CCO', weight=0.55, allocated=6),
#    BudgetAllocation(smiles='CCN', weight=0.45, allocated=4)]
```

#### `select_top_k_by_vote(candidates, k)`（N-of-N 投票）

每个候选按 ECFP4 Tanimoto 相似度给其它候选投票。结构相似的候选互相加权；不相似的互相抵消。**苯 + 苯酚**会互相加持超过**哌啶**（前者结构相近，后者孤立）。

```python
top, stats = select_top_k_by_vote([
    {"smiles": "c1ccccc1", "score": 1.0},       # benzene
    {"smiles": "c1ccc(O)cc1", "score": 1.0},    # phenol (similar)
    {"smiles": "CN1CCCC1", "score": 1.0},        # piperidine (dissimilar)
], k=1)
# → top[0].smiles in ("c1ccccc1", "c1ccc(O)cc1") (NOT piperidine)
```

### 6.10 复现命令

```bash
# 1) 离线 smoke：multi-agent 接线
python scripts/smoke_multi_agent.py

# 2) 跑 mutate 自带的离线一致性（无 LLM，无 docking）
python -c "from tools.mutate import offline_validation; print(offline_validation())"

# 3) 真 multi-agent 1 round
python scripts/run_multi_agent_round.py --n-per-generator 2 \
    --out runs/samples/multi_agent_round.json

# 4) 真 multi-agent 多 round（router → eval → multi-judge → debate → 下一轮）
python scripts/run_full_multi_agent.py --n-per-generator 1 --max-rounds 2 \
    --out runs/samples/multi_agent_full.json

# 5) A/B 验证 multi-agent vs single-agent
python scripts/run_phase4_6_ablation.py --n-per-generator 1 --max-rounds 1 \
    --out runs/samples/phase4_6_ablation.json

# 6) Dashboard multi-agent 模式
python agent_dashboard.py
# 在创建任务页面勾选 "multi_agent" 即可

# 7) 全量回归
python -m pytest -q
# 515 passed, 1 skipped
```

### 6.11 历史（multi-agent 章节）

- Phase 0-3（2026-09-13 之前）：单 agent loop，`loop.py` 主体
- Phase 4.1（2026-09-17 起）：WorkingMemory + FailedLigandSet + LoopController + HITL
- Phase 4.4（2026-09-27）：记忆校准 + 内存可视化 + 立体化学 + 安全口径指标 + 随机胜率门槛 + ADMET 扩展
- Phase 4.5（2026-09-27）：Selection Operator Bridge（PARENTS 块）
- **Phase 4.6（2026-09-29）**：Multi-Agent 完整实施
  - 阶段 1-3：README + config + 协调核心
  - 阶段 4-5：prompt 模板 + 对抗辩论 + 多 model provider 占位
  - 阶段 6：API 验证 + 真实端到端 run
  - 阶段 7：end-to-end wiring（per-gen focus + evaluate + multi-judge + debate）
  - 阶段 8：完整 loop 跑通 + README 更新
  - 阶段 9：Dashboard multi-agent 接入
  - 阶段 10：Generator re-call inside debate round
  - 阶段 11：GLM short-prompt path（3-generator multi-agent）
  - 阶段 12：A/B ablation runner
  - 阶段 13：Marketplace / N-of-N 投票

---

## 7. API 验证状态

| Key | 状态 | 端点 | 模型 |
|---|---|---|---|
| `MiniMax_API_KEY` | ✅ OK | `https://api.minimaxi.com/v1` | `MiniMax-M3` |
| `MiniMax_API_KEY_SECONDARY` | ✅ OK | `https://api.minimaxi.com/v1` | `MiniMax-M3` |
| `GLM_API_KEY` | ✅ OK | `https://ark.cn-beijing.volces.com/api/coding/v3` | `glm-5.3-flash` |
| `QWEN_API_KEY` | ✅ OK | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `qwen3.8-flash` |
| `DEEPSEEK_API_KEY` | ⚠️ **qwen-only** | 实际是 qwen3.8-flash 的另一个别名（同 key 字符串） |  |

`scripts/check_all_api_keys.py` 自动探测所有 `*_API_KEY` 环境变量，对每个 key 尝试一组候选 `(base_url, model)`，直到找到一个 OK（HTTP 200 + 有效 JSON）。输出每个 key 的 working endpoint 和所有 attempt 列表。

### 7.1 MiniMax-M3 的特殊处理

MiniMax-M3 在响应中把 reasoning 包裹在 `<think>...</think>` 块里再输出 JSON。`check_all_api_keys.py` 和 generator 解析都先 `re.sub` 掉 `<think>` 块再解析 JSON。

### 7.2 GLM-5.3-flash 的 short-prompt 必要性

GLM-5.3-flash 在长 prompt（5000+ 字符的 EGFR brief）下，4096 max_tokens 中 4091 用于 reasoning，`content=""`。配置 `prompt_mode: short` 后切到 `SHORT_SYSTEM_PROMPT`（~300 字符），正常产出 SMILES。

---

## 8. Local Dashboard / API

### 8.1 Dashboard

```bash
python agent_dashboard.py
# 打开 http://127.0.0.1:8765
```

特性：
- 创建任务时勾选 **multi_agent** 即启用 multi-agent 模式（要求 `config.yaml::loop.multi_agent.enabled=true`）
- multi-agent 模式会启动 `scripts/run_multi_agent_for_dashboard.py` 作为 worker，写日志到 `task_dir/multi_agent_log.json`
- Dashboard 主页和 snapshot 视图自动附加 multi-agent 日志（`view["multi_agent"]`）
- 单 agent 模式仍是 `agent_task.py resume` 路径

### 8.2 FastAPI

```bash
uvicorn api:app --host 127.0.0.1 --port 8766
```

端点：
- `POST /runs` — 创建任务（接受 `multi_agent: bool` 字段）
- `GET /runs/{task_id}` — 任务状态
- `GET /runs/{task_id}/candidates` — 候选列表
- pause / resume / cancel / approvals — 控制命令

⚠️ 仅绑定 127.0.0.1，不要暴露到公网（无鉴权）。

---

## 9. 受控实验矩阵

```bash
# 1) Offline 烟雾（< 1 秒）：验证 ExperimentRunner 端到端
python scripts/run_benchmark.py --profile smoke --mock --no-dock --benchmark-id smoke_check

# 2) 真实 A/B/C 消融（3 组 × 3 重复 × 3 轮 × 5 候选 ≈ 70 min）
python scripts/run_benchmark.py \
    --matrix experiments/matrix.yaml \
    --profile screening \
    --benchmark-id real_ablation_v4

# 3) Confirmatory：reflection vs reflection_memory（10 次重复 + futility 早停）
python scripts/run_benchmark.py \
    --matrix experiments/confirmatory_matrix_v4.yaml \
    --profile confirmatory \
    --benchmark-id confirmatory_v4

# 4) 中途挂掉？用 --resume 接着跑
python scripts/run_benchmark.py \
    --matrix experiments/confirmatory_matrix_v4.yaml \
    --profile confirmatory \
    --benchmark-id confirmatory_v4 \
    --resume

# 5) Phase 4.6 multi-agent vs single-agent ablation
python scripts/run_phase4_6_ablation.py --n-per-generator 1 --max-rounds 1 \
    --out runs/samples/phase4_6_ablation.json
```

报告写入 `<benchmark_dir>/benchmark_report.json` 与 `benchmark_report.md`，包含 Group summary、vs baseline 主对比、judge 投票分布、被排除 attempt 的原因、以及 confirmatory 决策。

---

## 10. 测试基线

```text
515 passed, 1 skipped   # 离线回归（最后 commit 66c5f2a）
```

跳过的 1 个是 `tests/test_mutate.py::test_mutate_deterministic_with_seed`，是 RDKit BRICSBuild 的随机性偶发问题（RDKit 内部顺序不完全确定），单独跑通常通过，与 multi-agent 改造无关。

测试增量（Phase 4.6 起）：
- `tests/test_multi_agent.py`（35 tests）：heterogeneity / aggregate / multi-judge vote / debate trigger / config validation
- `tests/test_loop_multi_agent.py`（18 tests）：config readers / maybe_route / call_multi_generators / aggregate_round / run_multi_agent_loop entry/exit
- `tests/test_loop_multi_agent_stage7.py`（9 tests）：build_per_generator_focus / maybe_debate / evaluate_aggregated_candidates
- `tests/test_prompts.py`（7 tests）：load / render / expert prompts
- `tests/test_debate.py`（16 tests）：extract_evidence_ids / validate_critic_turn / should_terminate / run_debate
- `tests/test_short_prompt.py`（6 tests）：SHORT_SYSTEM_PROMPT / prompt_mode 切换
- `tests/test_dashboard_multi_agent.py`（13 tests）：flag 接受 / snapshot / launch worker 选择
- `tests/test_debate_recall.py`（4 tests）：REVISION REQUEST 注入 / 失败降级 / 重新聚合
- `tests/test_marketplace.py`（16 tests）：allocate_budgets / select_top_k_by_vote

---

## 11. 真实跑通证据

### 11.1 Phase 4.6 stage 7-8：完整 multi-agent 2 rounds

```text
Multi-agent full loop: n_per_generator=1 max_rounds=2 mock=False

=== Round 0 ===
  [router] active expert: prompt_exploit_expert
  [aggregator] input=2 after_dedup=2 dropped_diversity=0 kept=2
  [evaluate] n_total=2 n_valid=2 best_property=None best_vina=None best_safe_vina=None
  [judges] combined=0.000 dispersion=0.000 vote=[(J1_property, 0.0), (J2_synthesis, 0.0)]
  [judges] next_focus: Replace the basic tertiary-amine propoxy tail ...
  [debate] round 0 trigger=low_confidence(min=0.000<0.3) critic=J1_property

=== Round 1 ===
  [router] active expert: prompt_exploit_expert
  [aggregator] input=2 after_dedup=1 dropped_diversity=0 kept=1
  [evaluate] n_total=1 n_valid=1
  [judges] combined=0.815 dispersion=0.035 vote=[(J1_property, 0.85), (J2_synthesis, 0.78)]
  [judges] next_focus: Replace the C7 2-methoxyethoxy on [0] with 3-morpholinopropoxy ...

Total wall time: 153.5 s
```

Round 0 judges 都返回 confidence=0（MiniMax-M3 的 judge 路径在 mock 模式下走 MockLLMClient 占位）；Round 1 真实 LLM 投票 confidence=0.85/0.78、dispersion=0.035，next_focus 是真实生成内容。

### 11.2 Phase 4.6 stage 11：3-generator multi-agent 1 round

```text
Multi-agent full loop: n_per_generator=1 max_rounds=1 mock=False
[multi_agent] 3 generators | 3 judges | max_rounds=1

=== Round 0 ===
  [router] active expert: prompt_exploit_expert
  [aggregator] input=3 after_dedup=2 dropped_diversity=0 kept=2
  [judges] combined=0.000 dispersion=0.000
  vote=[(J1_property, 0.0), (J2_docking, 0.0), (J3_synthesis, 0.0)]
  [debate] round 0 trigger=low_confidence
  [debate] re-aggregated: input=6 after_dedup=4 kept=3

Total wall time: 519.9 s
```

3 generators (MiniMax + GLM + Qwen) 都产生 SMILES，aggregator 去重保留 2，debate 触发后 re-aggregator 保留 3（包含辩论修订）。

### 11.3 Phase 4.6 stage 12：A/B ablation

```text
--- ARM 1: single-agent (1 generator, 1 judge) ---
  elapsed=12555.7 ms (12.6 s)

--- ARM 2: multi-agent (3 generators, 3 judges, debate) ---
  elapsed=683136.3 ms (11.4 min)

  - single-agent elapsed=12.6 s vs multi-agent elapsed=683.1 s (delta=670.6 s)
  - single-agent combined_score=0.0 vs multi-agent combined_score=0.0
  - multi-agent debate_triggered=True, single-agent=False
```

multi-agent 比 single-agent 慢 **54×**（3 generators + 3 judges + debate）。judges confidence 都为 0（MockLLMClient 占位限制）；真实 confidence 比较需要更长时间的真实 LLM judge 调用（Phase 4.7 follow-up）。

### 11.4 MiniMax 连通性（前置门槛）

```text
schema_version: 1
attempted: 3
successful: 3
clients_created: 1
client_closed_explicitly: true
retried_requests: 0
retried: false (sequence 1, 2, 3)
schema_valid: true (returned_sequence 1, 2, 3)
latency_ms: 1357.733, 847.44, 650.559
```

3/3 通过；任何分子实验前必须 3/3 通过。

---

## 12. 路线图

### 12.1 已完成（Phase 4.6 全 13 阶段）

- ✅ 多角色 multi-agent 协作（异构生成器 + 多裁判 + 对抗辩论 + 专家路由 + 评估器）
- ✅ Selection Operator Bridge（PARENTS 块，Phase 4.5）
- ✅ GLM-5.3-flash short-prompt 路径（Phase 4.6 stage 11）
- ✅ Dashboard multi-agent 接入（Phase 4.6 stage 9）
- ✅ Marketplace / N-of-N 投票（Phase 4.6 stage 13）
- ✅ A/B ablation runner（Phase 4.6 stage 12）
- ✅ End-to-end multi-agent loop 跑通（Phase 4.6 stage 7-8）

### 12.2 Phase 4.7 候选（按 ROI 排序）

1. **MiniMax-M3 judge 路径在 MockLLMClient 下的 confidence 占位修复**（小改动，A/B 验证可信度）
2. **Real A/B 验证 multi-agent vs single-agent**（科学价值最高，需要 n ≥ 3 repeats + 真实 LLM judge budget）
3. **Marketplace 接入 run_multi_agent_loop**（让 trader budget + N-of-N 投票真正影响下一轮）
4. **GLM-5.3-flash judge 路径同样走 short-prompt**（让 3-judge multi-judge 全部可用）
5. **deepseek-v3 / kimi-k3 真正接入**（需要新 API key，但 Aliyun Token Plan 已有 key 在 QWEN 上成功）

---

## 13. 历史时间线

保留旧的中部节作为时间线（不让外部读 README 的人失去上下文）。完整目录：

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
| 2026-09-29 | 4.6 | **完整 Multi-Agent 实施**（阶段 1-13） |

详细历史见 `docs/PROJECT_HANDBOOK.md`（chapter 41-42 是 Phase 4.5/4.6）、`docs/PHASE_4_PLAN.md`、`docs/REVIEW_MINIMAX_ADVICE_20260917.md` 和 `docs/EXPERIMENT_MATRIX.md`。

旧 README 标题"Multi-Agent Iterative Loop for AI-Driven Drug Design (AIDD)"保留作为原始项目定位；本 README 不声称回退到 single-agent。

---

## 14. 许可

MIT © 2026 LeslieDian

详见 [LICENSE](LICENSE)。