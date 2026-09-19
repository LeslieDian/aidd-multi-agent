# 持久任务智能体

`agent_task.py` 提供独立任务入口，复用现有 Generator、Evaluator 和 LLM 客户端。
`loop.py` 仍用于原有工作流和基准实验。开发本功能不需要执行大规模实验。

## 浏览器控制台

```powershell
python agent_dashboard.py
```

打开 **http://127.0.0.1:8765**。默认读取 `runs/` 下直接包含 `task.json` 的任务目录。
可用 `--task-root runs --port 8765 --config config.yaml` 指定目录、端口和新任务配置。

控制台可以：

- 创建离线演示或真实模型任务，设置本次步数及总预算；默认不启用 docking。
- 查看当前动作、简短决策依据、资源用量、候选结果和执行时间线。
- 提交新要求，查看“已提交待处理”与“已生效”的区别。
- 暂停、继续和取消任务；中断结果不确定时明确提示恢复确认。
- 查看智能体请求补充的问题及最终总结。

页面约每 1.5 秒从检查点更新；不会把配置或 API 密钥发送到页面。
显示的是模型提交的简短动作依据和工具证据，不是模型内部推理过程。
任务由独立 Python 进程执行，使用与命令行相同的 `agent_task.py`。
关闭浏览器或控制台服务不会自动取消正在执行的任务；需要停止时先点暂停或取消。
执行进程日志位于任务目录的 `worker.log`。控制台仅绑定本机地址，不提供公网部署功能。

这里的 **harness** 是项目自建的模型执行控制层，不是另一个模型，也不是已安装的第三方框架。
模型决定下一步动作；harness 校验并执行动作、记录结果、控制预算、处理重试和恢复。
新增控制台通过它让用户直接查看并干预任务。旧 `loop.py` 的固定工作流保持独立。

## 离线验收：执行、暂停、恢复、新要求

在项目根目录执行，使用一个尚不存在任务的输出目录：

```powershell
python agent_task.py start --goal "筛选 EGFR 候选，先完成性质初筛" --mock --task-dir runs/harness_demo --steps 2
python agent_task.py status --task-dir runs/harness_demo
python agent_task.py resume --task-dir runs/harness_demo --instruction "优先比较已有候选，暂时不要生成新分子" --steps 2
```

第一次调用完成生成和性质评估后暂停；第二次执行查看状态；恢复后完成比较和总结。
已有候选及评估结果保留，新要求写入 revision 1，并进入后续决策上下文。
最终状态为 `completed`。Mock 策略是确定性演示，不解释任意自然语言，也不证明优化效果。

## 真实决策

```powershell
python agent_task.py start --goal "围绕现有 EGFR 设计约束探索候选，并比较性质指标" --task-dir runs/my_task --steps 2 --max-steps 20
python agent_task.py resume --task-dir runs/my_task --instruction "优先优化候选 c1 的性质，保留已有结果" --steps 3
```

不传 `--mock` 时，模型根据当前状态选择工具。决策模型默认使用 `llm.judge`，
可通过配置 `harness.planner` 指定已配置的 provider；生成工具使用 `llm.generators` 的第一项。
需要相应的环境变量密钥；缺少密钥会留下错误记录，不会切换为 Mock。

默认不调用 Vina，结果是性质初筛。创建任务时传 `--dock` 才启用真实 docking。
目标、评分配置、Mock/docking 模式在创建时保存，恢复不会重新读取 config.yaml。
底层生成提示仍是项目现有的 EGFR 提示，不支持任意靶点的自动适配。

## 四块实现

| 模块 | 职责 |
|---|---|
| `agents/harness/state.py` | TaskState：目标、用户要求版本、候选、事件、预算、状态、最终结果；检查点原子写入和进程锁 |
| `agents/harness/tools.py` | 工具注册、参数检查、候选 ID 检查；复用生成和评估实现 |
| `agents/harness/runtime.py` | 模型单步决策、动作执行、结果提交、错误反馈、暂停恢复 |
| `agents/harness/reliability.py` | 调用预算、临时错误分类、显式重试和请求超时配置 |
| `agent_task.py` | start / resume / status / steer / pause / cancel 命令行入口 |
| `agent_dashboard.py` / `agents/harness/dashboard.py` | 本地浏览器控制台与任务进程管理 |
| `agents/harness/presentation.py` / `dashboard.html` | 中文状态、动作依据、结果和干预界面 |

工具包括 `generate`、`refine`、`evaluate`、`compare`、`history`、`retry_evaluation`、`pause`、`finish`。
每个动作必须给出结构化参数和简短依据。模型不能通过工具改变评分阈值或执行任意代码。
`refine` 记录父分子和修改意图，尚未验证实际结构是否落实修改。
`history` 查询旧动作与证据；`retry_evaluation` 只重新评估执行失败的候选，保留失败历史。
低评分和无效结构不是执行失败，不会触发自动评估重试。
最终候选必须具有成功的完整评估或性质初筛证据，不能将工具执行错误当作完成依据。

## 运行中干预

在另一个终端提交指令：

```powershell
python agent_task.py steer --task-dir runs/my_task --instruction "优先比较已有候选，暂时不要生成新分子"
python agent_task.py pause --task-dir runs/my_task
python agent_task.py resume --task-dir runs/my_task --steps 3
python agent_task.py cancel --task-dir runs/my_task
```

指令通过原子文件队列提交，每条仅应用一次。空闲任务立即应用；运行中的任务在执行边界处理。
决策过程中收到新要求时，旧决策被丢弃，下一步重新规划。
工具执行过程中收到指令时，先保存该工具结果，再暂停、取消或应用新要求。
暂停可恢复；取消是终态。指令不会强行终止正在运行的网络请求或 docking 子进程。
尚未解决的不确定工具调用会阻止新要求应用，但仍可取消任务。

## 执行预算和错误处理

```powershell
python agent_task.py start --goal "探索并比较 EGFR 候选" --task-dir runs/bounded_task --max-steps 20 --max-model-calls 40 --max-evaluations 100
```

预算包含总决策数、模型调用尝试数和候选评估次数；调用前持久化预留，恢复时不重置。
决策和生成共享模型调用预算，自动重试也计入。评估缓存命中不重复计数；失败后的显式重评计数。
为了避免中断后漏记，已预留但结果未知的调用不退还预算。Mock 使用同样的预算计数，实际不调用模型。
这些是工作量上限，不是精确 token 或人民币费用上限。

`harness.max_attempts` 默认 3，允许 1–5；只对允许重试的模型决策、生成工具的网络连接、
超时、HTTP 429 和 5xx 错误进行有界退避重试。SDK 内置重试关闭，避免重复叠加。
`harness.request_timeout` 默认 60 秒，允许 1–300 秒，传给模型 SDK；不是整个任务的强制终止期限。
其他工具默认不自动重放，评估工具返回的逐候选失败由模型通过 `retry_evaluation` 处理。
参数错误返回模型修正；连续三次错误、连续第三次相同动作、连续八个无候选变化的动作都会暂停。

## 检查点与中断语义

- 每次决策和工具调用前后保存 `task.json`；完整工具结果、候选和事件一起原子提交。
- 工具成功后先写入 `receipts/` 回执，再提交主检查点。若这两步之间中断，恢复时导入回执，不重新调用工具。
- 已保存的评估结果在同一任务中复用，恢复不会重跑已完成步骤。
- 每个任务目录只允许一个执行进程；锁随进程退出释放。
- `--steps` 是本次调用允许的决策数，到达后暂停；`--max-steps` 是持久的总决策预算。
- 无效动作和失败决策也消耗预算；连续三次错误暂停；连续第三次相同动作被阻止。
- Ctrl+C 尽力保存当前状态。若工具执行中被中断且没有回执，`pending` 标记其结果未知，不能保证外部调用未发生。
- 检查 pending 及 artifacts 后，使用 `resume --ack-interrupted` 确认这次不确定结果，交由模型重新规划。
  它不会自动重放中断调用，但模型后续仍可能提出新的调用；不承诺外部 API 的 exactly-once 语义。
- 已完成任务不会再次执行；需要新的工作时创建新任务目录。

恢复时检查评估协议指纹（评分代码、相关依赖、受体及评分配置）。指纹变化会暂停，避免复用不兼容结果。
该检查不冻结执行环境；任务期间应保持代码、依赖和受体文件稳定。
目前尚无后台任务调度、精确 token/费用硬预算和长任务上下文压缩。
网络超时重试仍可能产生服务端费用，不承诺外部 API 的恰好一次执行。

ADMET 为现有启发式代理，Vina 分数不是实验亲和力。关闭 docking 时不会生成完整综合分。
Mock 只模拟决策和生成，性质评估仍调用真实的本地 RDKit 工具。

## 验证

```powershell
python -m pytest tests/test_harness.py -q
```

覆盖自主动作链、新要求导致重新规划、暂停取消、参数纠错、临时错误重试、失败评估重试、
回执恢复、协议变化、持久预算、重复动作、显示故障和进程锁。
