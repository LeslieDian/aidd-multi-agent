# MiniMax 下一阶段连续交付提示词

下面的内容可以直接复制给 MiniMax。它以当前仓库状态为起点，目标是修复研究智能体的证据复用和决策可靠性，并完成一次小型二维验收。它不是服务平台扩建任务。

---

你正在维护仓库 `D:\vs project\AIDD agent`。请把本任务作为连续交付执行：先审计、再修改、再测试、再运行一个很小的真实验收、最后更新 README。不要停在方案说明，也不要未经证据宣布成功。遇到失败时保留原始记录，定位原因后继续完成同一阶段；不要删除或重写已有实验目录。

## 一、当前事实与研究主线

项目的论文主线是：二维结构约束与性质反馈闭环中的智能体，能否基于已有证据选择下一步确定性分子编辑，并在失败后继续、换策略或停止。

当前事实：

1. 全量测试基线为 `210 passed, 1 skipped`。
2. `real_ablation_v4` 已补齐四组各 3 个合格重复，但所有主比较仍为 inconclusive。不要启动每组 20 次 confirmatory。
3. 二维正对照 `runs/diagnostic_2d_positive_20260919` 有 16 个不同产物，其中 2 个满足 property_score 改善至少 0.01；这些答案不能传给策略。
4. 规则组评估 10 个新结构未命中。智能体组只评估 3 个结构，最好改善为 `+0.008208575961059172`，随后因 5 次网络重试和错误调用 `choose_strategy` 暂停。这个运行是 pilot，不得删除、覆盖或重启凑成功。
5. Repository 已写入 runs、候选、评估、事件和步骤快照；artifact 与 approval 只有显式调用时写入。FastAPI 只是本地研究原型，不是生产服务。

## 二、严格范围

按下面顺序完成四个阶段。不要增加新的分子编辑工具，不扩展 3D/docking，不开发登录、RBAC、多租户、Redis、Celery、分布式 Worker，不运行 n=20，不改变 property_score 公式、0.01 阈值、hERG 退化限制或已有实验数据。

不要仅靠修改 prompt 掩盖状态机错误。模型负责选择动作；合法动作、协议隔离、预算、结果判定必须由确定性代码执行。

不要使用 `git reset --hard`、`git checkout --` 或删除未提交文件。不得打印或提交 `.env`、API key、本地 SQLite、缓存、日志和 docking 大文件。

## 三、阶段 1：完成 Repository 的最小正确性

先阅读：

- `db/repository.py`
- `agents/harness/state.py`
- `agents/harness/runtime.py`
- `agents/harness/tools.py::_evaluate`
- `tests/test_repository.py`

需要完成：

1. 评估查询必须按 `(task_id, candidate_id, protocol_id)` 精确匹配。不要先取该候选最后一条记录再比较协议。
2. 当前协议没有记录、但旧协议存在时，返回“当前协议无结果”并执行新评估；绝不能复用旧协议，也不能仅因为旧协议存在就永久阻断新评估。
3. 明确 SQLite 是持久事实源，JSON checkpoint 是原子执行恢复副本。为两者保存单调版本或等价校验信息；读取时检测不一致，不要悄悄覆盖。
4. 评估历史要可审计。不要用同一键无声覆盖不同事实。如果同一协议允许状态从 evaluation_error 重试到 complete，保存独立 attempt 或明确的追加历史，同时提供“当前有效结果”查询。
5. Repository 关闭连接时也使用锁；为本地并发设置合理的 SQLite `busy_timeout`，按测试证明是否需要 WAL。不要声称支持分布式并发。
6. `rounds` 当前是按 `steps_used` 保存的检查点。将代码、字段或文档明确为 agent step snapshot，避免论文把它误写为化学优化 round。若迁移已有表风险过大，保留表名并在 payload/schema 文档中明确语义。

必须增加一条完整测试：

```text
协议 A 评估候选并持久化
→ 恢复任务
→ 用户改变会影响协议的约束，形成协议 B
→ B 查询不能复用 A
→ 真正执行 B 的新评估
→ 再次恢复
→ B 的结果可以复用且不重复计费
→ A 的历史仍可审计
```

该测试必须通过 `Harness.run` 或等价完整执行路径，不能只直接调用一个内部工具后断言异常。

阶段完成标准：Repository 定向测试通过；旧协议不会污染新证据；新协议不会被旧记录阻塞；恢复后不会重复评估当前协议已有结果。

## 四、阶段 2：把决策阶段约束变成确定性状态机

问题证据来自 pilot：`evaluate_options` 后没有达标方案，模型连续调用 `choose_strategy`；其中一次缺必填参数，后两次引用尚未 assessed 的 `planned_s1`，最终 `consecutive_errors`。

需要完成：

1. 新增唯一的 `available_actions(state)` 或同等函数。它根据 TaskState 返回当前允许的工具和每个工具允许引用的对象。
2. 模型 prompt 展示的工具、服务端 preflight 验证、Dashboard 的可用操作提示必须共享同一个判定来源，避免 prompt 隐藏了工具但 Registry 仍接受或反复报通用错误。
3. 明确状态规则：
   - 未评估母体：只允许 evaluate、pause、finish（finish 仍须事实性说明）。
   - 已评估母体且要求 planned edits：允许 propose_edits。
   - proposal 尚未 screening：允许 evaluate_options。
   - screening 有合规候选：允许 select_edit；选择后允许 execute_selected_edit。
   - screening 全部不合格：允许 propose_edits 新批次或 finish；不允许 choose_strategy。
   - 只有正式编辑、子体评估和 compare_parent_child 完成后，hypothesis 才是 assessed，此时才允许 choose_strategy。
4. 非法动作错误必须返回当前 stage、动作为什么非法、允许的工具名以及可引用 ID。错误仍计入预算，不能静默自动修正模型参数。
5. 对同一非法状态动作连续两次时，第三次不要只给相同文本后停机。确定性控制器应要求重新规划，或在无可行动作时生成事实性的停止原因；不得替模型伪造化学选择。
6. 网络错误、JSON/schema 错误、状态机错误分别统计。网络重试不应被算成“智能体科学决策失败”。

必须增加测试覆盖：

- 所有 screening 方案不合格时 `choose_strategy` 不在 available actions。
- 模型仍返回隐藏工具时服务端拒绝，并返回允许动作和 ID。
- 新 proposal 批次可以继续评估，不需要虚构 assessed hypothesis。
- 正式父子比较完成后才开放 choose_strategy。
- 暂停、恢复和用户新约束后 available actions 重新计算。

阶段完成标准：pilot 中三个 `choose_strategy` 错误均由测试覆盖；同一状态不再因动作语义冲突连续失败三次。

## 五、阶段 3：固定 MiniMax 网络传输配置

已观察到 Windows 系统代理 `127.0.0.1:12000` 使 Python/httpx 产生 TLS `UNEXPECTED_EOF_WHILE_READING`，而同一 API 主机直连成功。

需要完成：

1. 为 LLM client 增加显式、可配置的代理环境继承策略，例如 `llm.trust_env_proxy: false`，默认值必须在 README 解释。
2. 使用受支持的 httpx/OpenAI client 配置实现，不能关闭 TLS 证书校验，不能硬编码用户 API key。
3. 将 `trust_env_proxy`、base URL host、model、timeout、max retries 写入去密钥的运行 manifest/provenance。
4. 离线测试验证配置传给 client，不做真实网络请求。
5. 真实调用前只做一个最小 JSON preflight；保存是否成功、耗时和 usage，不保存 key。

阶段完成标准：benchmark 和 Harness 使用同一连接策略；不再依赖手工设置 PowerShell `NO_PROXY` 才能工作；网络错误单独出现在报告中。

## 六、阶段 4：一次新的二维正对照 v2

前三阶段测试通过后，创建新目录，不修改原 pilot：

```text
runs/diagnostic_2d_positive_v2_20260919/
```

继续使用原 pilot 的同一苯酚母体、同一 24 个操作、同一规则排序、同一 0.01 阈值、同一最多 10 个新结构评估。由于代码哈希已变化，必须重新生成 manifest 和 hash；在 README 明确 v2 是可靠性修复后的新协议，不能与 pilot 拼接成重复实验。

执行顺序：

1. prepare
2. audit，确认仍有可达解，但不能把结果传给策略
3. rule
4. agent，只运行一次，不因结果不理想重启
5. report

成功标准不是分数一定提高，而是：

- 智能体完成预算或有证据地停止；
- 没有 `consecutive_errors`、`repeated_action` 或非法 stage 动作导致暂停；
- 第一批未达 0.01 后能提出新批次或明确停止；
- 若找到达标方案，能够 select → execute → evaluate/reuse → compare → finish；
- 若未找到，报告已评估结构、最佳变化、失败原因和剩余未探索空间；
- 规则与模型的模型调用、评估数、首次命中成本、错误分类和最终 outcome 都由程序生成。

## 七、API 范围和最低修正

不要建设生产平台。只修正会影响当前本地研究验收的问题：

1. README 将 FastAPI 描述为本地研究原型，不称为可部署服务。
2. 说明 FastAPI `BackgroundTasks` 仍在 Web 进程内，不是持久 Worker。
3. API 测试不要 `monkeypatch.undo()` 撤销整个全局网络隔离。仅恢复 ASGI 测试确实需要的局部 socket 行为，或使用不触碰 socket 的同步/直接路由测试方法；mock 测试不得访问外网。
4. 增加 resume 后继续执行、终态任务拒绝恢复、并发启动同一任务只能有一个 writer、approval 枚举校验的测试。
5. 未实现鉴权前，README 只允许绑定 `127.0.0.1`，并明确 `mock:false` 可能调用付费模型。

## 八、测试和文档

每阶段先跑相关测试，全部完成后运行：

```powershell
python -m pytest -q
git diff --check
```

README 必须追加一节“Repository、阶段机与二维正对照 v2”，记录：

- 修改前的真实问题；
- 修改的文件和状态转换规则；
- 数据库事实源与 JSON checkpoint 的关系；
- 使用的模型 MiniMax-M3、连接策略和是否启用 thinking；
- 真实实验命令、预算、母体、编辑目录、阈值；
- 规则组和智能体组结果；
- 网络、schema、状态机、结构、评估错误的数量；
- 哪些结论被支持，哪些仍是 inconclusive；
- 明确没有做 3D 扩展、n=20、鉴权或分布式 Worker。

将机器可读的小型结果摘要放到允许提交的 `runs/samples/`，不要提交完整 artifacts、SQLite、缓存或日志。

最终回复必须按以下格式：

1. 完成了什么。
2. 每个验收标准对应的证据文件。
3. 测试命令和精确结果。
4. 真实模型调用次数、token、评估次数和费用未知项。
5. 实验结论和限制。
6. Git 状态；不要把本地密钥、数据库、缓存或大型运行目录加入 Git。

---

## 推荐的论文表述

如果 v2 顺利完成，可以写：

> 本研究构建了由确定性工具状态机约束的分子优化智能体。语言模型仅在当前合法动作集合中提出编辑和选择，RDKit 执行分子图操作，Repository 按评估协议保存证据。系统能在检查点恢复后拒绝旧协议结果、复用当前协议结果，并根据父子结构的实际评分选择继续、换策略或停止。

不要写“模型学会了药物设计”“证明了长期记忆有效”或“Vina 证明活性提高”。
