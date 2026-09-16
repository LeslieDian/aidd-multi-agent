# 第一阶段：工程收口

完成日期：2026-09-15。

本阶段解决配置、实验隔离、候选过滤和结果解释四类问题，为下一阶段的对照实验建立可靠基线。

## 1. 配置成为默认事实来源

不传命令行覆盖参数时，`loop.py` 读取：

- `loop.max_rounds`
- `loop.candidates_per_round_per_generator`
- `loop.early_stop_patience`
- `loop.output_dir`

`--rounds`、`--n` 和 `--output` 只做显式覆盖。每次运行的最终参数写入
`manifest.json.execution`，从结果文件即可判断实际跑了什么配置。

原理：实验配置和实际执行参数必须一致。否则修改配置后仍使用代码默认值，会让成本估算、复现和组间比较全部失真。

## 2. 记忆按实验边界隔离

正式运行的跨会话记忆位于：

```text
memory/v2/<target>/<protocol_id>/<memory_namespace>/
```

其中包含失败分子、策略历史和最佳分子。Mock 运行的记忆只保存在该次运行目录的
`_memory/` 中。`memory_namespace` 用于区分后续对照实验的不同实验组。

原理：Agent 的历史会影响后续输出，因此记忆本身也是实验输入。靶点、评分协议或实验组不同却共用记忆，会产生数据泄漏，无法判断结果来自当前策略还是旧历史。

## 3. 先去除确定重复，再谨慎处理相似结构

主循环在调用 RDKit、ADMET 和 Vina 前执行 canonical SMILES 精确去重：

- 移除同一批次中的等价结构；
- 移除正式失败记忆里已经评估过的完全相同结构；
- 保留无效 SMILES，让 Evaluator 记录 `invalid_structure`；
- 将过滤数量写入 `summary.candidate_filter`。

文本 embedding 相似度默认使用 `mode: report`，只为候选增加诊断标记，不直接淘汰。

原理：canonical SMILES 把同一分子的不同写法归一化，因此精确去重具有明确化学含义。
当前 embedding 模型主要学习自然语言相似性，未经过本项目化学空间的系统验证；直接过滤可能误删有价值的 SAR 改造。

## 4. 区分运行趋势与因果结论

`metrics.json` schema 升级到版本 2：

- `run_shows_improvement` 只描述本次运行末轮相对首轮是否改善；
- `agent_is_learning` 在单次运行中固定为 `null`；
- 合法率、骨架数、ADMET、采纳率和 Vina 曲线继续作为独立观测量保存。

原理：一次运行中分数变好可能来自随机采样、候选数量增加或模型波动。只有在相同预算下重复比较
“无反馈、反馈、反馈加记忆”等实验组，才能把收益归因于 Agent 的反思或记忆机制。

## 验证

```bash
python -m pytest -q
python loop.py --mock --no-dock
```

回归测试覆盖配置默认值、Mock 记忆隔离、canonical 去重和指标语义。

## 5. 协议级评估缓存

成功的完整评估和 `--no-dock` 初筛使用以下逻辑键缓存：

```text
protocol_id + canonical_smiles
```

缓存命中时保留当前候选的 run、round、provider 和 candidate ID，只复用 `validate`、
`admet`、`dock`、`scaffold` 与评分字段。`evaluation_cache.source` 指向首次计算的候选，
`dock.artifacts` 继续指向原始 docking 工件。原始 `result.json` 不存在时，缓存视为失效并重新计算。

工具失败和无效结构不进入缓存，避免把瞬时故障固化成长期结果。Mock 使用运行目录内的独立缓存。

已有运行可以通过以下命令导入：

```bash
python scripts/warm_evaluation_cache.py runs/<run-directory>
```

原理：canonical SMILES 解决同一分子的多种字符串写法，protocol ID 则隔离受体、口袋、
评分参数、软件版本和评分实现。两者同时一致时，固定 seed 的计算结果才具备复用条件。
