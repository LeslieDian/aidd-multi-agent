# aidd-multi-agent

> **Multi-Agent Iterative Loop for AI-Driven Drug Design (AIDD)**
> LLM-generated molecules with RDKit / ADMET / Vina reflection.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Status](https://img.shields.io/badge/Status-Phase%203%20complete-blue)]()

---

## 项目目标

构建一个**多 Agent 协作的分子优化闭环**，针对靶点蛋白（默认 **EGFR / PDB: 1M17**）实现：

> **LLM 生成候选分子 → 多工具打分 → 评判反馈 → 优化再生成**

迭代 5–6 轮后输出最有潜力的化合物集合。

---

## 架构概览（4-Agent 协作）

```
┌──────────────────────────────────────────────────────────────┐
│  Orchestrator  (loop.py)                                     │
│                                                              │
│   ┌────────────┐    SMILES 列表    ┌──────────────┐          │
│   │  Agent A   │ ───────────────▶ │   Agent B    │          │
│   │ Generator  │                  │  Evaluator   │          │
│   │ (LLM 双模型)│                  │  (B1/B2/B3)  │          │
│   └─────▲──────┘                  └──────┬───────┘          │
│         │ 优化指令                       │ 评估报告           │
│         │                                ▼                    │
│         │                          ┌──────────────┐           │
│         └──────────────────────────│   Agent C    │           │
│            反馈 + 新一轮 SMILES     │    Critic    │           │
│                                    └──────────────┘           │
└──────────────────────────────────────────────────────────────┘
```

| Agent | 职责 | 实现 |
|---|---|---|
| **A 生成器** | 提候选分子 | DeepSeek + Qwen 双模型并行 |
| **B1 化学评估** | 合法性 / Lipinski / SA | RDKit + sascorer |
| **B2/B3 ADMET / Docking** | 多目标打分 | RDKit 描述符 + AutoDock Vina |
| **C 裁判** | 汇总分歧 + 输出策略 | LLM (强模型) |

---

## 快速开始

### 1. 环境准备

```bash
# 推荐使用 conda
conda create -n aidd python=3.10 -y
conda activate aidd

# 安装依赖
pip install -r requirements.txt

# Vina 需要单独装（详见 docs/setup.md）
conda install -c conda-forge vina  # 或从 GitHub release 下载二进制
```

### 2. 配置 API 密钥

```bash
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY / QWEN_API_KEY
```

### 3. 跑通最小工具测试

```bash
python tests/test_tools.py
```

预期看到 4 个工具全部 PASS。

---

## 项目结构

```
aidd-multi-agent/
├─── README.md            # 本文件
├─── PLAN.md              # 5 阶段实施路线图
├─── LICENSE              # MIT
├─── .gitignore
├─── requirements.txt     # Python 依赖
├─── config.yaml          # 靶点 / 模型 / 评分阈值配置
├─── tools/               # Phase 1: 4 个独立工具
│    ├─── validate_mol.py # SMILES 合法性 + Lipinski + SA score
│    ├─── admet_score.py  # ADMET 多目标打分
│    ├─── dock_score.py   # Vina 对接打分
│    ├─── diversity.py    # Bemis-Murcko 骨架多样性
│    └─── __init__.py
├─── agents/              # Phase 2-3: Agent 实现
│    └─── __init__.py
├─── loop.py              # Phase 2: 主循环
├─── runs/                # 每轮 JSON 输出
├─── tests/               # 单元测试
└─── notebooks/           # 数据分析与可视化
```

---

## 路线图（5 个阶段）

| Phase | 内容 | 周期 |
|---|---|---|
| **0** | 环境准备 + API Key | 半天 |
| **1** | 工具封装 + 单元测试 | 1–2 天 |
| **2** | 2-Agent MVP 闭环 | 2–3 天 |
| **3** | 扩展到 4-Agent | 2 天 |
| **4** | 实验与图表 | 1–2 天 |
| **5** | README 收尾 | 1 天 |

详细分解见 [PLAN.md](PLAN.md)。

---

## 关键设计原则

- **B1/B2/B3 严格走工具调用**，禁止靠 LLM 自由输出结构化信息（省 token、降错误率）
- **循环 ≤ 6 轮**——避免上下文爆炸 + token 成本失控
- **多专家独立打分**——降低单一模型偏差
- **异构生成**——双 LLM 并行提高骨架多样性

---

## 第一阶段可信度修复（2026-09-13）

已修复受体准备、对照身份、缺失评分、上一轮反思、Mock 隔离与测试污染。
详细变更与验证见 [PHASE_1_CREDIBILITY.md](docs/PHASE_1_CREDIBILITY.md)。

旧版样例与图表使用过未经核验的受体和错误标注的参考结构，保留为历史资料，
不能用于证明亲和力、筛选优越性或自主学习收益。旧版与新版分数不可直接比较。
搜索框大小没有通用的 kcal/mol 加减换算关系。

重新准备受体（输出必须不存在；如需重建，选择新版本名并更新配置）：

```bash
python scripts/prepare_receptor.py data/1M17.pdb data/prepared/1M17_v1.pdbqt
python scripts/validate_protocol.py --output runs/new_protocol_validation
```

离线验证和运行：

```bash
python -m pytest -q
python loop.py --mock --no-dock --rounds 3
```

不传 `--rounds` 和 `--n` 时，主循环读取 `config.yaml` 的
`loop.max_rounds` 与 `loop.candidates_per_round_per_generator`；命令行参数只用于显式覆盖。

受控实验请使用三档配置，避免开发阶段直接执行昂贵的完整 docking：

```powershell
python scripts/run_benchmark.py --profile smoke
python scripts/run_benchmark.py --profile screening
python scripts/run_benchmark.py --matrix experiments/confirmatory_matrix.yaml --profile confirmatory
```

并行 Vina、GPU embedding、两阶段漏斗和无望提前停止说明见
[`docs/EXPERIMENT_SPEEDUP.md`](docs/EXPERIMENT_SPEEDUP.md)。
实际采用的参数会写入每次运行的 `manifest.json`。

完整评估按 `protocol_id + canonical SMILES` 缓存在 `memory/evaluation_cache/`。
同一受体、口袋、评分代码和 Vina 参数下再次出现相同分子时，会复用原评分与 docking
工件，并在候选的 `evaluation_cache.source` 中记录首次评估来源。

工程收口阶段的配置、记忆隔离、去重和指标语义说明见
[PHASE_1_ENGINEERING_CLOSURE.md](docs/PHASE_1_ENGINEERING_CLOSURE.md)。

真实模式缺少密钥会报错，不再自动使用 Mock。真实运行与 Mock 均有独立运行 ID、
manifest、输入/模型记录和评估协议；Mock 不读取或写入正式失败记忆。
当 docking 或其他必要评分缺失时，`composite_score` 为 null，候选不进入完整评分排名。
`--no-dock` 仅产生初筛性质分，不宣称完成结合能力评估。

ADMET 当前仍为 RDKit 描述符启发式，Vina 仍为未校准的 docking 分数。
即使重对接通过，也不等价于实验活性验证。

长期存储建议使用 [PostgreSQL + pgvector](docs/DATABASE_SETUP.md)。当前第一阶段
输出仍为本地 JSON/SDF/PDBQT；尚未自动写入 PostgreSQL。

---

## 许可

MIT © 2026 LeslieDian
