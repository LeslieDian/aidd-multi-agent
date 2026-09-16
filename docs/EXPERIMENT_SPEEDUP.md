# 实验加速管线

## GPU 与 Vina 的职责

本机 CUDA 可用时，失败集合的 sentence-transformers embedding 自动使用 GPU；不可用时回退到 CPU。设备记录在失败集合元数据和诊断状态中。

仓库内的标准 `vina.exe` 是 CPU 程序，不会因为 PyTorch 检测到 CUDA 而自动使用显卡。管线通过并行启动多个独立 Vina 进程加速；每个任务仍保持固定 seed、独立 UUID 工件目录和完整协议来源。

## 三种运行档位

```powershell
# 离线检查：mock LLM，不运行 Vina
python scripts/run_benchmark.py --profile smoke

# 快速筛选：真实 LLM，并行两阶段 docking
python scripts/run_benchmark.py --profile screening

# 正式确认：真实 LLM，并行完整 docking，不使用漏斗近似
python scripts/run_benchmark.py --matrix experiments/confirmatory_matrix.yaml --profile confirmatory
```

档位定义保存在 `experiments/profiles.yaml`。命令行的 `--repeats`、`--rounds` 和 `--n` 可以显式覆盖档位预算。

| 档位 | 重复 | 轮次 | 每生成器候选 | Docking |
|---|---:|---:|---:|---|
| smoke | 1 | 1 | 2 | 关闭 |
| screening | 3 | 3 | 5 | 两阶段漏斗 |
| confirmatory | 10 | 3 | 5 | 完整协议 |

## 并行 docking

`scoring.vina.workers` 控制同时运行的 Vina 进程数，`scoring.vina.cpu` 控制每个进程的 CPU 线程数。默认是 4 个进程、每个 2 个线程。候选并发完成后按原始提案顺序合并，缓存写入仍由主线程依次执行。

机器 CPU 较少时可把 `workers` 设为 `auto`，运行时使用 `逻辑 CPU 数 / 每任务 CPU 数`，并且不会超过当前任务数。

## 两阶段漏斗

`screening` 档执行：

1. 所有合法候选计算 RDKit、SA、ADMET 和 hERG 代理指标；
2. 所有合法候选执行快速 Vina（exhaustiveness 4、1 个 pose）；
3. 按安全门槛、性质分、快速 Vina 和风险排序；
4. 至少晋级 3 个候选，默认晋级合法候选的 35%；
5. 晋级候选执行正式 Vina（screening 档为 exhaustiveness 8、3 个 pose）；
6. 未晋级候选标记为 `screened_out`，不会混入正式综合分或 Pareto 排名。

如果某分子已有完全兼容的正式 docking 缓存，它会直接晋级并复用，不会被快速漏斗丢弃。完整评估缓存会保存 `screened_out` 结果及快速 docking 工件来源。

`confirmatory` 档关闭漏斗，对全部合法候选执行预注册的完整 Vina（exhaustiveness 16、5 个 pose）。因此筛选速度优化不会改变正式确认实验的评分协议。

## 无望提前停止

确认实验的准入条件是合取关系。若某个必要条件已经不可能达到，继续运行不会改变最终拒绝决策。

对于稳定改善率，管线在每个合格 treatment 重复后计算：

```text
最大可能成功次数 = 当前成功次数 + 剩余重复数
```

当最大可能成功次数小于预注册要求时，写入 `benchmark_manifest.json.early_stop` 并停止剩余调度。例如计划 10 次、要求 70%，前 5 次只有 1 次成功时，最多只能达到 6/10，因此可以确定停止。

可用 `--no-futility-stop` 完成固定样本，但默认确认矩阵启用确定性的无望停止。该规则不根据临时 p 值挑选有利结果，只在必要门槛数学上不可达时停止。

## 使用顺序

1. 每次代码改动先运行 `smoke`；
2. 新策略先进入 `screening`；
3. 只有相对基线方向有利、安全没有明显退化的策略才预注册并进入 `confirmatory`；
4. 确认阶段仍保留全精度、完整来源和统计门槛。
