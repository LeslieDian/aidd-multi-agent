# AIDD Agent 对照实验设计

## 目标

本实验回答两个可分离的问题：

1. Judge 的逐轮反思是否优于只有双生成器的固定循环？
2. 在 Judge 基础上加入工作记忆与失败分子记忆，是否进一步改善结果？

实验不会用一次运行中的最好个例证明 Agent 已经学会优化。结论来自预算相同的重复实验及组间统计比较。

## 实验矩阵

| 组 | 双生成器 | Judge 反馈 | 工作记忆 | 失败分子记忆 |
|---|---:|---:|---:|---:|
| `baseline` | 是 | 否 | 否 | 否 |
| `reflection` | 是 | 是 | 否 | 否 |
| `reflection_memory` | 是 | 是 | 是 | 是 |

每组运行 3 次独立重复；每次 3 轮；每轮 DeepSeek 与 MiniMax 各生成 5 个候选。靶点、受体、口袋、Vina 参数、评分代码和生成预算保持一致。

配置来源为 [`experiments/matrix.yaml`](../experiments/matrix.yaml)。正式执行命令：

```bash
python scripts/run_benchmark.py --profile screening --max-attempts 3
```

运行档位与加速原理见 [`EXPERIMENT_SPEEDUP.md`](EXPERIMENT_SPEEDUP.md)。正式确认使用：

```powershell
python scripts/run_benchmark.py --matrix experiments/confirmatory_matrix.yaml --profile confirmatory
```

## 实验有效性控制

- 每个实验组使用独立 `memory_namespace`，避免策略历史跨组泄漏。
- 每轮要求两个生成器各返回恰好 5 个候选；单个生成器失败时只重试该生成器。
- 连续 3 次仍无法得到完整生成批次时，立即终止该次尝试，标记为排除并自动重跑。
- Judge 启用的组要求每轮 Judge 状态为 `ok`。
- 只有完成全部轮次且通过上述门禁的运行进入统计报告；被排除的运行及原因仍保留。
- 相同 `protocol_id + canonical SMILES` 的评估复用缓存。缓存只复用确定性的评分和 docking 工件，不复用生成、Judge 或记忆状态。

## 指标与统计方法

预先指定的主指标为每次运行的全局最高综合分 `best_composite_global`。同时报告：

- 最佳及 Top-5 Vina 分数；
- 有效率、唯一有效分子数、唯一骨架数；
- 跨轮重复数、hERG 启发式标记率；
- token、耗时、缓存命中和新增 docking 次数。

每组报告均值、样本标准差和基于 t 分布的 95% 置信区间。实验组与基线使用双侧 Welch t 检验，因为不同组的方差不必相等。每组只有 3 次重复，因此统计结果用于发现方向和估计波动；`p >= 0.05` 解释为证据不足，不解释为两组等效。

## 输出

每次正式矩阵在 `benchmarks/<benchmark-id>/` 下生成：

- `benchmark_manifest.json`：实验定义、预算、运行状态与排除原因；
- `benchmark_report.json`：机器可读的逐次指标、组统计和组间比较；
- `benchmark_report.md`：便于评审的统计报告；
- `<group>/repeat_XX/attempt_XX/`：完整候选、评分、Judge 输出、工件与配置快照。

已有结果可重新汇总，无需重新调用模型或 docking：

```bash
python scripts/compare_experiments.py benchmarks/<benchmark-id>
```
