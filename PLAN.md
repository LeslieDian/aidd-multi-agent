# 项目实施路线图（PLAN.md）

> 本文档是 `aidd-multi-agent` 的开发路线图，按 5 个阶段推进，预计 **7–9 个工作日**完成 MVP。

---

## Phase 0：环境准备（半天）

### 目标
让 `python -c "import rdkit; from vina import Vina"` 不报错。

### TODO
- [ ] Python 3.10 + conda 环境
- [ ] `pip install rdkit-pypi meeko biopython` （基础化学库）
- [ ] AutoDock Vina 安装（conda-forge 或 GitHub release 二进制）
- [ ] LLM API Key：DeepSeek + Qwen（通义千问）
- [ ] EGFR 靶点文件下载（PDB: 1M17）
- [ ] 跑通：单分子 logP 计算 → 跑通一条 Vina docking

### 验证标准
```bash
python -c "from rdkit import Chem; print(Chem.MolFromSmiles('CCO'))"  # 应输出分子对象
vina --version  # 应输出版本号
```

---

## Phase 1：工具封装（1–2 天）⭐ 最关键

### 目标
实现 4 个**可独立调用、可单元测试**的工具函数，每个输入 SMILES 列表、输出 JSON 结构化评分。

### TODO
- [ ] `tools/validate_mol.py`
  - 输入：`SMILES` 单个或列表
  - 输出：`{valid, smiles, mw, logp, hbd, hba, sa_score, lipinski_pass}`
- [ ] `tools/admet_score.py`
  - 输入：SMILES 列表
  - 输出：TPSA / 旋转键 / hERG 风险 / CYP 抑制概率 / 综合 ADMET 分
- [ ] `tools/dock_score.py`
  - 输入：SMILES 列表 + 靶点 PDB 路径
  - 输出：Vina docking score (kcal/mol)，失败时返回 None
- [ ] `tools/diversity.py`
  - 输入：SMILES 列表
  - 输出：Bemis-Murcko 骨架集合 + 唯一骨架数 + 相似度矩阵
- [ ] `tests/test_tools.py` 覆盖 4 个工具，10 条已知药物 SMILES

### 验证标准
```bash
python tests/test_tools.py
# 预期：所有 4 个工具全部 PASS，无 ImportError / RuntimeError
```

### 风险点
- **Vina 算 SMILES → 3D → pdbqt** 链路长，Meeko + RDKit ETKDG 配合容易出岔
- **SA score** 需要 `sascorer.py` 文件（不是 pip 包），需手动放到 `tools/` 下

---

## Phase 2：2-Agent MVP（2–3 天）

### 目标
跑通**最小闭环**：Agent A 生成 → Agent B 评估 → 反馈给 A → 循环 5 轮。

### TODO
- [ ] `agents/generator.py` (Agent A)
  - system prompt："你是一名资深药物化学家..."
  - 输入：靶点描述 + 上轮反馈
  - 输出：5 条 SMILES + 设计理由（**强制 JSON 输出**）
- [ ] `agents/evaluator.py` (Agent B)
  - 不调 LLM，直接调用 Phase 1 的 4 个工具
  - 输出：每分子的结构化评分 + 综合排名
- [ ] `loop.py` 主循环
  - 串联 A + B，最多 5 轮
  - 每轮结果存 `runs/round_X.json`
- [ ] 双 LLM 支持：`DeepSeek-V3` + `Qwen2.5-72B`

### 验证标准
```bash
python loop.py --target EGFR --rounds 5 --output runs/
# 预期：runs/round_0.json ~ runs/round_4.json 全部生成
# 预期：合法率随轮次提升（至少看到趋势）
```

---

## Phase 3：扩展到 4-Agent（2 天）

### 目标
在小循环上加两个分支：
1. **生成器拆分**：`A1 (DeepSeek)` + `A2 (Qwen)` 并行生成 5 条/模型 = 10 条/轮
2. **裁判 Agent C**：汇总 B1/B2/B3 分歧 → 输出优化指令

### TODO
- [ ] `agents/judge.py` (Agent C)
  - 输入：B 报告 + 历史
  - 输出：分歧点识别 + 下一轮 prompt 增强建议
- [ ] `agents/generator_pool.py`
  - 并行调用 A1/A2
- [ ] `loop_v2.py` 支持 4-Agent 配置

### 验证标准
- 一次完整运行（10 条/轮 × 5 轮）能跑完不崩
- 两模型的 SMILES 重叠率 < 30%（说明异构生成有差异化）

---

## Phase 4：实验与图表（1–2 天）⭐ 简历素材

### 目标
3 组对比实验，每组一张图：

| 实验 | X 轴 | Y 轴 | 证明 |
|---|---|---|---|
| 合法率 vs 轮次 | 轮次 | 合法分子占比 | 反馈循环有效 |
| Vina / ADMET 打分 vs 轮次 | 轮次 | 平均得分 | 多目标优化有效 |
| 模型一 vs 模型二 骨架多样性 | — | Bemis-Murcko 数 | 异构生成有作用 |

### TODO
- [ ] `notebooks/01_legality_curve.ipynb`
- [ ] `notebooks/02_score_curve.ipynb`
- [ ] `notebooks/03_diversity_compare.ipynb`
- [ ] 导出 3 张 PNG 到 `docs/figures/`

---

## Phase 5：收尾（1 天）

### TODO
- [ ] README 完善（架构图 + 运行截图 + 实验结论）
- [ ] 添加 **失败案例**（哪些 prompt 改不动分、哪些 SMILES 跑不通 Vina）
- [ ] 添加 `docs/setup.md`（Vina / Meeko 安装详解）
- [ ] 写一份 `docs/architecture.md`（设计决策记录）

---

## 时间预算汇总

| 阶段 | 预计时长 | 累计 |
|---|---|---|
| Phase 0 | 0.5 天 | 0.5 天 |
| Phase 1 | 1–2 天 | 2.5 天 |
| Phase 2 | 2–3 天 | 5.5 天 |
| Phase 3 | 2 天 | 7.5 天 |
| Phase 4 | 1–2 天 | 9 天 |
| Phase 5 | 1 天 | 10 天 |

> 建议每个 Phase 结束就 commit 一次，留可回滚点。

---

## 风险与备选方案

| 风险 | 影响 | 备选 |
|---|---|---|
| LLM API 限流/断网 | 闭环跑不通 | 加 retry + 本地 mock LLM |
| Vina 安装失败 | Phase 1 卡住 | 用 DiffDock / GNINA 替代 |
| SA score 文件找不到 | validate_mol 报错 | 用 RDKit 的 `NumRotatableBonds` 替代 |
| DeepSeek 输出不合规 JSON | Agent A 解析失败 | 强 prompt + 二次修正 prompt |