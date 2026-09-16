# 第一阶段：可信度修复

2026-09-13。六类工程问题已修复；本报告不宣称 Vina 已成为经实验验证的活性模型。

## 受体准备

使用 Meeko 0.8.0 的蛋白残基模板替代旧 OpenBabel StripSalts 转换流程。
默认选择 1M17 的 A 链、altloc A，排除 HETATM（包括水和共晶配体），不自动删除坏残基。
当前为干受体 + 标准模板质子化假设；不包含 pKa 预测、结构水筛选或受体集合。

| 项目 | 旧 PDBQT | 新 PDBQT |
|---|---:|---:|
| 蛋白残基 | 133 | 312 |
| 重原子 | 1080 | 2497 |
| 极性氢 | 0 | 532 |

原始 PDB 有 2511 条蛋白 ATOM 记录；选择 altloc A 后为 2497 个重原子，全部保留。
新文件位于 `data/prepared/1M17_v1.pdbqt`。旧文件保留，不被当前配置使用。
准备脚本输出源文件/受体哈希、缺失原子列表、残基计数、参数、软件版本和日志。
受体审计缺失、失败或哈希不一致时，主循环拒绝启动 docking。

复现（输出文件须不存在；已有文件请指定新版本名，并更新 config）：

```bash
python scripts/prepare_receptor.py data/1M17.pdb data/prepared/1M17_v1.pdbqt
```

## 对照身份

`data/reference_compounds.json` 保存 PubChem 返回的 afatinib、erlotinib、gefitinib
结构、CID、分子式及 InChIKey。`tools/references.py` 加载时用 RDKit 同时核对分子式与
立体化学 InChIKey。生成器和两份 benchmark 脚本共用此注册表，不再复制手写结构。

Afatinib 的正确分子式为 C24H25ClFN5O3；其共价抑制机制不能用普通 Vina 分数比较效力。
旧 README 的“超过已知抑制剂”和“按搜索框换算能量”结论已撤回。
旧样例与图表原样保留，不能合并成新版有效验证证据。

## 评分缺失和错误处理

- 结构无效：保留原始提议，状态为 `invalid_structure`，不进入排名。
- 必要工具异常或结果缺失：`evaluation_error`，综合分 null。
- 明确关闭 docking 且性质计算成功：`screening_only`，只有 property_score，综合分 null。
- 全部必要评分成功：`complete`，使用固定权重的综合分。
- Lipinski 未通过仍保留对应权重，因此确实扣分，不通过移除分母来“隐藏失败”。
- NaN/Infinity、不成功的 docking 不参与综合评分。
- 工具错误和 Mock 输出不写入正式失败记忆；描述符错误按分子隔离。
- ADMET 输出明确标为描述符启发式，不是经过训练的毒性概率或实验 ADMET。

## 反思和模型隔离

修正“本轮摘要被当作上一轮”的索引错误；分别保存 `focus_used`、`focus_next`，
Judge 得到上一轮候选 ID、SMILES、分数。窗口滚动后轮号仍递增。
Judge 读取 `llm.judge`，取消每轮 ping 和静默切换模型。
Judge 失败时保存错误与原始响应并停止本次迭代，不凭空生成备用化学建议。
采纳数仍是对展示候选的 LLM 估计，记录分母并限幅，不冒充结构变换测量。

只有显式 `--mock` 才使用模拟客户端；真实模式缺少密钥直接报错。
Mock 不读取或写入正式记忆；测试默认独立临时记忆，并禁用网络连接。
旧记忆已完整复制到 `memory/archive/pre_phase1_failed_ligands.json`，包含的测试数据和
旧协议结果均被隔离。新版真实记忆按靶点与协议哈希分桶；未来由数据库迁移器接管。

## 每条结果的可追溯性

运行目录有唯一 ID 和 manifest，已有运行目录拒绝覆盖。CLI 自动建立新目录。
每次候选保留 candidate_id、run_id、轮次、来源模型、原始 SMILES 和采用的策略。
LLM 保存提示词、原始响应、可获得的 token usage；响应解析失败仍保留原文。
运行与候选共用同一个 protocol_id，覆盖受体、审计文件、评分参数、工具代码、
Vina 二进制、SA 片段数据及软件版本。实际 docking 同时记录 seed、命令、输入哈希。
每次 docking 保存输入 SDF/PDBQT、输出姿势、stdout/stderr 和 result.json。
代码已累计可获得的 usage，但供应商不返回 usage 时不能当作精确计费账单。

## 验证结果及重要局限

离线回归：`python -m pytest -q`，38 passed、1 个真实 docking 测试默认跳过。
另行显式启用该集成测试：1 passed。3 轮 CLI Mock 初筛也成功运行，无付费 LLM 调用。

重对接脚本直接在受体坐标系中用 RDKit CalcRMS，考虑对称性，不对配体单独叠合。
参考为 1M17/A/AQ4，共 29 个重原子。

| 搜索强度 | Seed | 首位 Vina | 首位 RMSD（Å） | ≤2 Å |
|---|---:|---:|---:|---|
| 8 | 2026 | -7.283 | 1.667 | 是 |
| 8 | 2027 | -7.302 | 1.306 | 是 |
| 8 | 2028 | -7.167 | 5.839 | 否 |
| 32 | 2026 | -7.304 | 1.904 | 是 |
| 32 | 2027 | -7.309 | 1.446 | 是 |
| 32 | 2028 | -7.167 | 5.839 | 否 |

两个搜索强度均为 2/3 首位姿势通过。Seed 2028 在强度 8 的第 3 个姿势 RMSD 为
1.225 Å，但首位姿势骨架 RMSD 仍为约 5.58 Å，不能用“仅柔性侧链偏移”解释。
这表明采样能找到接近共晶的姿势，但评分排序存在局限；增加搜索强度没有解决。
所有结果保留，不能选择有利种子后宣称全通过。

还运行了正确身份的 gefitinib、afatinib 与 ibuprofen、ethanol 比较。
对照很少，且并非严格构建的活性/失活匹配数据集，不足以证明富集能力。
小规模姿势验证不等价于活性、选择性、ADMET 或药物有效性验证。
关键候选仍需多种子、多姿势检查和后续独立评估，不应只按单个 Vina 数字下结论。

复现（每次使用新的输出目录）：

```bash
python scripts/validate_protocol.py --output runs/check_exh8 --exhaustiveness 8
python scripts/validate_protocol.py --output runs/check_exh32 --exhaustiveness 32
```

可追溯数据：[phase1_validation.json](phase1_validation.json)。
原始工件在 `runs/phase1_controls/` 和 `runs/phase1_controls_exh32/`。

## 数据库接入范围

推荐 PostgreSQL + pgvector，准备步骤见 [DATABASE_SETUP.md](DATABASE_SETUP.md)。
本次未部署数据库、未创建线上表、未自动写入 PostgreSQL。第一阶段的 ID、协议和
结果状态为后续迁移与自动入库提供基础；语义向量索引是下一阶段独立工作。
