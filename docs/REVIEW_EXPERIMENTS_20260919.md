# 实验与修改复核（2026-09-19）

本次读取用户提供的 MiniMax 交接文本，核对 Git 提交 `9cd3d02`、工作区差异、实际实验 JSON、配置与执行代码。没有把交接文本中的成功声明当作实验凭据，没有重新运行真实模型或 docking，没有改写旧实验数据。本次只增加复核文档与 README 提示。

## 1. 最新 v4 实际已经结束，但没有完成全部合格重复

来源：`benchmarks/real_ablation_v4/benchmark_manifest.json`、`benchmark_report.md`、各 attempt 的 summary、proposals 和 round 文件。

- 时间：2026-09-19 00:10:41 至 01:04:28，约 54 分钟。
- 计划：4 组 × 3 重复 × 3 轮，每轮单个生成器请求 5 个候选；不是双生成器。
- 模型配置：生成 MiniMax-M3，temperature=1.0；Judge MiniMax-M3，temperature=0.3。真实调用模式，启用 docking；采用 screening 两阶段筛选和缓存，不是每个候选都进行全精度 docking。
- 实际：17 个 attempt，10 个合格，7 个排除。前三组各 3 个合格，reflection_memory 仅 1 个合格。
- reflection/repeat_03 第一次生成不完整，第二次完成。
- reflection_memory/repeat_02 第一次已保存 3 轮候选，但最后 Judge 发生 `APIConnectionError: Connection error.`，按现有完整性规则不合格；其后两次在第 0 轮生成失败。
- reflection_memory/repeat_03 三次均在第 0 轮生成失败，末次 proposals 记录连续 3 次 APIConnectionError。
- 当前没有实验 Python/Vina 进程。交接末尾 VS Code Copilot 的连接重置与实验自身的 API 连接错误是不同证据，不能仅根据聊天中断定位实验中断原因。

| 组 | 合格重复 | best composite 均值 | 每次最优 Vina 的均值 | 与 baseline 主指标比较 |
|---|---:|---:|---:|---|
| baseline | 3 | 0.772 | -8.407 | 基准 |
| reflection | 3 | 0.778 | -8.473 | Δ +0.006，p=0.717，未定 |
| reflection_failed_set | 3 | 0.803 | -8.456 | Δ +0.031，p=0.054，未定 |
| reflection_memory | 1 | 0.794 | -8.476 | 重复不足 |

表格取自原报告并保留其四舍五入。候选存在真实 docking 记录与产物，但 Vina 是计算评分，不能当作实测活性；hERG 等为项目的描述符代理指标。上述主指标为 best_composite_global，不应写成 best_safe_composite_global。小样本检验仅作探索性描述。

当前值得追踪的是 FailedLigandSet 组的正向主指标趋势；不能宣布模块有效，也不能把网络故障当作记忆策略失败。按组顺序执行使后期网络故障集中落在记忆组；失败成本与排除情况必须一并披露。

## 2. 修改的价值与核查问题

有价值的改动包括：新增 FailedLigandSet 单独消融组；记忆提示区分安全门通过、失败与未知；保存安全门内 Vina；为循环增加 safe_vina 进度信号；保留排除 attempt 和无望达标早停。以下问题应在大规模确认实验之前解决：

1. **n=20 未按文档命令生效。** `experiments/confirmatory_matrix_v4.yaml` 写 repeats=20，但 `scripts/run_benchmark.py` 优先读取 profile 的 repeats；`experiments/profiles.yaml` 的 confirmatory 为 10。README 原命令未传 `--repeats 20`，实际会跑每组 10 次。须明确优先级、记录最终预算并测试；增大样本量本身不会保证达标。
2. **两个 treatment 并未接入确认判决。** YAML 使用 `treatment_groups` 数组，但 `experiments/reporting.py::_confirmatory_decision` 和 `scripts/run_benchmark.py::assess_confirmatory_futility` 只读取单数 `treatment_group`，缺失时默认为 reflection_memory。FailedLigandSet 组会运行并被描述性比较，但不参加所宣称的两处理组确认门控。配置注释所谓“自动挑最小 p 值”没有实现；也不应未经多重比较设计这样选优。
3. **配置描述包含未实现的早停。** YAML 声称两批连续 2-sigma 分离会触发 interim stop，当前 runner 没有这段逻辑。已有实现是成功率数学上不可能达到要求的 futility。注释公式 `ceil(rate * planned) - 1` 也不对；代码用 `ceil(rate * planned - 1e-12)`，20 次、70% 要求 14 次成功。
4. **四组设计还缺正确增量报告。** 当前 incremental comparison 仍是 reflection_memory 对 reflection，混合了失败过滤与 WorkingMemory 的作用。需要同时报告 reflection_failed_set 对 reflection，以及 reflection_memory 对 reflection_failed_set，才能分别观察新增组件。
5. **“真因”超出证据。** `diagnose_reflection.py` 只是回放轨迹和 Judge 文本，不能排除 prompt、temperature、随机生成等解释。README 与 matrix_v4 对“真正起作用的是 FailedLigandSet 而非 WorkingMemory”的描述，应视为待检验假设。v4 记忆组仅一次，更无法归因。
6. **safe_vina 的控制与报告口径尚未统一。** 新控制器无安全候选时直接返回，不增加无改善计数；这是现有显式设计，不等于“持续没有合格候选会触发 patience 停止”。确认判决的 stable_improvement 仍读取全候选 best_vina_delta 和 run_improvement_rate。v4 仅 3 轮且 patience=3，通常也无法检验该进度信号的提前终止效果。
7. **续跑没有冻结全部配置。** `--resume` 检查预算、模式和 profile，但生成新 attempt 时重新读取当前 base config、当前 matrix overrides。修改配置或代码后直接续跑可能混入不同条件，必须先比对历史 resolved_config、manifest 和代码哈希。各 attempt 的记忆 namespace 独立；评估与 docking 缓存则共享，时间和新 docking 成本需结合命中率解释。

这些是静态代码和产物核查发现；本次没有改变统计规则、过滤规则或原始阈值，以免事后改变实验解释。

## 3. 以前的结果与两条执行路径

`confirmatory_pareto_v3_1_20260916/benchmark_report.json` 的原始确认判决是 do_not_approve：主指标 Δ=-0.002677，p=0.719937；improved_run_rate=0.1；efficacy_supported=false、stable_improvement=false、safety_noninferior=true。它支持“尚未确认记忆提升主指标”，不是“所有改动无效”。

最新 v4 走 `loop.py` 的批量生成—评价—Judge—记忆消融路径。此前受约束编辑的 `agent_task.py` / `agents/harness/` 是另一条执行路径。v4 的成功不能证明 TaskState、确定性编辑、用户约束变更和恢复链路已通过验收。

原小型二维对照 `runs/diagnostic_2d_comparison_20260918` 的 agent/metrics.json 仍是 paused/consecutive_errors：4 步、5 次 planner attempt、3 次动作拒绝、0 个新结构评估。不能写成 MiniMax 已完成这个对照。冻结目录中的可达性检查显示 24 个不同产物没有达到预设性质分改善 0.01；因此该任务也不能据此比较谁更会找到达标解。

## 4. 建议的下一次具体操作

先修复确认配置/执行器合同与报告增量对照，并增加针对这些问题的离线测试。不要先启动 20 重复确认实验。原 v4 的缺失重复可在网络恢复且核对冻结条件后补齐，保留全部失败 attempts，并明确这是增加基础设施重试上限后的补跑。

当前每个缺失重复已经达到 attempt_03。单加 `--resume` 使用默认 max-attempts=3 不会产生 attempt_04。确认条件一致后，以下命令才会给未合格重复各增加一次机会，跳过 10 个合格重复；它从新的 attempt 第 0 轮开始，不是从旧失败轮次接续：

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:AIDD_EMBED_AUTO_DOWNLOAD = '0'
python scripts/run_benchmark.py --matrix experiments/matrix_v4.yaml --profile screening --benchmark-id real_ablation_v4 --resume --max-attempts 4
```

本次没有执行此命令。离线 Hugging Face 环境变量只针对已缓存 embedding 的加载，不能修复 MiniMax API 连接；需先检查 API 连通性。补跑的完整时间无法保证：既有成功 run 约 3–6 分钟，两个缺失重复若一次成功，运行本身约 6–12 分钟，另计检查与连接重试。

如果近期目标仍是“二维受约束智能体比简单规则更有价值”，优先处理该 Harness 的动作 schema 错误，选择事先验证存在可达解的固定任务，重新冻结协议再对照；不要把不同路径的实验混成一个结论。

## 5. 本次验证

运行 `python -m pytest -q tests/test_benchmarking.py tests/test_loop.py tests/test_phase4_1.py tests/test_2d_comparison.py`，结果 **35 passed**（10.48 秒）。现有测试通过不代表上述新配置语义有覆盖。本次未执行全量测试、真实模型调用或新的 docking 实验。
