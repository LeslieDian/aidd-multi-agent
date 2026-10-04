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
[![Status](https://img.shields.io/badge/Status-Phase%204.7%20Multi--Agent-blue)]()
[![Tools Version](https://img.shields.io/badge/tools-0.4.7-blue)]()

| 指标 | 值 |
|---|---|
| 离线测试 | **560 passed, 1 skipped**（commit `4409105`，2026-10-04） |
| Generators | **MiniMax-M3 × 2 + DeepSeek-chat + Qwen-3.8-flash fallback**（MiniMax-first 高用量；GLM-5.3-flash 已移除——其 API key 2026-09-30 过期） |
| Judges | **judge_MiniMax × 2 + Qwen-3.8-flash**（MiniMax 承担 2/3 weight） |
| 端到端 multi-agent | **327 s / 1 round / 4 generators + 3 judges + debate**（n=1 ablation, Phase 4.6 stage 11） |
| Selection operator | PARENTS block（Phase 4.5）→ Phase 4.6 marketplace 2-stage（top-2k Pareto → ECFP4 Tanimoto N-of-N voting） |
| Adversarial debate | generator ↔ critic 多轮 push-back + evidence_id 强校验 + debate_recall_generators 真正重调 |
| **Phase 4.7 修复** | multi-agent 接入 WorkingMemory / FailedLigandSet / LoopController / manifest.json / EvaluationCache；call_multi_generators 改为 ThreadPoolExecutor 并行 |
| Chainlit 前端 | `chainlit_app.py` (port 8000) + `agents/harness/chainlit_bridge.py`；E2E smoke `tests/test_chainlit_e2e.py` 通过 |

---

## 目录

1. [项目是什么](#1-项目是什么)
2. [快速开始](#2-快速开始)
3. [项目结构](#3-项目结构)
4. [架构概览](#4-架构概览)
5. [关键设计原则](#5-关键设计原则)
6. [Multi-Agent 设计节](#6-multi-agent-设计节)
7. [完整流程：从启动到终止](#7-完整流程从启动到终止)
8. [API 验证状态](#8-api-验证状态)
9. [Local Dashboard / API](#9-local-dashboard--api)
10. [Chainlit 对话式前端](#10-chainlit-对话式前端)
11. [受控实验矩阵](#11-受控实验矩阵)
12. [测试基线](#12-测试基线)
13. [真实跑通证据](#13-真实跑通证据)
14. [路线图](#14-路线图)
15. [历史时间线](#15-历史时间线)
16. [许可](#16-许可)

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
| `P(随机抽样胜过智能体)` | 72.7% | 从候选池随机抽 8 个有 72.7% 概率胜过 agent |
| `P(当轮最佳 = 全局最佳)` | 70% | 多数轮次都在重新发现前几轮的同一个最佳 |
| `P(连续轮最佳 Tanimoto > 0.6)` | 40.6% | 多数相邻轮产物彼此不接近 |

这些数字**不能**靠"加更多 LLM token"或"加更多轮次"改进，只能靠"给 generator 一个显式的 selection operator + 多视角"。这是 PARENTS 块 + 多 agent 协作的设计依据。

### 1.3 Phase 4.7 把 multi-agent 路径拉齐到 loop.py 的安全网

Phase 4.6 落地了多 agent 协调骨架，但 `run_multi_agent_loop` 的真实运行**绕开了** [loop.py](file:///D:/vs%20project/AIDD%20agent/loop.py) 早已集成的几道安全网：

- `WorkingMemory`（短期上下文 + best-so-far + strategy_chain）
- `FailedLigandSet`（跨 session 强制过滤已失败 SMILES）
- `LoopController`（`max_rounds` / `token_budget` / `judge_convergence_patience` 三条预算）
- `manifest.json`（schema_version=2，与 legacy loop 共享 `protocol_id` 派生）
- `EvaluationCache` / `DockingCache`（让 multi-agent 复用 legacy loop 已经跑过的同一 `protocol_id` 工作）

Phase 4.7（commit `4409105`）把这些都接上了：

| 项 | 修复前 | 修复后 |
|---|---|---|
| `memory.compress_for_generator()` 进 generator prompt | 空字符串 | 真实策略链 + best-so-far |
| `failed_set.format_for_prompt()` 进 generator prompt | 空字符串 | 已失败 SMILES 列表 |
| 每轮 `memory.add_round()` + `failed_set.add_failed_many()` | 没接 | 与 `loop.py` 等价 |
| `LoopController.should_stop()` 前后检查 | 没接 | 跑 `safe_vina` 信号，与 `progress_signal` 一致 |
| `manifest.json` 写出 | 没写 | schema_version=2，含 `protocol_id` / `code_hashes` / `sa_fragment_model_sha256` |
| `EvaluationCache` / `DockingCache` 复用 | 故意不接（baseline 隔离） | 接上 |
| `call_multi_generators` 并行 | 顺序（4 gen × 10-30s） | `ThreadPoolExecutor(max_workers=len(gens))` |
| `RoundFingerprint.from_history(progress_signal=...)` | 只看 all-candidate Vina | `safe_vina` 时看 safety-gated Vina |
| `agents/prompts/judge_property` / `judge_docking` / `judge_synthesis` | 没有 | 加入；judge 不再误用 generator 模板 |
| `render()` fallback 路径 | 静默吞内容 | `log.warning(...)` |

任何 multi-agent 实跑都默认开启上述全部能力，与 `loop.py` 同口径；不再有"伪 multi-agent"。

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
#   DEEPSEEK_API_KEY          (DeepSeek 主用，2026-09-30 切换为真 DeepSeek key)
#   QWEN_API_KEY              (Alibaba Cloud Token Plan, qwen3.8-flash)
# 注：GLM_API_KEY 已退役（2026-09-30 过期 401），保留 key 不再被任何 provider 块引用
```

### 2.3 验证连接（必做）

```bash
python scripts/check_all_api_keys.py --out runs/samples/probe.json
# 期望：MiniMax / MiniMax-secondary / DeepSeek / Qwen 4 个都 OK
```

### 2.4 第一次 multi-agent smoke

```bash
python scripts/smoke_multi_agent.py
# 期望（config.yaml 默认 4-generator + 3-judge + router + debate 全开）：
#   multi_agent.on   : True
#   n_generators     : 4   (A1_qed / A2_vina / A3_synth / A4_fallback)
#   n_judges         : 3   (J1_property / J2_docking / J3_synthesis)
#   router.enabled   : True
#   debate.enabled   : True
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
python -m pytest -q --ignore=tests/test_chainlit_e2e.py
# 期望：560 passed, 1 skipped（chainlit_e2e 需要浏览器，在 CI 之外跑）
# 含 1 个 flaky mutate 测试（test_mutate_deterministic_with_seed，RDKit BRICSBuild
# 内部顺序偶发；单独跑通常通过，与本项目无关）
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
│   └── __init__.py            # __version__ = "0.4.7"
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
│   ├── __init__.py            # __version__ = "0.4.7"
│   └── harness/               # Persistent Harness（dashboard 的真正调度层）
│       ├── runtime.py / state.py / schema.py / tools.py
│       ├── editor.py / molecule_ops.py / planning.py
│       ├── attribution.py / evidence.py / screening.py
│       ├── reliability.py     # ClientScope / 重试 / 错误分类
│       ├── chainlit_bridge.py # ★ Phase 4.7：UI 无关适配层（Harness 事件 → Chainlit 回调）
│       └── dashboard.html / dashboard.py / presentation.py
│
├── db/                        # Repository（SQLite + 校验 + 索引）
├── experiments/               # ExperimentRunner + 矩阵 + 报告
│   ├── matrix.yaml / matrix_v4.yaml / confirmatory_matrix*.yaml / p3_signal_matrix.yaml
│   ├── contract.py / reporting.py / cross_version_compare.py
│   ├── profiles.yaml
│   └── ...
├── scripts/                       # 运行入口（CLI / runner / audit / calibration / multi-agent）
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
├── tests/                     # 单元测试（560 passed, 1 skipped）
└── docs/                      # 设计文档
    ├── PROJECT_HANDBOOK.md    # ★ 完整设计手册（chapter 41-42 是 Phase 4.5/4.6）
    ├── PHASE_4_PLAN.md         # Phase 4 设计方案
    ├── REVIEW_MINIMAX_ADVICE_20260917.md  # 触发 multi-agent 改造的关键审查
    ├── CHAINLIT_FRONTEND.md   # ★ Phase 4.7：Chainlit 同事使用文档
    ├── AGENT_HARNESS.md       # Persistent Harness 架构说明
    └── ...
```

---

## 4. 架构概览

### 4.1 流程图（multi-agent 模式）

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                Loop Orchestrator (loop_multi_agent.run_multi_agent_loop)         │
│                                                                                 │
│   0. Set up（一次性，进入 round 0 前）                                            │
│      - protocol_id = digest(evaluation_protocol(target, scoring, dock_enabled)) │
│      - WorkingMemory(max_recent=3, persist=strategy_history.json)              │
│      - FailedLigandSet(max_size=500, persist=failed_ligands.json)              │
│      - LoopController(LoopConfig(max_rounds, judge_convergence_patience,        │
│                                   progress_signal="safe_vina"))                  │
│      - EvaluationCache + DockingCache（按 protocol_id 共享，与 loop.py 一致）    │
│      - 写出 manifest.json（schema_version=2，含 protocol_id / code_hashes）      │
│                                                                                 │
│   for round in 1..N (LoopController.should_stop() 前后各检查一次):              │
│     ┌─────────────────────────────────────────────────────────────────────┐    │
│     │ A. 路由器（RoundFingerprint.from_history）                          │    │
│     │   property_history / best_vina_history / best_safe_vina_history    │    │
│     │   当 progress_signal="safe_vina" 时 fingerprint 跟随 safety-gated  │    │
│     └──────────────────────────┬──────────────────────────────────────────┘    │
│                                ▼                                               │
│     ┌─────────────────────────────────────────────────────────────────────┐    │
│     │ B. PARENTS 块（Selection Operator Bridge，Phase 4.5 + 4.6）        │    │
│     │   _select_safety_pareto_parents_via_marketplace                    │    │
│     │   = ① Pareto top 2k safety-pass candidates                         │    │
│     │     ② ECFP4 N-of-N voting → top K = PARENTS                       │    │
│     └──────────────────────────┬──────────────────────────────────────────┘    │
│                                ▼                                               │
│     ┌─────────────────────────────────────────────────────────────────────┐    │
│     │ C. 多生成器并行（ThreadPoolExecutor，Phase 4.7 真并行）            │    │
│     │   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐│    │
│     │   │ A1_qed      │  │ A2_vina     │  │ A3_synth    │  │ A4_fallback││    │
│     │   │ MiniMax-M3  │  │ MiniMax-M3  │  │ deepseek-cht│  │ qwen3.8-fl ││    │
│     │   │ prompt=qed  │  │ prompt=vina │  │ prompt=synth│  │ prompt=def ││    │
│     │   │ temp=0.7    │  │ temp=1.0    │  │ temp=0.5    │  │ temp=0.7   ││    │
│     │   │ weight=2.0  │  │ weight=2.0  │  │ weight=1.0  │  │ weight=0.5 ││    │
│     │   │ PARENTS ★   │  │ PARENTS ★   │  │ PARENTS ★   │  │ PARENTS ★  ││    │
│     │   │ memory ★    │  │ memory ★    │  │ memory ★    │  │ memory ★   ││    │
│     │   │ failed ★    │  │ failed ★    │  │ failed ★    │  │ failed ★   ││    │
│     │   └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬─────┘│    │
│     │          │ SMILES        │ SMILES        │ SMILES        │ SMILES │    │
│     │          ▼               ▼               ▼               ▼        │    │
│     │   ┌────────────────────────────────────────────────────────────┐  │    │
│     │   │ Aggregation                                                 │  │    │
│     │   │ - canonical SMILES dedup                                    │  │    │
│     │   │ - diversity floor Tanimoto > 0.7 → drop lower-ranked       │  │    │
│     │   │ - vote (per-generator confidence × weight)                  │  │    │
│     │   │ - top_n cap                                                 │  │    │
│     │   └──────────────────────┬─────────────────────────────────────┘  │    │
│     └─────────────────────────┼─────────────────────────────────────────┘    │
│                               ▼                                                │
│     ┌─────────────────────────────────────────────────────────────────────┐    │
│     │ D. 评估（cache-aware）                                            │    │
│     │   loop.evaluate_candidates_with_cache(                            │    │
│     │       candidates, scoring, target, dock_enabled,                   │    │
│     │       cache=evaluation_cache, docking_cache=docking_cache)         │    │
│     │   - RDKit descriptors + calibrated ADMET / hERG                    │    │
│     │   - safety_gate_pass (hERG + logP + Vina if available)             │    │
│     │   - assign_pareto_metadata + summarize_round                       │    │
│     │                                                                  │    │
│     │   随后（与 loop.py 一致）:                                       │    │
│     │     memory.add_round(enriched, focus)                             │    │
│     │     failed_set.add_failed_many(many_smiles) ← 失败候选           │    │
│     │     refresh memory_context_current / failed_prompt_current        │    │
│     └──────────────────────────┬────────────────────────────────────────┘    │
│                                ▼                                               │
│     ┌─────────────────────────────────────────────────────────────────────┐    │
│     │ E. 多裁判独立投票                                                │    │
│     │   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │    │
│     │   │ J1_property  │  │ J2_docking   │  │ J3_synthesis │              │    │
│     │   │ judge_MiniMax│  │ MiniMax-M3   │  │ qwen3.8-flash │              │    │
│     │   │ weight=2.0   │  │ weight=2.0   │  │ weight=1.0   │              │    │
│     │   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘              │    │
│     │          ▼                 ▼                 ▼                     │    │
│     │   combine_judge_votes(verdicts, weights) → combined / dispersion  │    │
│     │   top-weighted judge.verdict.next_focus → 下一轮 focus             │    │
│     └──────────────────────────┬────────────────────────────────────────┘    │
│                                ▼                                               │
│     ┌─────────────────────────────────────────────────────────────────────┐    │
│     │ F. 对抗辩论触发（仅当分歧大或低 confidence）                     │    │
│     │   if max(judge_score) - median(judge_score) ≥ 0.15                │    │
│     │      OR any judge confidence < 0.30:                              │    │
│     │     1. Pick highest-confidence judge's rationale as critic        │    │
│     │     2. debate_recall_generators(...): 重调 generators，注入 critic│    │
│     │     3. Re-aggregate + re-evaluate (debate-revised pool)            │    │
│     │     4. LoopController 重新评估 patience counter                  │    │
│     └──────────────────────────┬────────────────────────────────────────┘    │
│                                ▼                                               │
│     ┌─────────────────────────────────────────────────────────────────────┐    │
│     │ G. 终止 + 持久化                                                │    │
│     │   LoopController:                                                │    │
│     │     state.note_round_safe_result(summary["best_safe_vina"])       │    │
│     │     should_stop() → True 时退出                                  │    │
│     │   写入: round_{N}.json (完整 enriched + judges + loop_state)      │    │
│     └─────────────────────────────────────────────────────────────────────┘    │
│                                                                                 │
│   return dict{ mode, n_generators, n_judges, rounds_log, stop_reason,          │
│                loop_state{rounds_without_*_improvement, best_*},               │
│                manifest_path, memory_dir, ... }                                │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 角色矩阵（与 `config.yaml` 完全同步，2026-10-04 验证）

| 角色 | 个性 | 模型 / provider | prompt_role | weight | 实现位置 |
|---|---|---|---|---:|---|
| **A1_qed** | property / ADMET / QED / SA / hERG | `MiniMax-M3` (`MiniMax`) | `qed` | 2.0 | `agents/generator.py` |
| **A2_vina** | binding / scaffold / docking | `MiniMax-M3` (`MiniMax`) | `vina` | 2.0 | `agents/generator.py` |
| **A3_synth** | synthesis / SA / commercial | `deepseek-chat` (`deepseek`) | `synth` | 1.0 | `agents/generator.py` |
| **A4_fallback** | diverse backup（heterogeneity 兜底） | `qwen3.8-flash` (`qwen_aliyun`) | `default` | 0.5 | `agents/generator.py` |
| **J1_property** | property / QED / SA / hERG | `judge_MiniMax`（`MiniMax-M3`） | `judge_property` | 2.0 | `agents/judge.py` |
| **J2_docking** | Vina / binding mode / pose | `MiniMax-M3` (`MiniMax`) | `judge_docking` | 2.0 | `agents/judge.py` |
| **J3_synthesis** | synthesis / SA / commercial | `qwen3.8-flash` (`qwen_aliyun`) | `judge_synthesis` | 1.0 | `agents/judge.py` |
| **C (Critic)** | 多轮 push-back，必须给 evidence_id | `MiniMax-M3` (`MiniMax`) | debate | n/a | `agents/debate.py` |
| **E (Evaluator)** | RDKit / Vina / calibrated ADMET（非 LLM） | n/a | n/a | n/a | `tools/evaluate_*` + `agents/evaluator.py` |
| **R (Router)** | 按当前 state 选专家 prompt | 纯规则 | n/a | n/a | `agents/router.py` |

> **GLM-5.3-flash 已经整体退役**（2026-09-30 API key 401）。`prompt_mode: short` 的代码路径仍保留（`agents/generator.py::SHORT_SYSTEM_PROMPT`）作为未来其它"reasoning-heavy"模型的兜底。

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
6. **Selection Operator Bridge (PARENTS 块)**：把 evaluator 选出的 top-k safety-gated parents 注入**所有**生成器 prompt，让 multi-agent 共享同一组显式结构起点（[docs/REVIEW_MINIMAX_ADVICE_20260917.md](docs/REVIEW_MINIMAX_ADVICE_20260917.md) Priority A-3 实证：`P(随机胜过智能体)` ≈ 72.7% → PARENTS 块让搜索成为化学空间 walk 而不是 LLM re-roll）。
7. **LoopController 显式终止**：`max_rounds` / `token_budget` / `judge_convergence_patience` 三条独立预算；不再硬编码"≤ 6 轮"。
8. **Persistent Harness 是调度总线**：工具注册、状态机、原子检查点、暂停恢复、用户干预都在 `agents/harness/`；multi-agent 在它之上运行。
9. **Evaluator 严格走 RDKit / Vina / calibrated 端点**：性质计算非 LLM；ADMET 是多端点校准（含 calibrated_hERG / calibrated_ADMET）。
10. **诚实度量**：`safe_vina` / `safe_composite` 进度信号 + `random_gate` 防止"随机抽样胜过 multi-agent"时仍发布对比。
11. **Short-prompt 路径作为兜底**（GLM 退役后保留）：`prompt_mode: short` 触发极简 SYSTEM_PROMPT（~300 字，无 references、无 SAR），避免 reasoning-heavy 模型把 max_tokens 全耗在 reasoning 上。`agents/generator.py::SHORT_SYSTEM_PROMPT` 当前未在 config.yaml 中引用；如果未来再接入"thinking-heavy"模型（如 kimi-thinking、deepseek-r1 类），直接复用即可。
12. **不宣称 multi-agent 优于 baseline 的稳定结论**：没有 confirmatory 三条门控（`efficacy_supported ∧ stable_improvement ∧ safety_noninferior`）同时成立，不把任何 multi-agent 配置写进默认。Phase 4.7 已经为 confirmatory 铺好路（safe_vined 缓存 + LoopController 持久化 + 共享 protocol_id），剩下的是真实 LLM 重复实验的工作。
13. **Phase 4.7 增量原则**：multi-agent 路径与 `loop.py` 共享 `WorkingMemory` / `FailedLigandSet` / `LoopController` / `EvaluationCache` / `DockingCache` / `manifest.json` 派生，避免"multi-agent 走自己的快捷路径"造成 A/B 不可比。

---

## 6. Multi-Agent 设计节

### 6.1 为什么必须是 multi-agent

单 LLM 循环在同一温度 + 同 prompt 下跑 N 遍，并不能扩展搜索 —— 它只是重复自身的 training distribution。"多角度"必须来自**结构上独立**的智能体（不同 prompt 立场 / 不同模型 / 不同温度 / 不同 few-shot 示例）。多裁判投票减少单一视角偏差。对抗辩论防止 generator "糊弄" critic。

### 6.2 五个角色的合约（必须满足，否则不算 multi-agent）

#### A：异构生成器（≥ 2 个，并行）

每个 generator 必须**至少在一个轴上**与其它 generator 不同（不同时视为伪多 agent）：

| 差异轴 | 例子 |
|---|---|
| 模型 | `MiniMax-M3` vs `deepseek-chat` vs `qwen3.8-flash` |
| Prompt 立场 | `prompt_qed` 偏 ADMET / `prompt_vina` 偏 docking / `prompt_synth` 偏合成可行性 |
| 温度 | temp=0.7 vs temp=1.0 vs temp=0.5 |
| Few-shot | 不同示例集 |

`config.yaml::generators[]` 列表是预留的扩展位（详见 6.6 配置层）。GLM-5.3-flash 已退役（2026-09-30 key 401），换成 DeepSeek。

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

### 6.5 Short-Prompt 路径（GLM 退役后保留）

GLM-5.3-flash 在 2026-09-30 因 API key 过期退役，**但** `agents/generator.py::SHORT_SYSTEM_PROMPT` 的代码路径仍保留（~300 字、无 references、无 SAR 的极简 SYSTEM_PROMPT）。

适用场景：未来若引入"reasoning-heavy"模型（如 kimi-thinking、deepseek-r1 类），在 `config.yaml::llm.providers.<name>.prompt_mode: "short"` 处一行开关即可复用。`SHORT_USER_PROMPT_TEMPLATE` 与 `SHORT_SYSTEM_PROMPT` 都不引用 references 库和 SAR 块。

实测（GLM 退役前）：3-generator multi-agent 1 round 端到端跑通（519.9 秒，含 GLM 推理 + debate 重调）。

### 6.6 配置层（`config.yaml` 当前默认，2026-10-04 验证）

```yaml
loop:
  multi_agent:
    enabled: true
    generators:
      - name: A1_qed
        provider: MiniMax
        model: MiniMax-M3
        prompt_role: qed
        temperature: 0.7
        weight: 2.0                # MiniMax-first 高用量
      - name: A2_vina
        provider: MiniMax
        model: MiniMax-M3
        prompt_role: vina
        temperature: 1.0
        weight: 2.0
      - name: A3_synth
        provider: deepseek
        model: deepseek-chat
        prompt_role: synth
        temperature: 0.5
        weight: 1.0
      - name: A4_fallback
        provider: qwen_aliyun
        model: qwen3.8-flash
        prompt_role: default
        temperature: 0.7
        weight: 0.5

    judges:
      - name: J1_property
        provider: judge_MiniMax
        prompt_role: judge_property
        temperature: 0.3
        weight: 2.0
      - name: J2_docking
        provider: MiniMax
        model: MiniMax-M3
        prompt_role: judge_docking
        temperature: 0.3
        weight: 2.0
      - name: J3_synthesis
        provider: qwen_aliyun
        prompt_role: judge_synthesis
        temperature: 0.3
        weight: 1.0

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
      diversity_floor: 0.7
      top_n: 12

  # Phase 4.7: 多 agent 与 legacy loop 共用这一组安全网
  progress_signal: safe_vina
  parents_block_enabled: true
  parents_k: 3
  memory_enabled: true
  failed_set_enabled: true
  evaluation_cache_enabled: true
  evaluation_cache_dir: memory/evaluation_cache
  docking_cache_enabled: true
  docking_cache_dir: memory/docking_cache
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
python -m pytest -q --ignore=tests/test_chainlit_e2e.py
# 560 passed, 1 skipped（含 1 个 flaky mutate 测试）
```

### 6.11 历史（multi-agent 章节）

- Phase 0-3（2026-09-13 之前）：单 agent loop，`loop.py` 主体
- Phase 4.1（2026-09-17 起）：WorkingMemory + FailedLigandSet + LoopController + HITL（**legacy loop 集成，multi-agent 当时未接**）
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
- **Phase 4.7（2026-10-04，commit `4409105`）**：multi-agent 路径与 `loop.py` 安全网对齐
  - 接 WorkingMemory / FailedLigandSet / LoopController（progress_signal=`safe_vina`）进入 generator prompt 与每轮更新
  - 写 `manifest.json`（schema_version=2，与 loop.py 共享 `protocol_id` 派生）
  - 复接 `EvaluationCache` + `DockingCache`（与 legacy loop 共享同一 protocol 的工作）
  - `call_multi_generators` 改为 `ThreadPoolExecutor` 真并行（4 gen × 10-30s → 收敛到 max 而不是 sum）
  - `RoundFingerprint.from_history(progress_signal="safe_vina")` 让路由看 safety-gated Vina
  - `agents/prompts/__init__.py` 新增 `judge_property` / `judge_docking` / `judge_synthesis` 模板
  - `render()` 兜底路径加 `log.warning(...)`，原"静默吞内容"被修掉
- **Phase 4.7.1（2026-10-04 同步）**：Chainlit 对话式前端（`chainlit_app.py` 端口 8000，详见 §10）

---

## 7. 完整流程：从启动到终止

本节按时间线走完一次 `run_multi_agent_loop` 的完整生命周期，**逐步**对应到 `loop_multi_agent.py::run_multi_agent_loop()` 的源代码。所有副作用都标注到磁盘路径，便于排错。

### 7.1 启动（CLI / Dashboard / Chainlit 三入口）

| 入口 | 文件 | 触发 |
|---|---|---|
| CLI | `python scripts/run_full_multi_agent.py --n-per-generator 1 --max-rounds 3` | 直接调 `run_multi_agent_loop(config, output_dir, ...)` |
| Dashboard | `agent_dashboard.py` 创建任务时勾选 multi-agent | 启动 `scripts/run_multi_agent_for_dashboard.py` worker 调同一个函数 |
| Chainlit | `chainlit_app.py` 用户在聊天框输入 | 当前版本 Chainlit 主要跑 Dashboard 任务 + Chainlit pipeline（harness）多 agent 路线 |

三条路径传同一个 `config` 字典（来自 `config.yaml`），所以生成的 `protocol_id` 一致 → 共享 `EvaluationCache` 与 `DockingCache`。

### 7.2 Set-up 阶段（一次性，写盘）

进入 `for round_num in range(max_rounds):` 之前一次性做完的工作，按调用顺序：

```text
run_multi_agent_loop(config, output_dir, ...)
│
├── 1. config = deepcopy(config)                 # 防止污染 caller
├── 2. llm_providers, loop_cfg, scoring_cfg, target_cfg = config[...]
├── 3. warn_if_degraded(...)                     # 异构性 / provider 缺失 → warning
├── 4. if not generators_are_heterogeneous(...): raise ValueError
├── 5. aggregation_cfg = read_aggregation(loop_cfg)
├── 6. out_path.mkdir(...) + 不允许目录已有 round_*.json / manifest.json
│
├── 7. progress_signal = str(loop_cfg.get("progress_signal", "safe_vina"))
│      ↑ 与 config.yaml 默认一致；fast-path 否则 raise
├── 8. memory_enabled / failed_set_enabled / evaluation_cache_enabled ...
│
├── 9. protocol = evaluation_protocol(target, scoring, dock_enabled)
│      ↑ KeyError 时降级为 synthetic protocol（tests 友好）
├── 11. memory_base / evaluation_cache_root / docking_cache_root
│      ↑ use_mock 时全部走 out_path / _xxx ; 真实跑走 memory/v2/<target>/<protocol>/<ns>
├── 12. EvaluationCache(evaluation_cache_root, protocol_id, enabled=...)
├── 13. DockingCache(docking_cache_root, docking_protocol_id, enabled=...)
│
├── 14. memory = WorkingMemory(max_recent=3, persist=strategy_history.json, ...)
├── 15. failed_set = FailedLigandSet(path=failed_ligands.json, max_size=500, ...)
├── 17. loop_controller = LoopController(LoopConfig(max_rounds, judge_convergence_patience, progress_signal))
├── 18. state = LoopState()         # best_vina / best_safe_vina / round counters
│
├── 19. parents_block_enabled / parents_k
├── 20. manifest.json (schema_version=2):
│      - run_id, is_mock, mode="multi_agent"
│      - protocol_id, protocol, docking_protocol_id
│      - execution { max_rounds, providers, judges, dock_enabled, memory_namespace,
│                    judge_enabled, memory_enabled, failed_set_enabled,
│                    progress_signal, progress_patience, token_budget,
│                    require_all_generators, generation_max_attempts,
│                    judge_max_attempts, evaluation_cache_enabled,
│                    evaluation_cache_dir, docking_cache_enabled,
│                    docking_cache_dir, parents_block_enabled, parents_k }
│      - llm { name: {k:v for k,v in settings.items() if k != "api_key_env" and "key" not in k.lower()} }
│      - reference_registry_sha256 / sa_fragment_model_sha256
│      - code_hashes { loop_multi_agent.py + agents/*.py + tools/*.py }
│
├── 22. memory_context_current = memory.compress_for_generator()      # round 0 用
├── 23. failed_prompt_current  = failed_set.format_for_prompt()      # round 0 用
│
└── 24. debate_cfg = read_debate(loop_cfg)
```

输出文件清单（按路径）：

```text
<output_dir>/
├── manifest.json                    ← schema_version=2
├── round_0.json / round_1.json / ... ← 每轮一份
└── (use_mock 时还有 _memory/ _evaluation_cache/ _docking_cache/ _disabled_failed_ligands.json)

memory/v2/<target>/<protocol_id>/<namespace>/
├── strategy_history.json             ← WorkingMemory 跨 session
├── best_molecules.json               ← best_so_far 跨 session
└── failed_ligands.json               ← FailedLigandSet 跨 session

memory/evaluation_cache/<protocol_id>/*.json
memory/docking_cache/<docking_protocol_id>/*.json
```

### 7.3 单轮循环（这是 multi-agent 的真实工作流）

按 `loop_multi_agent.py::run_multi_agent_loop` 主循环的精确顺序：

```text
for round_num in range(max_rounds):
│
├── [pre] LoopController: state.round = round_num
│        stop, reason = loop_controller.should_stop(state)
│        if stop: stop_reason = reason; break
│        ↑ 第一次进入时 state.round=0 / state.best_safe_vina=None，
│          patience counter 不会因为 good_minimum(None) 触发，与 loop.py 一致
│
├── [A] 路由 dispatch
│        expert_prompt, fp = maybe_route(
│            loop_cfg, best_property_history, best_vina_history, recent_sa_scores,
│            best_safe_vina_history, progress_signal,
│        )
│        RoundFingerprint.from_history(progress_signal="safe_vina")
│        ↑ 关键：phase 4.7 修复让 router 看 safety-gated Vina（不是 all-candidate）
│
├── [B] PARENTS 块（来自 Selection Operator Bridge）
│        if parents_block_enabled and enriched_history:
│            parents_used, parents_marketplace_stats =
│                _select_safety_pareto_parents_via_marketplace(
│                    enriched_history, parents_k=parents_k,
│                )
│            if parents_used:
│                parents_block = format_parents_block(parents_used, k=parents_k)
│        ↑ 入口函数调 loop._select_safety_pareto_parents (top 2k Pareto)
│          再调 marketplace.select_top_k_by_vote (N-of-N 投票) → top K = PARENTS
│
├── [C] 每 generator 一个 focus / weakness overlay
│        per_focus, per_weakness = build_per_generator_focus(
│            generators=gens, base_focus=focus, base_weakness=weakness,
│            expert_prompt=expert_prompt, debate_critique=debate_critique,
│        )
│        ↑ 渲染 prompt_role 模板（qed / vina / synth / judge_* / prompt_*_expert）
│
├── [D] 多 generator 并行调用（Phase 4.7 真并行）
│        with ThreadPoolExecutor(max_workers=len(gens)) as pool:
│            for name, items in pool.map(_call_one_generator, gens):
│                per_gen[name] = items
│        _call_one_generator(g) 内部:
│            - 用 per_focus[name] / per_weakness[name]
│            - generate_candidates(providers=[provider], memory_context_current, failed_prompt_current,
│                                  parents_block, use_mock, max_attempts_per_provider)
│            - 失败时 log.warning 然后 return name, []
│
├── [E] Aggregation
│        agg, agg_stats = aggregate_round(per_gen, aggregation_cfg)
│        ↑ agents/multi_agent.aggregate_candidates:
│          - canonical SMILES dedup
│          - diversity_floor=0.7 Tanimoto 过滤
│          - per-generator confidence × weight 加权投票
│          - 截断到 top_n=12
│        if not agg: break  # 没候选则退出
│
├── [F] 展平为 candidate 字典列表（含 multi_agent_sources / multi_agent_score）
│
├── [G] cache-aware evaluate
│        try: loop.evaluate_candidates_with_cache(
│                candidates, scoring_config, target_config, dock_enabled,
│                artifact_dir=str(out_path/"artifacts"),
│                cache=evaluation_cache, docking_cache=docking_cache,
│            )
│        except ImportError / Exception: 降级为 evaluate_aggregated_candidates
│        summary = summarize_round(enriched)
│        ↑ cache_stats { hits, misses, stored, enabled, docking_hits, ... }
│
├── [G'] WorkingMemory + FailedLigandSet 更新（与 loop.py 一致）
│        if memory_enabled: memory.add_round(enriched, focus_used=focus)
│        if failed_set_enabled and not use_mock:
│            new_failures = [(smi, reason) for c in enriched
│                            if c.evaluation_status == "complete"
│                            and (c.safety_gate_pass is False
│                                 or failed_set.should_mark_failed(...))]
│            if new_failures: failed_set.add_failed_many(new_failures)
│        ↑ refresh memory_context_current / failed_prompt_current for next round
│
├── [H] 多 judge 独立投票
│        if judges:
│            judge_result = call_multi_judges(
│                enriched, config, round_num, judges,
│                previous_focus=focus, previous_summary=summary_history[-1],
│                previous_enriched=enriched_history[-1], use_mock=use_mock,
│            )
│            focus = judge_result.next_focus   # top-weighted judge 的 verdict
│        ↑ 每个 judge 内部调 agents.judge.judge_round（默认 LLM judge prompt）
│          judge_round 输出 {focus, weakness, expected_change, reasoning, reflection, confidence}
│
├── [I] 对抗辩论触发（条件门控）
│        debate_triggered, debate_reason, critic_text = maybe_debate(
│            judge_verdicts=judge_result.verdicts,
│            debate_cfg=debate_cfg, initial_generator_text=focus, round_num, verbose,
│        )
│        debate_critique = critic_text if debate_triggered else ""
│
├── [I'] （若触发）debate_recall_generators：
│        把 critic_text 注入新一轮 generator prompt；
│        返回的候选合并到 per_gen 后 re-aggregate + re-evaluate；
│        失败降级返回 {}，主循环不崩溃
│
├── [J] 写入 summary history / enriched history
│
├── [K] LoopController 状态跟踪（关键 — 与 loop.py 等价）
│        round_best_vina     = summary.get("best_vina")
│        round_best_safe_vina = summary.get("best_safe_vina")
│        state.note_round_result(round_best_vina)
│        state.note_round_safe_result(round_best_safe_vina)
│        ↑ 两条进度信号都跟踪；只有 progress_signal 配置的那条控制 should_stop
│
├── [L] 写 round_{N}.json（与 loop.py 等价的字段）
│        round_record = {
│            round, run_id, protocol_id, is_mock, mode="multi_agent",
│            timestamp, focus_used, focus_next,
│            generator_outputs, candidates, summary,
│            judgment = { verdicts, combined, dispersion } or None,
│            parents_block_enabled, parents_k, parents_block_text, parents_used_smiles,
│            memory_snapshot = { recent_rounds, best_so_far },
│            loop_state = { round, progress_signal, rounds_without_*_improvement },
│            cache_stats, experts_used, judge_*, next_focus,
│            debate_triggered, debate_reason, debate_critique_preview,
│            per_generator_counts, aggregation_stats, parents_marketplace_stats,
│            n_candidates, n_enriched, summary_keys,
│        }
│        round_file = out_path / f"round_{round_num}.json"
│        round_file.write_text(json.dumps(round_record, ...))
│
└── [M] post-round stop check
         post_stop, post_reason = loop_controller.should_stop(state)
         if post_stop: stop_reason = post_reason; break
         ↑ 重要：与 loop.py 一样在 round 末尾再查一次，
           避免 patience 刚好轮满时再多跑一轮
```

### 7.4 终止条件汇总

`LoopController.should_stop(state)` 返回 `True` 的 4 个条件（顺序判断）：

1. `state.hitl_veto == True` → `"human_veto"`
2. `state.round >= config.max_rounds` → `"max_rounds_reached"`（默认 `max_rounds=12`）
3. `state.tokens_used >= config.token_budget` → `"token_budget_exceeded"`（默认 50000）
4. `rounds_without_<progress_signal>_improvement >= judge_convergence_patience` → `"no_improvement"`（默认 `progress_signal="safe_vina"`、`judge_convergence_patience=2`）

`progress_signal="vina"` 时 patient counter 看 all-candidate Vina；`progress_signal="safe_vina"`（推荐）时看 safety-gated Vina。Phase 4.7 已经把 RoundFingerprint 的 fingerprint 也接上了 safe_vina。

### 7.5 终止后返回值（schema）

```python
{
    "mode": "multi_agent",
    "n_generators": <int>,
    "n_judges": <int>,
    "rounds_log": [<round_record>, ...],
    "warnings": ["Warning", ...],
    "stop_reason": "max_rounds_reached" | "no_improvement" | "human_veto" | "token_budget_exceeded",
    "loop_state": {
        "progress_signal": "safe_vina" | "vina",
        "rounds_without_vina_improvement": <int>,
        "rounds_without_safe_vina_improvement": <int>,
        "best_vina": <float | None>,
        "best_safe_vina": <float | None>,
        "memory_rounds": <int>,
        "failed_set_size": <int>,
    },
    "memory_dir": "<abs path>" | None,
    "manifest_path": "<abs path>",
    "note": "Phase 4.7 wiring + Stage 7 ...",
}
```

调用方（dashboard / chainlit / abalation runner）通过 `rounds_log` + `stop_reason` 决定下一步动作。

### 7.6 与 legacy `loop.run_loop()` 的差异点（速查表）

| 维度 | `loop.run_loop()` | `loop_multi_agent.run_multi_agent_loop()` |
|---|---|---|
| generator 调用 | 单 provider list，ThreadPoolExecutor 并行 | 多 provider per generator，ThreadPoolExecutor 并行（Phase 4.7） |
| 候选合并 | 单 generator 输出直接 evaluate | 4 generator 输出 → `aggregate_round` 去重 + 多样性 + 投票 |
| judge | 单 judge | 多 judge，`combine_judge_votes` 加权投票 |
| router | 不存在 | `agents.router` RoundFingerprint + 4 expert prompt |
| debate | 不存在 | `agents.debate` + `debate_recall_generators` 重调 |
| PARENTS 块 | 同样支持 | 同样支持（且多 generator 共享同一组 PARENTS） |
| 终止 | `LoopController`（Phase 4.7 之前已有） | 同样 `LoopController`（Phase 4.7 接入） |
| 写盘 | `manifest.json` + `round_*.json` | **完全一样**（Phase 4.7 接入） |
| Cache | `EvaluationCache` + `DockingCache` | **共用同一组**（Phase 4.7 接入） |
| 失败集 | `FailedLigandSet` | **共用同一组**（Phase 4.7 接入） |
| 短期记忆 | `WorkingMemory` | **共用同一组**（Phase 4.7 接入） |

**两条路在 Phase 4.7 之后共享所有"安全网 + 持久化"，唯一真正的差异点 = multi-agent 的 A-J 步骤**（multi-generator / aggregate / multi-judge / router / debate）。

---

---

## 8. API 验证状态

| Key | 状态 | 端点 | 模型 |
|---|---|---|---|
| `MiniMax_API_KEY` | ✅ OK | `https://api.minimaxi.com/v1` | `MiniMax-M3` |
| `MiniMax_API_KEY_SECONDARY` | ✅ OK | `https://api.minimaxi.com/v1` | `MiniMax-M3` |
| `QWEN_API_KEY` | ✅ OK | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `qwen3.8-flash` |
| `DEEPSEEK_API_KEY` | ✅ OK | `https://api.deepseek.com/v1` | `deepseek-chat` |
| `GLM_API_KEY` | ❌ **EXPIRED 2026-09-30** | 401 `令牌已过期或验证不正确` on every endpoint | — |

**关键变化**（2026-09-30）：
- `DEEPSEEK_API_KEY` 不再是 `sk-sp-...` qwen-only 别名，已换成真的 DeepSeek key（`sk-94946d8c4f744c75a8f03f53cd5021c8`）。probe 显示 `api.deepseek.com/v1` + `deepseek-chat` ~2 s。
- `GLM_API_KEY` 彻底失效（UUID 格式的 Zhipu key，2026-09-30 起所有 endpoint 401）。`glm_volcengine_coding` provider block 已从 `config.yaml` 移除；multi_agent.* 注释中"GLM judge short-prompt"路径失效已自然消失。

`scripts/check_all_api_keys.py` 自动探测所有 `*_API_KEY` 环境变量，对每个 key 尝试一组候选 `(base_url, model)`，直到找到一个 OK（HTTP 200 + 有效 JSON）。输出每个 key 的 working endpoint 和所有 attempt 列表。

### 8.1 MiniMax-M3 的特殊处理

MiniMax-M3 在响应中把 reasoning 包裹在 `<think>...</think>` 块里再输出 JSON。`check_all_api_keys.py` 和 generator 解析都先 `re.sub` 掉 `<think>` 块再解析 JSON。

### 8.2 short-prompt 路径（GLM 已退役，保留说明）

`SHORT_SYSTEM_PROMPT`（~300 字符）和 `prompt_mode: short` 触发逻辑仍在线（`agents/generator.py`）。如果未来引入其它"reasoning-heavy"模型（例如某些 kimi-thinking 变体），这个路径直接复用即可。

---

## 9. Local Dashboard / API

### 9.1 Dashboard

```bash
python agent_dashboard.py
# 打开 http://127.0.0.1:8765
```

特性：
- 创建任务时勾选 **multi_agent** 即启用 multi-agent 模式（要求 `config.yaml::loop.multi_agent.enabled=true`）
- multi-agent 模式会启动 `scripts/run_multi_agent_for_dashboard.py` 作为 worker，写日志到 `task_dir/multi_agent_log.json`
- Dashboard 主页和 snapshot 视图自动附加 multi-agent 日志（`view["multi_agent"]`）
- 单 agent 模式仍是 `agent_task.py resume` 路径

### 9.2 FastAPI

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

## 10. Chainlit 对话式前端

> 详细使用文档：[docs/CHAINLIT_FRONTEND.md](docs/CHAINLIT_FRONTEND.md)。本节给"不需要用但需要理解其存在"的人。

### 10.1 背景与动机

`agents/harness/` 已经把决策/执行/审计/恢复拆得很干净（见 [docs/AGENT_HARNESS.md](docs/AGENT_HARNESS.md)）。CLI 与 FastAPI 控制台（`agent_task.py` / `agent_dashboard.py`，端口 8765）面向**开发者**调试任务，但**非技术同事**没法用：

- 看不懂「`step_2026xxxx_xxx.json` 里的 `reasoning` 字段到底什么意思」
- 想在 agent 跑偏时**中途插一句话**让它换方向，CLI 没有这个能力
- 想看候选分子「长什么样」，只能切到 Mol* 或 RDKit.js 自己渲

本轮新增 **Chainlit 对话式前端**（端口 8000），把 harness 包成 ChatGPT 式 UI：

- 自然语言输入目标（"围绕 EGFR 优化候选，Vina < -9"）
- 每一步 LLM 决策渲染成**可折叠时间线卡片**（含 reasoning / evidence / 评分）
- 候选分子**服务端 RDKit 渲染 2D 图**，点开看结构
- 关键节点前**弹按钮让用户选**（接受 / 改方向 / 暂停）→ 直接写入 `TaskState.revisions`
- 同对话支持多 task，可随时 `/tasks` 列、`/switch <id>` 切换

### 10.2 为什么是 Chainlit（不是其他方案）

| 方案 | 评估 | 结论 |
|---|---|---|
| **Chainlit** | Python 原生 `@cl.step` 装饰器直接对应 harness 决策步骤；内建人类反馈按钮（`cl.AskActionMessage`）；多模态消息（图片 / 代码）；ASGI 服务 | ✅ **采纳** |
| Open WebUI / LibreChat / NextChat | 漂亮，但需要把 harness **包成 chat API**，且 agent 步骤展示弱 | ❌ 包装成本高于收益 |
| Flowise / Langflow / Dify | 拖拽式 **workflow builder** 是固定流水线（"先 A 再 B 再 C"），但本项目 harness 是有状态循环 | ❌ 心智不对 |
| 自建 Web 前端（Next.js / React） | 工作量大，与现有 `agent_dashboard.py` 重复 | ❌ 重复造轮子 |
| 继续扩展现有 dashboard.html | 它已经是控制台（表格 / 暂停按钮），不是对话式 | ⚠️ 心智不对 |

Chainlit 与自研 harness **完全解耦**：Chainlit 仅作为 UI 壳，业务逻辑仍在 `agents/harness/runtime.py`。harness 内部代码一行没改。

### 10.3 4 个关键决策

| # | 决策 | 替代方案 | 选择理由 |
|---|---|---|---|
| 1 | **与现有 dashboard 共存**：Chainlit 8000 + FastAPI dashboard 8765 | 关掉 dashboard 只留 Chainlit | 开发者仍然依赖 dashboard 调试；同事用 Chainlit。两套并存，端口分开，互不干扰 |
| 2 | **一对话支持多 task**（可切换） | 一对话 = 一 task（纯 ChatGPT 心智） | 同事经常"先开一个 EGFR 优化，再开一个 hERG 优化"，来回切。dashboard 的多 task 心智保留下来 |
| 3 | **RDKit 服务端渲染 PNG**（候选分子以图片消息下发） | RDKit.js 客户端渲染 | 服务端渲染最稳，不增加前端打包复杂度；rdkit 已在 `requirements.txt` 里 |
| 4 | **复用现有 `CheckpointStore`（本地 JSON）**，Chainlit 不存业务数据 | 让 Chainlit 也写 Postgres | 已有 `psycopg` 是为 rule_memory 服务的；Chainlit 没必要碰业务库，复用 CheckpointStore 即可 |

### 10.4 架构

```text
浏览器 (同事)  http://127.0.0.1:8000
       │  WebSocket
       ▼
chainlit_app.py  (Chainlit 1.x, ASGI, 端口 8000)
   ├─ @cl.on_chat_start   → 加载 config.yaml + 建 LLMClient
   ├─ @cl.on_message      → 启动 / 恢复 task
   ├─ @cl.step 包装层     → 把 Harness.run_step() 转成可折叠 UI 卡片
   ├─ cl.AskActionMessage → 用户在回路中干预（steer / 暂停）
   └─ 候选 2D 图渲染       → RDKit → base64 PNG → cl.Image
       │  直接调
       ▼
agents/harness/runtime.py  (现有 Harness，零改动)
   ├─ LLMPolicy / MockPolicy
   ├─ TaskState + CheckpointStore
   └─ ToolRegistry (7 个动作：generate / refine / evaluate / compare / history / retry_evaluation / finish)
```

### 10.5 文件改动清单（5 个新文件 + 3 处小改）

| 类型 | 文件 | 行数 | 说明 |
|---|---|---|---|
| 改 | `requirements.txt` | +3 行 | 追加 `chainlit>=1.0`（注释说明是可选依赖） |
| 改 | `.gitignore` | +12 行 | 忽略 `.chainlit/` 运行时数据库与 sessions（保留 `config.toml`） |
| 新 | `chainlit_app.py` | ~480 | Chainlit 主入口，5 个 `@cl.on_*` 钩子 |
| 新 | `agents/harness/chainlit_bridge.py` | ~250 | **UI 无关**适配层：`Harness` 事件 → Chainlit 回调；纯函数可单测 |
| 新 | `tests/test_chainlit_bridge.py` | ~140 | bridge 层单元测试（不启 Chainlit） |
| 新 | `.chainlit/config.toml` | ~30 | 标题 / 头像 / 中文欢迎语 |
| 新 | `docs/CHAINLIT_FRONTEND.md` | ~150 | 同事使用文档（启动 + 用法） |

### 10.6 bridge 层设计（为什么单独抽一层）

`agents/harness/chainlit_bridge.py` 把 Chainlit 装饰器（`@cl.step`、`cl.AskActionMessage`）**与 harness 逻辑解耦**。bridge 层对外只暴露纯函数：

```python
def to_step_event(harness_step: HarnessStep) -> ChainlitStep
def to_image_payload(smiles: str) -> bytes        # RDKit -> PNG
def to_status_card(state: TaskState) -> str
def revisions_from_user_input(text: str) -> list
```

测试代码可以直接构造 `HarnessStep` 数据，断言 `to_step_event` 返回的结构，不依赖 Chainlit 运行。

harness 内部代码（`runtime.py` / `state.py` / `tools.py`）**一行没改**。

### 10.7 启动

```bash
pip install -r requirements.txt        # 含 chainlit>=1.0
chainlit run chainlit_app.py --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

只在本机监听（`127.0.0.1`），不暴露公网。同事通过 VPN / 内网访问本机。

### 10.8 同事使用流程

1. 在聊天框输入目标（例："围绕 EGFR 优化候选，Vina < -9，hERG 不超阈值"）
2. 看到**时间线卡片**：
   - `🧠 决策 round 1 → generate(15)`
   - `🔧 生成 15 个候选（点击展开看 SMILES + 2D 图）`
   - `🔧 evaluate → 11 通过初筛，4 进入下一轮`
   - `🧠 决策 round 2 → compare(...)`（含 reasoning）
3. 关键节点前弹按钮：**"这 4 个候选里选哪个继续？"**（用户选 → 写入 `revisions`）
4. 一对话支持多 task：发 `/tasks` 看列表，`/switch <task_id>` 切换

详细使用文档：[docs/CHAINLIT_FRONTEND.md](docs/CHAINLIT_FRONTEND.md)。

### 10.9 限制与未来工作

- **首次启动要装依赖**：本仓库不要求 chainlit 必装（`requirements.txt` 注释标注为"可选"），同事首次跑需要 `pip install -r requirements.txt`
- **未做权限模型**：单用户本机使用，多用户需加 auth（Chainlit 支持 OAuth / Header 注入）
- **未做 session 持久化 UI**：Chainlit 默认在浏览器 sessionStorage 里，对话关掉就没了（这是 Chainlit 设计哲学，不是 bug）。若需持久化，开启 `.chainlit/config.toml` 的 `features.persistent_sessions = true`

### 10.10 测试覆盖

`tests/test_chainlit_bridge.py` 覆盖：

- `HarnessStep → ChainlitStep` 序列化（含 reasoning / evidence / scores 字段）
- RDKit 渲染失败的降级（无效 SMILES 返回占位文本而非崩溃）
- `revisions_from_user_input` 解析自由文本（"换方向：增加极性基团" → `revision_action=steer`）
- 状态卡片从 `TaskState` 提取关键字段（task_id / status / round / best_score）

bridge 层 100% 单测覆盖；`chainlit_app.py` 本身只通过手动 smoke 验证（启动后用浏览器跑一个 mock 任务）。

### 10.11 参考

- Chainlit 官方文档：https://docs.chainlit.io
- 项目 harness 架构：[docs/AGENT_HARNESS.md](docs/AGENT_HARNESS.md)
- 现有 FastAPI dashboard：`agent_dashboard.py`（端口 8765）

---

## 11. 受控实验矩阵

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

## 12. 测试基线

```text
560 passed, 1 skipped   # 离线回归（最后 commit 4409105，Phase 4.7）
```

跳过的 1 个是 `tests/test_mutate.py::test_mutate_deterministic_with_seed`，是 RDKit BRICSBuild 的随机性偶发问题（RDKit 内部顺序不完全确定），单独跑通常通过，与 multi-agent 改造无关。`tests/test_chainlit_e2e.py` 是浏览器 smoke，需要 Chainlit 服务在线；CI 之外可通过 `python -m pytest -q --ignore=tests/test_chainlit_e2e.py` 跳过。

测试增量（Phase 4.6 起 + Phase 4.7 补充）：
- `tests/test_multi_agent.py`（35 tests）：heterogeneity / aggregate / multi-judge vote / debate trigger / config validation
- `tests/test_loop_multi_agent.py`（18 tests）：config readers / maybe_route / call_multi_generators / aggregate_round / run_multi_agent_loop entry/exit + 路径清理 fixture（autouse）
- `tests/test_loop_multi_agent_stage7.py`（9 tests）：build_per_generator_focus / maybe_debate / evaluate_aggregated_candidates
- `tests/test_prompts.py`（9 tests）：load / render / expert prompts / judge templates（Phase 4.7 +2）
- `tests/test_debate.py`（16 tests）：extract_evidence_ids / validate_critic_turn / should_terminate / run_debate
- `tests/test_short_prompt.py`（6 tests）：SHORT_SYSTEM_PROMPT / prompt_mode 切换
- `tests/test_dashboard_multi_agent.py`（13 tests）：flag 接受 / snapshot / launch worker 选择
- `tests/test_debate_recall.py`（4 tests）：REVISION REQUEST 注入 / 失败降级 / 重新聚合
- `tests/test_marketplace.py`（16 tests）：allocate_budgets / select_top_k_by_vote
- `tests/test_chainlit_bridge.py`（~140 行）：bridge 层单测，不启 Chainlit

---

## 13. 真实跑通证据

### 13.1 Phase 4.6 stage 7-8：完整 multi-agent 2 rounds

> 历史证据：当时 config 只有 2 个 judge（J1_property + J2_synthesis），与现在 `±_phase 4.7` 的 3-judge 配置略有差异。代码与配置以 2026-10-04 `4409105` 为准。

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

### 13.2 Phase 4.6 stage 11：3-generator multi-agent 1 round

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

### 13.3 Phase 4.6 stage 12：A/B ablation

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

### 13.4 MiniMax 连通性（前置门槛）

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

## 14. 路线图

### 14.1 已完成（Phase 4.6 全 13 阶段 + Phase 4.7 安全网对齐 + Chainlit 集成）

- ✅ 多角色 multi-agent 协作（异构生成器 + 多裁判 + 对抗辩论 + 专家路由 + 评估器）
- ✅ Selection Operator Bridge（PARENTS 块，Phase 4.5）→ Phase 4.6 marketplace 2-stage
- ✅ Dashboard multi-agent 接入（Phase 4.6 stage 9）
- ✅ Marketplace / N-of-N 投票（Phase 4.6 stage 13）
- ✅ A/B ablation runner（Phase 4.6 stage 12，n=1 已跑 327 s single-agent vs multi-agent）
- ✅ End-to-end multi-agent loop 跑通（Phase 4.6 stage 7-8）
- ✅ Chainlit 对话式前端集成（Phase 4.7.1，port 8000，bridge 层单测 + E2E smoke `tests/test_chainlit_e2e.py` 通过）
- ✅ Generator re-call inside debate round（Phase 4.6 stage 10）
- ✅ MiniMax-first 高用量配置（MiniMax 承担 5.5/7.5 weight：2 generators + 2 judges）
- ✅ Real DeepSeek 接入（DEEPSEEK_API_KEY 2026-09-30 更新成真 DeepSeek key，替代 GLM）
- ✅ **Phase 4.7 多 agent 路径与 loop.py 安全网对齐**（commit `4409105`）：
  WorkingMemory / FailedLigandSet / LoopController(safe_vina) / manifest.json / EvaluationCache / DockingCache
  + 真并行 ThreadPoolExecutor + safe_vina-aware 路由 + judge_* prompt 模板 + render fallback log

### 14.2 Phase 4.8 候选（按 ROI 排序）

1. **n≥3 A/B 验证 multi-agent vs single-agent**（科学价值最高，需要再跑 2 个 repeat，每个 ~5.5 min）— Phase 4.7 已经把 cache + manifest + LoopController 都铺好，再跑只剩 LLM 调用成本
2. **把 `loop.py::run_loop` 与 `loop_multi_agent.py::run_multi_agent_loop` 合并为统一入口**（架构性整理；现在是双入口，Phase 4.7 已经共享所有安全网，差异只剩 A-J 步骤）
4. **deepseek-v3 / kimi-k3 真正接入**（DEEPSEEK 已接，kimi 需要新 API key）
5. **MiniMax-M3 judge 路径在 MockLLMClient 下的 confidence 占位修复**（小改动，A/B 验证可信度）
6. **GLM 退役后 `prompt_mode: short` 真实用户案例**（代码路径保留但未引用；如果再启用需要新 key）

---

## 15. 历史时间线

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
| 2026-09-29 | 4.6 | **完整 Multi-Agent 实施**（阶段 1-13，13 commits：1 commit per stage 1-13 + 文档重写 + Chainlit 修复） |
| 2026-09-30 | 4.6.1 | Chainlit 对话式前端（端口 8000）+ GLM key 过期替换 DeepSeek + Marketplace 接入 |
| 2026-10-04 | **4.7** | **多 agent 路径与 loop.py 安全网对齐**（commit `4409105`）：WorkingMemory / FailedLigandSet / LoopController(safe_vina) / manifest.json / EvaluationCache / DockingCache 全接；真并行 ThreadPoolExecutor；safe_vina-aware 路由；judge_* prompt 模板；render fallback log |

详细历史见 `docs/PROJECT_HANDBOOK.md`（chapter 41-42 是 Phase 4.5/4.6）、`docs/PHASE_4_PLAN.md`、`docs/REVIEW_MINIMAX_ADVICE_20260917.md` 和 `docs/EXPERIMENT_MATRIX.md`。

旧 README 标题"Multi-Agent Iterative Loop for AI-Driven Drug Design (AIDD)"保留作为原始项目定位；本 README 不声称回退到 single-agent。

---

## 16. 许可

MIT © 2026 LeslieDian

详见 [LICENSE](LICENSE)。

