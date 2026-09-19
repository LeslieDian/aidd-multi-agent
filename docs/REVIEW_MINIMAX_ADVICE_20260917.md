# 对「MiniMax 诊断与优先级建议」的技术复核（2026-09-17）

> 复核范围：仓库当前代码（`agents/`、`tools/`、`config.yaml`）、`benchmarks/` 下全部真实运行产物、
> `docs/EXPERIMENT_RESULTS_20260916.md`、`docs/CONFIRMATORY_RESULTS_20260916.md`。
> 所有数字均可由 `scripts/audit_pool_vs_random.py` 与 `benchmarks/**/round_*.json` 复现。

## 0. 总体结论

MiniMax 的方向感是对的（**优化目标里安全权重不足 → hERG 回归**），但它引用的证据集**已被更严格的实验取代**，
它的事实判断有 **2 处明确错误**，它的第一优先级建议（跑 12 轮）在当前配置下**不会真正跑满 12 轮**，
而它的第 4 条建议（把 embedding 从 `report` 切成 `exclude`）**经实测会让搜索空间崩塌**。

更关键的是：**它和现有实验体系都漏掉了真正的首要根因。**

**首要根因（新发现，有数据支撑）：这个 loop 目前不是"搜索"，而是"带脚手架的重采样"。**

| 证据 | 数值 | 出处 |
|---|---|---|
| 单轮打分分子数（中位） | 8 | 本次审计 |
| 循环实测 mean best-of-round | **-8.324** | 60 个真实轮次 |
| 同一分子池随机抽 8 个的 mean best | **-8.441** | 4000 次重采样 |
| 随机采样胜过智能体的概率 | **72.7%** | 本次审计 |
| 3 轮后 best-safe-Vina 变差/变好 | **8 / 10（掷硬币）** | 20 次运行 |
| 第 0 轮最优 = 全程最优的运行占比 | **70%（14/20）** | 20 次运行 |
| 相邻两轮最优分子 Tanimoto | 均值 0.560，仅 **40.6%** ≥ 0.6 | 32 对 |
| 头号 Murcko 骨架占全池 | **31.0%**；top-5 占 52.8% | 216 个分子 |

**结论**：在当前实现下，"多 Agent 迭代"没有产出超过"从它自己生成的分布里随机抽样"的搜索结果。
因此**加轮数不会变好，它只会让一个零效应更精确**。这不是样本量问题，是**缺少选择算子**的问题。

---

## 1. 逐条复核 MiniMax 的 5 个"根本原因"

### ① 搜索空间太小（MiniMax 排第 1）→ **部分正确，但归因错误**

- 事实正确：单组预算确实小。
- 归因错误：真正的限制不是"轮数不够"，而是**生成器输出分布本身极窄**。
  216 个分子里 73 个 Murcko 骨架，头号 4-苯胺基喹唑啉母核占 31.0%，top-5 占 52.8%。
  整个分子池可用区间只有 **0.549 kcal/mol**（best -8.777，p25 -8.228）。
- **反证**：如果是样本量问题，随机抽样应该也打不过智能体。但实测随机抽 8 个就已经胜过智能体
  （-8.441 vs -8.324，胜率 72.7%）。轮数不是瓶颈。
- **与 `early_stop_patience` 的冲突**：`config.yaml` 里 `early_stop_patience: 3`，
  由 `loop.py:348` 传入 `LoopController.judge_convergence_patience`。
  而 `state.note_round_result()` 用的信号是 `summary["best_vina"]`，
  `summarize_round()` 里定义为**对全部已 docking 分子取 min，不看安全性**。
  即：连续 3 轮打不破 best_vina 就停。
  **实测（20 次运行）**：**70%（14/20）的运行里，第 0 轮的最优分子就是全程最优**；
  3 轮内"连续未改进"的最大连长为 2，平均约 1.9/3。
  也就是说在这个改进率下，耐心计数器约在第 4–5 轮就会累计到 3。
  **把 `max_rounds` 从 3 调到 12，实际会在第 4–5 轮被 early stop 掐掉，跑不满 12 轮。**

### ② scalarization / Pareto（MiniMax 排第 2）→ **方向对，事实错**

MiniMax 说"`pareto.enabled: true` 在 config 里但实际评估器没用——仍是加权求和"。**这是错的：**

- `agents/evaluator.py:339 assign_pareto_metadata()` 实现了完整的多轮非支配排序（`pareto_rank`、`pareto_dominated_by`），
  在 `evaluate_candidates()` 末尾（`:180`）和 `evaluate_candidates_with_cache()`（`loop.py:232`）**都会被调用**。
- `candidate_priority_key()`（`evaluator.py:381`）的排序键是
  `(safety_gate_pass, -pareto_rank, composite)` —— **Pareto 排名确实参与决策**，
  被 `summarize_round()`、`judge.py:99`、`working_memory._candidate_priority()` 三处消费。
- **而且最新实验已经把主指标换成安全门控版本**：`benchmarks/confirmatory_pareto_v3_1_20260916/benchmark_manifest.json`
  里 `primary_metric` 已经是 `best_safe_composite_global`。

**但方向是对的，我用数据确认了权衡客观存在**（这批数据上）：

| 量 | 值 |
|---|---|
| Pearson r(vina, herg_risk_score) | **-0.513**（n=480）|
| 安全门通过率 | 34.2% |
| 全池 best Vina | -8.777 |
| **全池 best Vina（仅安全分子）** | **-8.757**（只差 +0.020 kcal/mol）|

**这个 0.020 是整份复核里最有价值的数字。** 它说明安全约束**在物理上几乎不花代价**——
池子里已经存在一个既安全、docking 又和"不安全冠军"一样好的分子。
所以 **hERG 回归不是不可避免的权衡，而是选择/上报环节的缺陷**：
`best_vina` 这个进度信号在全量分子上取 min，把一个 herg=0.830 的分子当成了"进步"并重置了 patience 计数器。

**真正该改的地方（比 MiniMax 说的更精确）：**
- 把 `summary["best_vina"]` 拆成 `best_vina_all` 与 `best_safe_vina`，
  **`LoopController` 的改进信号必须用 `best_safe_vina`**。
- `tools/provenance.py` 的 protocol 摘要里应记录"安全门控是否参与进度信号"，
  否则跨协议比较会混淆。
- **不建议删 `objective.weights`**：`composite_score` 在 Pareto 同层内做细排序是有用的
  （`confirmatory_pareto_v3_1` 已经把精度提到 6 位就是为了这个）。
  加权和的问题是**被当成主指标**，而不是它存在。删掉会让 Pareto 同层出现大量并列。

### ③ Judge 反馈"言之无物" → **半数正确，但不是瓶颈**

- `judge.py:22` 的 system prompt 已经很具体（要求指明结构元素、替换基团、预期性质影响），
  并且已经注入了 `tools/references.py` 的 SAR 事实块（`judge.py:211`）。
- 但输出确实只是**文字指令**，没有指定父分子 SMILES 和位点。这一点 MiniMax 说得对。
- **不过它不是瓶颈**：`docs/EXPERIMENT_RESULTS_20260916.md` 的审计已经显示，
  Judge 的自报采纳率不可靠（LLM 自报 0.578 vs 结构判定 0.267，平均高估 3.0 个候选）。
  即"说得好"和"改得对"之间隔着的不是措辞精度，**是根本没有把反馈变成受约束的分子操作**。

### ④ Failed-set 是"反应式"不是"前瞻式" → **描述正确，但建议是反向的**

- 描述正确：`agents/failed_set.py` 的 `is_failed()` 是纯精确匹配；
  阈值判定在 `should_mark_failed()`（写入时），嵌入相似度走 `is_similar_to_failed()`。
- **但 MiniMax 的第 4 条建议（把 `mode: report` 改成 `exclude`）经实测是有害的。**
  我在真实池上实测了 `all-MiniLM-L6-v2`：

  | 量 | 值 |
  |---|---|
  | Pearson r(文本余弦, Morgan-Tanimoto) | 0.564 |
  | 文本余弦 ≥ 0.85 的分子对 | **219/300 = 73%** |
  | 文本余弦分布 | min 0.754 / 中位 0.916 / max 0.999 |
  | 这 219 对里结构其实不相似（Tanimoto<0.40）的 | 17 对 |
  | 真近缘对（Tanimoto≥0.85）被漏掉的 | 0/6 |

  `all-MiniLM-L6-v2` 是**文本**模型，SMILES 字符串的字面相似度和分子结构相似度只弱相关，
  且余弦分布严重各向异性（中位就 0.916，0.85 阈值大致落在第 27 百分位）。
  **开到 `exclude` 会以"和失败分子太像"为由砍掉约 73% 的候选对，其中包含大量结构上毫不相干的分子。**

- **正确的修法**：排除判定改用 **Morgan/ECFP4 Tanimoto（RDKit 已是依赖，确定、免费）**，
  阈值 0.6–0.7，先在历史池上做 ROC 校准；文本嵌入降级为廉价预筛或直接删除。

### ⑤ 单 provider 砍掉多样性红利 → **正确，但已不适用**

- 事实对：`config.yaml:43-55` 的 DeepSeek provider 块被注释掉，`generators: [MiniMax]`。
- 但**这正是最不需要担心的方向**。`docs/CONFIRMATORY_RESULTS_20260916.md` 里
  记忆组的跨轮重复数已经降到 **0.6**（对照组 3.3），重复不是当前的主要损失源。
- 真正的问题是**记忆把搜索空间收窄了**：记忆组的安全 Pareto 候选数 **5.4 vs 6.4**（更少），
  首末轮 Vina 变化 **+0.3600 vs +0.1055**（更差）。报告自己写了：
  *"它更像抑制器，还不是可靠的优化器。"*
- 所以加回异构 provider 是合理的**多样性来源**，但不要指望它救指标。

---

## 2. 逐条复核 MiniMax 的 8 个行动项

| # | MiniMax 建议 | 判定 | 说明 |
|---|---|---|---|
| 1 | 单组 12 轮 × 15 候选 | **否决** | ① `early_stop_patience: 3` 会在第 4–5 轮掐停；② 随机抽样已胜过智能体，加轮数只会更精确地测出一个零效应 |
| 2 | 改 Pareto + 删 weights | **部分采纳** | Pareto 已实现且已参与排序；`primary_metric` 已换成 `best_safe_composite_global`。**该做的是把 `LoopController` 的进度信号也换成安全门控版**，并保留 weights 用于同层细排 |
| 3 | Judge 输出 `patches[]` | **采纳（Priority B）** | 这是把"建议"变成"受约束操作"的正确方向，但必须和 #7 的算子库一起做才有意义 |
| 4 | `failed_set: report → exclude` | **强烈反对** | 实测 0.85 文本阈值会砍掉 73% 候选对。改用 ECFP4 Tanimoto + 校准 |
| 5 | 重新启用异构生成 | **低优先** | 重复数已不是瓶颈；可作为多样性来源，但别当主手段 |
| 6 | 换 ADMET 模型 | **采纳，升到 Priority A** | 见下方 §3「证据链污染」 |
| 7 | 加 `tools/mutate.py` 算子 | **采纳，应为 Priority A** | 这是补上"选择算子"的唯一路径，也是本次审计指出的首要根因 |
| 8 | 加 early stop | **已存在** | `early_stop_patience: 3` 已经在跑，而且**正是它阻碍了长轮次实验** |

MiniMax 的「不要做的事」列表我**全部同意**：换模型、加 prompt、叠更深 reflection、急着跑 5 轮以上对照，
这四条判断都对，理由也站得住。

---

## 3. 复核中发现的两个额外问题（MiniMax 完全没提）

### A. ADMET 打分器在实验中途被换过，两批数字不可直接并列

| 实验 | 候选数 | ADMET method | hERG 字段 |
|---|---|---|---|
| `real_ablation_v3_20260915` | 263 | `rdkit_descriptor_heuristic_v1` | 只有二值 `herg_risk` |
| `confirmatory_pareto_v3_1_20260916` | 487 | `rdkit_descriptor_heuristic_v2` | 连续 `herg_risk_score` |

MiniMax 在同一张证据表里同时引用了 `best_composite 0.819→0.842`（来自 v1 的 3 次重复消融）
和 `hERG 标记率 0.771→0.864`（同为 v1 的二值指标），却又说 `pareto.enabled` 里
`herg_risk_score` 这个字段没用上——**它在 v1 里根本不存在**，
`assign_pareto_metadata()` 当时是回退到二值 `admet.herg_risk` 计算的。
所以 v3.1 的 Pareto 前沿和 v3 的 Pareto 前沿不可比。

另外 `memory/best_molecules.json` 里带 v1 schema 的旧条目，其 `admet` 块缺少 `herg_risk_score`，
warning 字段还有乱码（`hERG 椋庨櫓鍋忛珮`，v2 已修）。跨会话加载这种条目时，
`working_memory.compress_for_generator()` 会把 `hERG-risk=None` 拼进 prompt，
**同时仍标注为 "Best safe Pareto candidate"**。

### B. `compress_for_generator()` 无条件把最优分子标成"safe"（真实 bug）

`agents/working_memory.py:150-155`：

```python
parts.append(
    f"Best safe Pareto candidate: Vina={bv:.2f}, ..."
)
```

**没有检查 `best_so_far["safety_gate_pass"]`。** 当全轮都没有分子通过安全门时
（实测安全门通过率只有 34.2%），生成器会被明确告知一个 `herg_risk ≈ 0.83` 的分子是
"Best **safe** Pareto candidate"，并被要求参照它设计。这是对 hERG 回归最直接、
最廉价、最应优先修的一条。

---

## 4. 我建议的优先级（与 MiniMax 的顺序不同）

### Priority A — 让 loop 重新变成搜索（1–2 天，不重跑实验即可验证）

1. **修 `compress_for_generator()` 的安全标注 bug**（1 行）。不安全就不要写 safe。
2. **拆分进度信号**：`summarize_round()` 增加 `best_safe_vina`，
   `LoopController` 改用它判断改进；`best_vina_all` 只保留为诊断字段。
3. **引入显式父代/精英机制**（对应 MiniMax #7）：
   - `tools/mutate.py`：ECFP4 Tanimoto 近邻检索 + BRICS 切分重组 / 原子替换 / 末端基团交换，
     全部确定性、可复现、不需要 LLM。
   - 生成器 prompt 增加结构化段：`PARENTS` = top-k 安全 Pareto 分子的 SMILES + 各自短板，
     明确要求"在其中至少一个基础上做局部改造"。
   - 先做**离线验证**：拿现有 216 个分子跑算子库，看生成的类似物是否落在
     best-safe-Vina 的邻域内。**这一步不花 docking，不需要 LLM，今天就能出结论。**
4. **跑 `scripts/audit_pool_vs_random.py` 作为所有未来实验的常设基线。**
   任何一版 loop，如果"随机抽样胜率"仍 > 50%，就不该进入统计比较阶段。

### Priority B — 让反馈可执行、让安全约束真的生效（3–5 天）

5. Judge 输出 `patches[]`（MiniMax #3）：`{parent_smiles, site, operation, expected_delta}`，
   由 `tools/mutate.py` 执行，LLM 只做**提议**不做**生成**。这样采纳率变成可测量、可审计的量。
6. Failed-set 排除判定换成 ECFP4 Tanimoto，阈值在历史池上校准；
   **embedding 保持 `report`，不要开 `exclude`**（对应 MiniMax #4，但做法相反）。
7. **记忆结构化**：拆分「负向约束 / 正向结构变换 / 适用上下文 / 证据强度」四类
   （`docs/CONFIRMATORY_RESULTS_20260916.md` §下一阶段建议 2 已提出，我同意并认为应提前）。

### Priority C — 证据链与可信度（2 周+）

8. **换掉 ADMET/hERG 代理**（MiniMax #6）：升到 Priority A 和 C 之间。
   在换之前，**任何跨 v1/v2 的指标比较都要显式标注协议版本**，
   并在 `docs/` 里加一段说明，避免 MiniMax 这类复核者把两批数字并列。
9. 统一 `protocol_id` 与报告头：把 ADMET 方法、安全门阈值、进度信号定义都写进
   `manifest.json`，让"能否比较"变成机器可判定的。
10. 加外部验证集与真实 hERG 预测器后，才谈"企业级自主迭代"。

---

## 5. 对"企业级自主选择/自我迭代 Agent"这句话的直白回答

当前仓库的工程质量（协议摘要、缓存分层、预注册、质量门禁、诚实的结果边界声明）
**已经超过多数 AIDD 项目**。但"自主迭代"需要四个闭环要素，现在缺三个：

| 要素 | 现状 |
|---|---|
| 可测量的目标 | ✅ `best_safe_composite_global`，已预注册 |
| 可执行的反馈 | ❌ Judge 只输出文字，没有受约束的分子操作 |
| **选择算子（精英保留/父代继承）** | ❌ **完全没有**，这是首要缺口 |
| 可信的评价器 | ⚠️ Vina 未校准、ADMET 是描述符启发式 |
| 记忆的进出条件 | ⚠️ 只有负向（失败黑名单），没有正向迁移 |

只补 #1（样本量）就像给一个没有选择算子的随机采样器加预算——
`P(random beats agent) = 72.7%` 这个数字会一直是 72.7%。
**先让 loop 在离线分子池上打赢随机抽样，再谈扩样本和上企业级。**

---

## 附：复现命令

```bash
# 完整审计（只读产物，不跑 docking / 不调 LLM）
python scripts/audit_pool_vs_random.py benchmarks/confirmatory_pareto_v3_1_20260916
python scripts/audit_pool_vs_random.py benchmarks/real_ablation_v3_20260915   # 注意：该批为 ADMET v1，无 herg_risk_score
```

---

## 6. Priority A-1 / A-2 已实施（2026-09-17）

改动范围小、可验证，`python -m pytest -q` → **149 passed, 1 skipped**。

### 新增的关键证据：进度信号确实在奖励项目不要的分子

对 20 次确认实验逐运行计算 `best_safe_vina - best_vina`：

| 量 | 值 |
|---|---|
| **"best Vina" 分子未通过安全门的运行** | **17 / 20** |
| 每次运行的安全代价（中位） | +0.228 kcal/mol |
| 均值 / 最大 | +0.296 / +0.908 kcal/mol |
| 池内上限（全池 vs 仅安全分子） | 只差 +0.020 kcal/mol |

也就是说：**池子里存在几乎同样好的安全分子，但 85% 的运行把冠军位置给了一个项目明确不要的分子。**

### A-1 记忆标签 bug — 已修

`agents/working_memory.py`：`compress_for_generator()` 不再无条件声称 "Best safe Pareto candidate"。

- `safety_gate_pass is True` → `Best safety-gate-passing candidate`
- `safety_gate_pass is False` → `Best candidate so far (WARNING: FAILS the safety gate - do not copy its hERG/logP profile)`
- 未评估 → `Best candidate so far (safety gate not evaluated)`
- 顺带修掉 v1 ADMET schema 下 `hERG-risk=None` 渲进 prompt 的问题（现输出 `unknown` / 兼容 `herg_risk`）

### A-2 进度信号拆分 — 已实施

**设计原则：只新增字段，不改 `best_vina` 语义**，因此 `experiments/reporting.py` 的历史指标与已有 20 次运行保持可比。

| 文件 | 改动 |
|---|---|
| `agents/evaluator.py` | `summarize_round()` 新增 `best_safe_vina`、`best_safe_smiles`、`best_safe_composite`、`n_docked_safe`；`best_vina` 原样保留 |
| `agents/loop_controller.py` | `LoopState` 同时维护两套耐心计数；新增 `LoopConfig.progress_signal`（`"vina"` 默认=旧语义 / `"safe_vina"`）；非法值抛 `ValueError` |
| `loop.py` | 两套计数始终更新，只有配置的那个决定终止；`manifest.execution` 记录 `progress_signal` + `progress_patience`；`round_record.loop_state` 与 `summary.loop_state` 同时记录两个计数；HITL 突破判定跟随所选信号 |
| `config.yaml` | `loop.progress_signal: safe_vina`（含依据注释） |
| `experiments/reporting.py` | 新增 `best_safe_vina_global`、`top5_safe_vina_mean`（**由分子记录回溯计算**，对历史运行同样有效） |
| `tests/test_phase4_1.py` | +5 个测试：安全信号只认安全进步、`None` 不推进耐心、非法值被拒、默认仍是旧信号、记忆标签回归 |

**协议可比性**：`progress_signal` 影响的是循环控制流、不是打分，因此**故意不进入 `protocol_id`**
（评估缓存与 `memory_namespace` 全部保持有效），只进 `manifest.json`。
不同 `progress_signal` 的运行不可直接比较。

**`None` 语义**：某轮没有任何分子通过安全门时 `best_safe_vina` 为 `None`，
按项目"缺失分数是未知、不是成功"的原则，**既不记为进步也不记为平台期**（计数冻结）。
这样 `--mock --no-dock` 冒烟运行也不会被提前掐停。

### 顺带修掉的既有故障：`--mock` 冒烟命令已失效

`agents/llm.py` 的 `MockLLMClient` 固定返回 5 个分子，而 `loop.candidates_per_round_per_generator`
在 Phase 4.4（2026-09-14）已提到 15。结果是**文档里的冒烟命令
`python loop.py --mock --no-dock --rounds 3` 自 09-14 起直接 `no_candidates` 退出、跑 0 轮**。
现改为从 system prompt 读取 `Provide exactly N SMILES` 并循环生成（上限 200），冒烟恢复 3 轮。

### 验证记录

```
manifest.execution.progress_signal = safe_vina | progress_patience = 3
round_2.loop_state = {"progress_signal": "safe_vina",
                      "rounds_without_vina_improvement": 0,
                      "rounds_without_safe_vina_improvement": 0}
[B] valid=5/5 ... best_Vina=None best_safe_Vina=None (safe 0/0 docked)
[controller] signal=safe_vina patience=3 no_improvement=0 (vina=0, safe_vina=0)
```

### 尚未做（明确的下一步）

1. `best_safe_vina_delta` 曲线（`agent_metrics` 目前只算 `best_vina` 曲线）→ 让"首末轮变化"也能按安全口径给出。
2. `tools/mutate.py` + 生成器结构化 `PARENTS` 段（= Priority A-3，补上缺失的选择算子）。
3. 把 `scripts/audit_pool_vs_random.py` 的随机胜率设为所有实验的准入门槛。
4. 用一次真实运行验证 `safe_vina` 是否真的提升了 `best_safe_composite_global`
   —— **这才是这次改动唯一的验收标准**。
