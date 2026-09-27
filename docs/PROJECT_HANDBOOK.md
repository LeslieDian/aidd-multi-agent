# AIDD Multi-Agent 项目完整操作手册

> **本文档的承诺**：**任何读者——不管你是 AI 研究员、药物化学家、完全不懂技术的高中生、还是产品经理**——按顺序读下去，都能看懂这个项目的**每一个设计决策**、每一个术语、每一段代码为什么是这样写的。
>
> 不需要你懂任何前置知识。遇到不懂的概念，我会用比喻讲清楚。遇到设计选择，我会告诉你"为什么要这样"。
>
> **本文档不假设任何背景**。

---

## 阅读方式

**从头读到尾**。这是唯一推荐的阅读方式。

不需要"先看第几章再看第几章"，不需要"如果你懂 X 就跳过"。  
每一章都从"这是什么"开始讲，到"为什么这样设计"结束。

如果你中途累了，可以停。  
任何时候回来，从上次的地方继续读就行。

---

## 目录

### 第 0 部分：基础概念（给完全不懂的人）

- [0.1 大语言模型 (LLM) 是什么](#01-大语言模型-llm-是什么)
- [0.2 智能体 (Agent) 是什么](#02-智能体-agent-是什么)
- [0.3 多智能体 (Multi-Agent) 是什么](#03-多智能体-multi-agent-是什么)
- [0.4 AI 药物发现 (AIDD) 是什么](#04-ai-药物发现-aidd-是什么)
- [0.5 化学概念速讲](#05-化学概念速讲)
- [0.6 软件工程概念速讲](#06-软件工程概念速讲)

### 第 1 部分：项目本身

- [1. 这个项目到底是什么](#1-这个项目到底是什么)
- [2. 为什么需要这个项目](#2-为什么需要这个项目)
- [3. 这个项目要解决什么具体问题](#3-这个项目要解决什么具体问题)
- [4. 项目设计遵守的 5 条原则](#4-项目设计遵守的-5-条原则)
- [5. 项目的完整目录长什么样](#5-项目的完整目录长什么样)

### 第 2 部分：怎么运行起来

- [6. 5 分钟跑通你的第一个实验](#6-5-分钟跑通你的第一个实验)
- [7. 配置文件 config.yaml 详解](#7-配置文件-configyaml-详解)
- [8. 两种运行模式](#8-两种运行模式)
- [9. 一个完整运行的全程追踪](#9-一个完整运行的全程追踪)
- [10. 30 个最常见的问题](#10-30-个最常见的问题)
- [11. 故障排查清单](#11-故障排查清单)

### 第 3 部分：核心模块

- [12. LLM 客户端](#12-llm-客户端)
- [13. 化学工具层](#13-化学工具层)
- [14. 工具 API 速查](#14-工具-api-速查)
- [15. 生成器 Agent](#15-生成器-agent)
- [16. 评估器 Agent](#16-评估器-agent)
- [17. 裁判 Agent](#17-裁判-agent)
- [18. 记忆系统](#18-记忆系统)
- [19. 持久化 Harness](#19-持久化-harness)
- [20. 状态机百科](#20-状态机百科)

### 第 4 部分：辅助系统

- [21. 数据库](#21-数据库)
- [22. 数据库 ERD 图与查询示例](#22-数据库-erd-图与查询示例)
- [23. HITL 与控制流](#23-hitl-与控制流)
- [24. 脚本与基准测试](#24-脚本与基准测试)

### 第 5 部分：设计与决策

- [25. 12 个核心架构决策（每个都是完整的故事）](#25-12-个核心架构决策每个都是完整的故事)
- [26. 为什么用 LLM 而不是传统机器学习](#26-为什么用-llm-而不是传统机器学习)
- [27. 为什么禁用 SDK 自重试](#27-为什么禁用-sdk-自重试)
- [28. 为什么需要 4 步 schema sanitizer](#28-为什么需要-4-步-schema-sanitizer)
- [29. 为什么需要 4 类记忆而不是单一黑名单](#29-为什么需要-4-类记忆而不是单一黑名单)
- [30. 为什么进度信号要区分 safety 与否](#30-为什么进度信号要区分-safety-与否)
- [31. 为什么需要 API key 双 fallback](#31-为什么需要-api-key-双-fallback)
- [32. 为什么用 Receipt 而不是简单的全状态快照](#32-为什么用-receipt-而不是简单的全状态快照)
- [33. 为什么数据库要"只追加"](#33-为什么数据库要只追加)
- [34. 为什么 Mock 和真实 LLM 客户端共享接口](#34-为什么-mock-和真实-llm-客户端共享接口)
- [35. 为什么 Catalogue summary 能修复 SMILES 错误](#35-为什么-catalogue-summary-能修复-smiles-错误)
- [36. 为什么用 LRF 淘汰失败集合](#36-为什么用-lrf-淘汰失败集合)
- [37. 为什么状态机要分阶段而不是一锅端](#37-为什么状态机要分阶段而不是一锅端)

### 第 6 部分：项目历史

- [38. 项目演进史](#38-项目演进史)
- [39. 常见误解与"不要做的事"](#39-常见误解与不要做的事)
- [40. 性能与成本估算](#40-性能与成本估算)
- [41. Phase 4.4 — 校准、可视化、立体化学（2026-09-27）](#41-phase-44--校准可视化立体化学2026-09-27)

### 附录

- [附录 A：完整目录树](#附录-a完整目录树)
- [附录 B：术语速查表](#附录-b术语速查表)
- [附录 C：实验结论摘要](#附录-c实验结论摘要)

---

# 第 0 部分：基础概念（给完全不懂的人）

> 这一章不需要任何前置知识。读完后，你会理解本项目用到的所有基本概念。

---

## 0.1 大语言模型 (LLM) 是什么

### 一句话回答

**大语言模型是一个"读过很多书、什么话题都能聊几句的 AI 聊天机器人"**。

### 详细解释

想象你养了一只鹦鹉。这只鹦鹉非常特别：

- 它读过人类写过的几乎所有书籍、文章、网页；
- 它能根据你前面说的话，**猜出下一个最可能的字**；
- 一个字一个字猜下去，就能拼出一整段通顺的话。

这就是**大语言模型**的核心原理。它做的事情本质上就是：**根据前面的字，预测下一个字**。

### 它怎么"懂"化学？

鹦鹉读过很多书，但它不"懂"书的内容——它只是记住了"某些字常跟在某些字后面"。

LLM 也一样。它**不真正懂**化学，但它读过的化学书足够多，所以：

- 当你问"苯酚的 SMILES 是什么"，它能答出来；
- 当你问"哪种分子能结合 EGFR"，它能说出"带芳香环 + hinge 结合 + 适度 logP"；
- 当你让它"生成 5 个 EGFR 抑制剂"，它能写出看起来像样的 SMILES。

它靠的是**统计模式**，不是真的理解。但因为训练数据太大、模式太丰富，效果惊人地好。

### 它能做什么？

| 它擅长 | 举例 |
|---|---|
| 写文章 | 写邮件、写报告 |
| 翻译 | 中英文互译 |
| 问答 | "什么是肺癌？" |
| **生成结构化输出** | 给一段 JSON 格式的输出 |
| 推理 | "A=B, B=C, 那么 A=C 吗？" |
| 角色扮演 | "假设你是医生，告诉我……" |

### 它不能做什么？

| 它做不到 | 为什么 |
|---|---|
| 上网搜索 | 它只是文字游戏，没有浏览器 |
| 执行代码 | 它只能输出文字 |
| 100% 准确 | 本质是猜，只是猜得很准 |
| 记住很久以前的话 | "记忆"有限 |

### 本项目用什么 LLM？

本项目用 **MiniMax-M3**（MiniMax 公司的大模型）。  
它通过 HTTP API 调用，就像访问一个网站一样。  
配置在 `config.yaml`：

```yaml
llm:
  providers:
    MiniMax:
      base_url: https://api.minimaxi.com/v1
      model: MiniMax-M3
      api_key_env: MiniMax_API_KEY
```

API key 是一个密码，放在 `.env` 文件里。

---

## 0.2 智能体 (Agent) 是什么

### 一句话回答

**智能体是一个"会用工具的 AI"——它不仅能聊天，还能做事**。

### 详细解释

普通 LLM 只能"聊天"。智能体不一样，它能做**完整的工作循环**：

```
观察 → 思考 → 行动 → 回到观察
```

每一步：

1. **观察**：看现在是什么状态（比如"我已经评估了 5 个分子"）；
2. **思考**：用 LLM 决定下一步做什么（比如"第 6 个分子应该对接 Vina"）；
3. **行动**：执行（调用 Vina 工具）；
4. 回到第 1 步，看结果，再决定下一步。

### 比喻：自动驾驶

> 普通 LLM 像乘客，只能告诉你"前面该左转"。  
> 智能体像自动驾驶汽车，自己看路况、自己转方向盘、自己踩油门。

### 智能体的"工具"是什么？

工具（Tool）是智能体能调用的**预先定义好的函数**。

比如一个"计算器工具"：

```python
def calculate(expression: str) -> float:
    """计算数学表达式并返回结果"""
    return eval(expression)
```

智能体在思考时，会输出这样的结构化文本：

```json
{
  "action": "calculate",
  "arguments": {"expression": "2 + 3 * 4"}
}
```

然后**系统**（不是 LLM 本身）会调用对应的 Python 函数，把结果返回给智能体。

### 为什么不让 LLM 直接执行？

LLM 输出的只是**文字**，它本身不能执行代码。

就像你写的菜谱给机器人看，机器人需要**机械臂**才能真正做菜。  
智能体的"机械臂"就是工具函数。

### 智能体如何避免乱来？

智能体有一个**工具清单**（Tool Registry）：事先规定哪些工具能用。

当 LLM 想调用不存在的工具或参数错误，系统会**拒绝执行**并把错误告诉 LLM，让它重新决策。

---

## 0.3 多智能体 (Multi-Agent) 是什么

### 一句话回答

**多智能体是多个 AI 智能体扮演不同角色，分工合作完成复杂任务**。

### 详细解释

一个智能体擅长一件事。复杂任务需要多个智能体**分工合作**。

### 比喻：医院看病

> 你不舒服去医院，遇到的不是一个人，而是**一个团队**：
>
> - **内科医生**：先问症状，初步判断；
> - **化验师**：抽血化验，拿数据；
> - **放射科**：做 CT，看片子；
> - **资深主任**：综合所有信息，做最终诊断；
> - **护士**：执行医嘱。
>
> 每个人只擅长一件事，但**协同**就能给病人最好的治疗。

### 本项目的 AI 团队

| AI 角色 | 干什么 | 对应医院的谁 |
|---|---|---|
| **Generator（生成器）** | 设计新分子 SMILES | 资深药物化学家 |
| **Evaluator（评估器）** | 计算每个分子的性质、对接、安全性 | 化验室 + 实验仪器 |
| **Judge（裁判）** | 综合分析，决定下一步方向 | 主任医师 |
| **Harness（执行引擎）** | 让各部门协同 | IT 系统 |
| **Database（数据库）** | 把所有信息存起来 | 病案室 |

### 多智能体的好处

1. **每个智能体只擅长一件事**：可以独立优化、测试；
2. **互相检查**：生成器给的分子被评估器"打分"；
3. **可以分工**：评估慢的活同时做；
4. **失败隔离**：一个智能体出错不会拖垮整个系统。

### 多智能体的挑战

1. **协调成本**：智能体之间需要通信协议；
2. **状态管理**：每个智能体要知道"现在做到哪一步"；
3. **错误传播**：一个智能体犯错可能影响下游；
4. **资源消耗**：多个 LLM 调用 = 多个 API 请求 = 花钱。

---

## 0.4 AI 药物发现 (AIDD) 是什么

### 一句话回答

**AIDD 是用 AI（特别是 LLM + 多智能体）设计新药分子**。

### 为什么这件事重要？

新药研发平均需要 **10-15 年**、**20-30 亿美元**。  
其中大部分时间花在"试错"上：化学家设计 → 合成 → 测活性 → 失败 → 再设计。

如果能用 AI **提前把没希望的分子淘汰掉**，化学家就能专注最有希望的少数几个，节省几年时间和几亿美元。

### 传统流程 vs AI 辅助流程

**传统流程**（10+ 年）：

```
化学家凭经验设计分子  →  化学家合成  →  动物测试  →  人体临床试验  →  上市
   几周                  几个月          几年           几年
```

**AI 辅助流程**：

```
AI 生成候选分子  →  自动打分  →  AI 选最有希望的  →  合成  →  动物/临床
   几秒             几小时        几小时           几个月    几年
```

### 本项目做的事

1. AI 设计成百上千个候选分子（用 LLM）；
2. 自动给每个分子打分（用化学工具）；
3. 选出最有希望的（再用 LLM）；
4. 输出报告给化学家。

### ⚠️ 重要免责

**本项目只做"分子设计 + 计算评估"**这一步。  
**它不做真正的化学合成，不做生物实验，不做临床试验**。  
它的价值是：**让化学家少走弯路**。

### 本项目的目标靶点

**EGFR**（表皮生长因子受体）：

- 是一种控制细胞生长的蛋白质；
- 很多肺癌（特别是非小细胞肺癌）的 EGFR 突变导致它过度活跃；
- 治疗思路：设计小分子"堵住"它的活性口袋。

本项目用 **PDB ID: 1M17**（EGFR 的实验解析结构）。

---

## 0.5 化学概念速讲

> 这一章把后面会用到的所有化学术语翻译成大白话。

### SMILES —— 分子的"字符串身份证"

把分子结构写成一串字符。

| 分子 | SMILES |
|---|---|
| 水 | `O` |
| 乙醇 | `CCO` |
| 苯 | `c1ccccc1` |
| 阿司匹林 | `CC(=O)Oc1ccccc1C(=O)O` |

为什么用字符串？因为可以像普通文本一样搜索、存储、计算相似度。

### 分子对接 (Docking) —— 钥匙和锁

> 想象一把钥匙（你的分子）和一把锁（蛋白质口袋）。  
> 分子对接 = 计算钥匙能插进锁多深。

**关键概念**：

- **结合自由能 (kcal/mol)**：负得越多，结合得越紧。`-10` 比 `-5` 好。
- **重要免责**：对接分数是**估算**，不是实测。

本项目用 **AutoDock Vina** 软件。

### ADMET —— 药物的"五项全能"

| 缩写 | 中文 | 大白话 |
|---|---|---|
| **A**bsorption | 吸收 | 吃下去能被肠道吸收吗？ |
| **D**istribution | 分布 | 能不能到达目标器官？ |
| **M**etabolism | 代谢 | 肝脏会不会把它分解掉？ |
| **E**xcretion | 排泄 | 能不能从尿/粪便排出？ |
| **T**oxicity | 毒性 | 有没有毒副作用？ |

### hERG —— 心脏毒性

心脏细胞上有一种叫 hERG 的离子通道。  
很多药物会**意外地**堵住它，导致心律失常。  
**hERG 风险高的分子不能用**。

### logP —— 亲油还是亲水

分子在"油"和"水"之间的分配系数。

- logP 大 → 亲油（像油一样）；
- logP 小 → 亲水（像糖一样）。

**经验法则**：药物 logP 在 1-4 之间比较好。

### 分子骨架 (Scaffold) —— 核心环

分子的"骨架"是去掉所有侧链后剩下的核心环结构。  
就像雕塑的"骨架"是核心形状，装饰品可以变。

### Tanimoto 相似度 —— 两个分子有多像

0 到 1 之间的数：

- 1 = 完全相同；
- 0.7 以上 = 非常相似。

### SA Score —— 合成难度

1 到 10：

- 1-3 = 容易合成；
- 7-10 = 极难合成。

### 类药性 / Lipinski 五规则

5 条经验法则，过滤"不像药的分子"：

| 规则 | 阈值 |
|---|---|
| 分子量 ≤ 500 Da | |
| logP ≤ 5 | |
| 氢键供体 ≤ 5 | |
| 氢键受体 ≤ 10 | |

### 帕累托前沿 (Pareto Front) —— "多目标最优集"

当有多个目标（比如既要 vina 高、又要 hERG 低），没有任何分子在所有目标上都最好。  
帕累托前沿就是"不被任何其他分子支配的集合"——你想改进任何一个目标，都得牺牲另一个。

### ATP 结合口袋 —— EGFR 的"锁眼"

EGFR 蛋白质上有一个凹进去的小坑（ATP 结合位点）。  
本项目要设计的分子要"插进"这个小坑，堵住 EGFR。

---

## 0.6 软件工程概念速讲

### 缓存 (Cache) —— "上次算过的不再算"

把"算过的结果"存起来，下次直接拿。

```
第 1 轮：要评估分子 A
  → 计算对接分数 -7.5 → 缓存
第 5 轮：又要评估分子 A
  → 检查缓存 → 发现有 → 直接用 -7.5，不再重新对接
```

### 哈希 / 指纹 (Hash) —— 给数据一个指纹

把任意长的数据变成固定长的"指纹"：

```
"hello" → sha256 → 2cf24dba5fb0a30e...
```

特性：

- 同样的输入 → 同样的输出；
- 不同输入 → 大概率不同；
- 没法从指纹反推原内容。

**本项目的用途**：

- 验证文件没被改过；
- 给"评估协议"一个指纹（protocol_id）。

### 协议 ID (Protocol ID) —— "我这个实验怎么做的"

不同的实验参数 → 不同的 protocol_id → 不能混在一起。

改 `vina.exhaustiveness: 16 → 32` → 不同的 protocol_id → 缓存全部失效。

### 检查点 (Checkpoint) —— 存档

任务跑到一半 → 存档（`task.json`） → 进程崩溃  
重启 → 读取存档 → 从这里继续。

### 持久化 (Persistence) —— 数据不会丢

把内存数据写到硬盘。本项目**双写**：JSON 文件 + SQLite 数据库。

### 并行 (Parallel) —— 多件事同时做

10 个分子要对接，开 4 个进程同时做 → 时间缩短到 1/4。

### 异步 (Async) —— 等的时候不阻塞

HTTP 等待响应时，可以同时处理其他任务。

### 测试套件 (Test Suite) —— 自动检查代码

301 个测试，每次改代码都跑一遍，看有没有改坏。  
本项目：**301 passed, 1 skipped**。

### 只追加 (Append-only) —— 已经写的事不能改

数据库触发器拒绝任何 UPDATE。  
**事实不可篡改**。

### 沙箱 (Sandbox) —— 隔离的执行环境

测试时用 autouse fixture 阻断 socket，保证 CI 离线可跑。

---

# 第 1 部分：项目本身

---

## 1. 这个项目到底是什么

### 一句话回答

**这是一个用多个 AI 智能体协同工作，代替药物化学家，自动设计能治疗肺癌的新分子的工具**。

### 它不是一个能治病的项目

这个项目**不能**直接治病。它只做**计算评估**这一步：
- ✅ 设计分子（LLM 推测）；
- ✅ 评估分子（计算打分）；
- ❌ 合成（需要化学实验室）；
- ❌ 测试（需要生物实验室）；
- ❌ 上市（需要 10+ 年的临床试验）。

它的价值是：**让化学家把"最没希望的分子"先淘汰掉**，把精力集中在最有希望的几个上。

### 它不是一个完美的工具

它的输出**可能错**：

- Vina 分数是**估算**，不是实测；
- ADMET 是**描述符启发式**，不是真正的 ADMET 预测；
- LLM 生成的分子可能不合化学规律。

它是一个**研究工具**，帮你加速探索，不是替你做决策。

---

## 2. 为什么需要这个项目

### 药物发现的核心矛盾

新药研发平均需要 **10-15 年**、**20-30 亿美元**。  
其中大部分成本花在"试错"上。

### 计算辅助药物设计 (CADD) 的局限

传统 CADD 用分子指纹相似性搜索、QSAR 等方法生成候选。  
这些方法依赖专家设计的特征，**每个新靶点都要重新写策略**。

### AI 的机会

LLM 在训练时读过了大量化学文献。它**已经"知道"**：

- 什么样的分子可能是好的；
- 各种药物的共同结构特征；
- 化学反应的可行性。

如果能让 LLM **自主决策**该探索哪个方向，而不是依赖专家手写策略，理论上能加速药物发现。

### 本项目的定位

本项目不是要取代化学家，而是：

- **加速**：把化学家从重复劳动中解放出来；
- **补盲**：AI 能探索人类专家没考虑过的方向；
- **可重现**：所有决策都有完整日志。

---

## 3. 这个项目要解决什么具体问题

### 问题 1：药物发现太慢

**传统**：化学家设计 → 合成 → 测活性 → 失败 → 再设计  
**AI 辅助**：AI 设计 → 自动打分 → AI 选最有希望的 → 化学家合成 → 测活性

→ 把"合成—测活性"这一步推迟到最有希望的分子，节省时间。

### 问题 2：AI 容易出错

LLM 不是完美的。它可能：

- 输出不合法的 SMILES；
- 重复尝试已知失败的分子；
- 在错误的时机调用错误的工具；
- 卡在死循环里不出来。

本项目用**多层防护**：

- schema sanitizer：自动修复模型的小错误；
- 4 类记忆：避免重复失败；
- 状态机：不允许越界；
- 强基线对比：知道 AI 是不是真的有效。

### 问题 3：实验难以重现

传统 AI 研究常被批评"不可重现"。本项目用**严格的工程纪律**：

- 所有评分参数在 `config.yaml`（可审计）；
- `protocol_id` 指纹保证缓存不会错乱；
- `manifest.json` 记录每次运行的代码哈希；
- 数据库"只追加"，任何改动都有痕迹。

### 问题 4：实验难以分析

每次运行都生成完整的结构化数据：

- `runs/<run>/{manifest, round_*, summary}.json`；
- SQLite 可查询；
- Postgres 可跨 campaign 长期存储。

---

## 4. 项目设计遵守的 5 条原则

> 每条原则后面，我会**详细解释为什么这样设计**。

### 原则 1：事实可追加、不可改写

**大白话**：已经记下来的事不能偷偷改。

**为什么这样设计**：在合规和科学诚信上有重要意义。如果允许改写历史数据，研究者可能"事后调参"让结果看起来更好。本项目用 PostgreSQL 触发器拒绝任何 UPDATE；SQLite 用 SHA-256 校验。

### 原则 2：评分与阈值完全可配置、可审计

**大白话**：所有打分规则都写在配置文件里。

**为什么这样设计**：评分参数直接影响实验结论。如果藏在代码里，未来读者无法知道"为什么 vina < -7.0 算合格"。把规则写在 YAML 里 → 自动哈希为 `protocol_id` → 不同协议不混缓存。

### 原则 3：决策可回滚、可重放

**大白话**：每一步都有"存档点"，出错了可以从最近存档恢复。

**为什么这样设计**：长任务可能崩溃或重启。Receipt 机制保证**幂等**：即使重复调用同一个工具，也不会产生副作用。

### 原则 4：错误信息要"说出合法下一步"

**大白话**：拒绝一个动作时，不仅说"不允许"，还要说"应该做什么"。

**为什么这样设计**：模型的"小错误"是常见的。如果只说"不允许"，模型会猜三次然后用尽错误预算。但如果明确告诉它"应该填 X、可以选 Y"，它一次就能改正。这是项目后期最重要的工程纪律。

### 原则 5：任何外部输入（含模型）都被规约

**大白话**：LLM 的输出必须经过校验才能用。

**为什么这样设计**：LLM 本质是"猜"，不信任它说的任何东西。所有 LLM 输出进 `LlmPolicy.decide()` 后由 `validate(action)` 强制信封（`tool`/`arguments`/`reason`），自由文本字段在重复护栏里被归一化。

---

## 5. 项目的完整目录长什么样

> 这一章让你对项目的"地形"有完整印象。即使每个文件你都不知道是干什么的，至少知道它们大致属于哪个类别。

### 顶层结构

```
AIDD agent/
├── config.yaml        ← 主配置（项目唯一可信源）
├── loop.py            ← 基准多轮闭环入口
├── agent_task.py      ← 持久化任务 CLI
├── agent_dashboard.py ← 本地 Web 控制台
├── api.py             ← FastAPI 服务
├── agents/            ← 所有智能体与持久化逻辑
├── tools/             ← 化学工具层
├── db/                ← 数据库层
├── scripts/           ← 操作/分析脚本
├── benchmarks/        ← 受控基准
├── data/              ← 蛋白结构 + 参比化合物
├── experiments/       ← 矩阵/报告/汇总
├── notebooks/         ← 离线分析
├── runs/              ← 运行产物（git 忽略）
├── tests/             ← 测试套件（301 通过）
└── docs/              ← 文档（含本文档）
```

### `agents/` 子目录

这是项目最核心的目录——所有 AI 智能体都在这里。

```
agents/
├── llm.py            ← LLM 客户端（OpenAI 兼容）
├── generator.py      ← 生成器 Agent
├── evaluator.py      ← 评估器 Agent（deterministic）
├── judge.py          ← 裁判 Agent
├── working_memory.py ← 工作记忆（短期）
├── rule_memory.py    ← 4 类规则记忆
├── failed_set.py     ← 失败集合（黑名单）
├── loop_controller.py ← 终止条件
├── hitl.py           ← 人在环检查点
├── agent_metrics.py  ← 聚合指标
├── redaction.py      ← 防 secret 泄漏
└── harness/          ← 持久化 Harness 子系统
```

### `tools/` 子目录

化学工具——给分子打分的"实验仪器"。

```
tools/
├── validate_mol.py   ← SMILES 解析 + Lipinski + SA
├── admet_score.py    ← ADMET 启发式
├── calibrated_herg.py ← 7 特征 logistic hERG
├── dock_score.py     ← AutoDock Vina 包装
├── diversity.py      ← 骨架 + Tanimoto
├── evaluation_cache.py ← 按 protocol_id 的属性评估缓存
├── docking_cache.py  ← 按 docking_protocol_id 的对接缓存
├── provenance.py     ← digest/file_hash/versions
├── references.py     ← 参比化合物 + SAR 文本
├── sascorer.py       ← SA 评分（vendored）
├── fpscores.pkl.gz   ← SA 评分模型
└── vina.exe          ← Vina 二进制（Windows）
```

### `db/` 子目录

```
db/
├── schema.sql        ← PostgreSQL + RDKit + pgvector
├── access.py         ← Postgres 访问层
├── repository.py     ← SQLite 仓储（Harness 状态）
└── patch_constraints.sql ← 约束补丁
```

---

# 第 2 部分：怎么运行起来

---

## 6. 5 分钟跑通你的第一个实验

> 这一章假设你**什么都不懂**，跟着步骤做就行。

### 第 1 分钟：环境准备

```bash
# 创建 conda 环境
conda create -n aidd python=3.10 -y
conda activate aidd

# 安装依赖
pip install -r requirements.txt
```

### 第 2 分钟：受体准备

```bash
python scripts/prepare_receptor.py --pdb data/1M17.pdb --het AQ4
```

输出：

- `data/prepared/1M17_v1.pdbqt`：受体文件；
- `data/prepared/1M17_v1.pdbqt.audit.json`：audit。

### 第 3 分钟：跑测试（验证安装成功）

```bash
python -m pytest -q
```

应该看到：`301 passed, 1 skipped`。

### 第 4 分钟：跑 mock 模式（不需要 API key）

```bash
python loop.py --mock --rounds 2 --output runs/demo
```

输出：`runs/demo/{manifest.json, proposals_0.json, round_0.json, ...}`。

### 第 5 分钟：查看结果

```bash
cat runs/demo/summary.json
```

---

## 7. 配置文件 config.yaml 详解

### `target` 段 —— 靶点

```yaml
target:
  name: EGFR
  pdb_id: "1M17"
  receptor_pdbqt: data/prepared/1M17_v1.pdbqt
  pocket:
    center_x: 22.014
    center_y: 0.253
    center_z: 52.794
    size_x: 27.7
    size_y: 16.7
    size_z: 19.1
```

**大白话**：EGFR 的活性口袋在蛋白质的 `(22.014, 0.253, 52.794)` 位置，大小 `(27.7, 16.7, 19.1)` Å。  
分子要嵌进这个盒子才能对接。

### `llm` 段 —— LLM 配置

```yaml
llm:
  trust_env_proxy: false
  judge: judge_MiniMax
  providers:
    MiniMax:
      base_url: https://api.minimaxi.com/v1
      api_key_env: MiniMax_API_KEY
      api_key_env_fallbacks: [MiniMax_API_KEY_SECONDARY]   # 双 key fallback
      model: MiniMax-M3
      temperature: 1.0
      max_tokens: 2048
      extra_body: { thinking: { type: disabled } }          # 生成器关 thinking
    judge_MiniMax:
      base_url: https://api.minimaxi.com/v1
      api_key_env: MiniMax_API_KEY
      model: MiniMax-M3
      temperature: 0.3
      max_tokens: 4096
      extra_body: { thinking: { type: adaptive } }          # 裁判开 thinking
  generators: [MiniMax]
```

**为什么有 MiniMax 和 judge_MiniMax 两个 provider**？

- 同一个模型，但**参数不同**；
- 生成器：`temperature=1.0`、thinking 关闭——需要稳定 JSON；
- 裁判：`temperature=0.3`、thinking 自适应——需要深思熟虑。

### `scoring` 段 —— 评分规则

```yaml
scoring:
  objective:
    weights: { property: 0.55, vina: 0.30, herg_safety: 0.15 }
  pareto:
    safety: { max_herg_risk_score: 0.55, max_logp: 4.5 }
  vina:
    accept_below: -7.0          # vina < -7.0 才"可能合格"
    exhaustiveness: 16
    cpu: 2
  failed_set:
    composite_floor: 0.5
    max_size: 500
    embeddings: { enabled: true, threshold: 0.85 }
```

**为什么 vina accept_below = -7.0**：对接分数是估算。`-7.0` 是一个**临时门槛**，用于排序，不意味着"真的能结合"。

**为什么需要 safety gate**：`herg_risk > 0.55` 的分子**不能用**，即使 vina 很低。

### `loop` 段 —— 主循环

```yaml
loop:
  max_rounds: 12
  candidates_per_round_per_generator: 15
  early_stop_patience: 3
  progress_signal: safe_vina
  judge_enabled: true
  memory_enabled: true
  failed_set_enabled: true
  evaluation_cache_enabled: true
```

**为什么 progress_signal 用 safe_vina**：

- 传统 `vina`：跟踪所有候选的最佳对接分数；
- `safe_vina`：只跟踪**安全性通过**的候选的最佳对接分数。

2026-09-16 confirmatory 实验发现，传统 `vina` 让模型**奖励**高对接、高 hERG 的分子（危险）。`safe_vina` 避免这个陷阱。

### `harness` 段 —— 持久化执行

```yaml
harness:
  planner: MiniMax
  max_attempts: 3
  retry_base_delay: 1.0
  retry_max_delay: 30.0
  retry_jitter: 0.25
```

**为什么需要 max_attempts + backoff**：网络抖动不可避免。指数 backoff 给网络恢复时间，jitter 避免"惊群效应"。

---

## 8. 两种运行模式

### 模式 1：基准模式（loop.py）

**大白话**：像"一次性播放的电影"——跑 5 轮就结束。

```bash
python loop.py --rounds 5
```

### 模式 2：持久化模式（agent_task.py / api.py）

**大白话**：像"可以暂停的电视剧"——可以暂停、恢复、改方向。

```bash
# CLI 模式
python agent_task.py start --goal "..." --task-dir runs/demo
python agent_task.py status --task-dir runs/demo
python agent_task.py pause --task-dir runs/demo
python agent_task.py resume --task-dir runs/demo

# Web 控制台
python agent_dashboard.py --port 8765

# HTTP API
uvicorn api:app --host 127.0.0.1 --port 8000
```

### 两种模式共用

它们共用所有底层代码（generator、evaluator、judge、记忆、工具）。  
只是"运行入口"和"持久化方式"不同。

---

## 9. 一个完整运行的全程追踪

> 这一章用 `python loop.py --mock --rounds 1` 作为例子，**逐行追踪**整个过程。

### 第一阶段：参数解析与配置加载

```python
parser = argparse.ArgumentParser()
args = parser.parse_args()
config = load_config("config.yaml")
```

发生了什么：解析 `--mock --rounds 1` 等参数；加载 `config.yaml` 为嵌套字典。

### 第二阶段：协议指纹计算

```python
protocol = evaluation_protocol(config["target"], config["scoring"], dock_enabled=True)
protocol_id = digest(protocol)
# 例如：protocol_id = "a3f8c9..."（64 个十六进制字符）
```

**为什么需要 protocol_id**：把"评估参数"压缩成指纹。任何参数改动 → 不同指纹 → 不同缓存目录。  
**这避免了**："改了参数但用了旧缓存"导致结果错乱。

### 第三阶段：缓存与内存初始化

```python
evaluation_cache = EvaluationCache(memory_base / "_evaluation_cache", protocol_id, enabled=True)
docking_cache = DockingCache(memory_base / "_docking_cache", docking_protocol_id, enabled=True)
```

发生了什么：建立协议 ID 隔离的缓存目录。第一次跑会创建空目录。

### 第四阶段：写 manifest.json

```python
manifest = {
    "schema_version": 2,
    "run_id": run_id,
    "is_mock": True,
    "protocol_id": protocol_id,
    "code_hashes": {str(p): file_hash(p) for p in all .py files},
}
```

**为什么需要 manifest.json**：未来可以通过这个文件知道"这次运行用了什么代码、什么配置"。任何代码改动都会让哈希不一致。

### 第五阶段：循环第 0 轮

#### 步骤 5.1：generator 生成候选

```python
gen_results = generate_candidates(
    config=config,
    providers=["MiniMax"],
    n_per_provider=15,
    use_mock=True,   # ⭐ mock 模式
)
```

**内部发生**：`MockLLMClient.chat()` 返回 15 个固定 SMILES（循环 5 个药物分子 3 次）。  
**输出**：`proposals_0.json`。

#### 步骤 5.2：evaluator 评估候选

```python
candidates = flatten_generator_results(gen_results)
candidates, filter_stats = filter_candidates_for_evaluation(candidates, failed_set, similarity_mode="report")
evaluated, cache_stats = evaluate_candidates_with_cache(
    candidates, config["scoring"], config["target"], dock_enabled=True,
    artifact_dir="runs/demo/_docking", cache=evaluation_cache, docking_cache=docking_cache,
)
```

**内部发生**：

1. **filter**：去重精确匹配（mock 5 个 SMILES 循环 3 次 → 5 个唯一）；
2. **validate**：每个 SMILES 调 `validate_smiles()` → MW、logP、SA score；
3. **admet**：每个 SMILES 调 `estimate_admet()` → ADMET 分、hERG 风险；
4. **dock**：每个 SMILES 调 `dock_smiles()` → 调 Vina → kcal/mol；
5. **merge**：计算 composite_score；
6. **pareto**：计算 Pareto 排名。

**输出**：`round_0.json`。

#### 步骤 5.3：judge 评估

```python
judgment = judge_round(
    enriched=evaluated,
    config=config,
    round_num=0,
    use_mock=True,
)
```

**输出**：`judgments_0.json`。

#### 步骤 5.4：memory 更新

```python
memory.add_round(evaluated, focus=judgment["focus"])
state.note_round_result(summarize_round(evaluated)["best_vina"])
```

**发生了什么**：把本轮摘要加入 WorkingMemory；更新 LoopState 的最佳分数记录。

### 第六阶段：终止判断

```python
stop, reason = loop_controller.should_stop(state)
# round=0 < max_rounds=1 → 不停止
```

### 第七阶段：写 summary.json

```python
summary = {
    "run_id": run_id,
    "rounds_completed": 1,
    "history": summary_history,
    "metrics": compute_agent_metrics(summary_history, judgments, loop_state_dict),
}
```

### 完整时序图

```
T=0      命令行解析
T=0.1s   加载 config.yaml
T=0.1s   计算 protocol_id
T=0.1s   创建缓存目录 + 写 manifest.json
T=0.2s   LoopController.should_stop → 继续
T=0.2s   generate_candidates() 调用 MockLLMClient
T=0.3s   MockLLMClient 返回 15 个 SMILES
T=0.4s   filter 去重
T=2.6s   evaluator 评估（Vina 慢）
T=2.7s   judge_round 调用 MockLLMClient
T=2.7s   memory.add_round 更新
T=2.7s   LoopController.should_stop → 继续
T=2.7s   第 1 轮（同样流程）
T=5.0s   LoopController.should_stop → 停止
T=5.0s   写 summary.json
T=5.0s   退出
```

---

## 10. 30 个最常见的问题

### Q1：这个项目能治好癌症吗？

**不能**。本项目只做**分子设计 + 计算评估**，不做合成、不做生物实验、不做临床试验。

### Q2：Vina 分数 -8 kcal/mol 是不是个好药？

**不是**。Vina 是**估算**，不是实测。需要生物实验验证。

### Q3：为什么不用 DeepSeek？

DeepSeek 的 provider 块被**刻意注释**。原因：

- 本项目只用一个 provider（MiniMax）作为对照；
- 多 provider 会让实验难以对比；
- 防止误用造成预算浪费。

### Q4：没有 API key 也能跑吗？

**能**，用 mock 模式：`python loop.py --mock`。

### Q5：模型发疯（输出错误 JSON）怎么办？

4 步 schema sanitizer 会自动修复大部分常见错误。详见第 28 章。

### Q6：跑了一半进程崩溃了怎么办？

持久化模式有 **checkpoint + receipt** 机制。重启任务自动从最近的 receipt 恢复。

### Q7：缓存是什么？什么时候失效？

缓存存"算过的评估结果"，按 **protocol_id** 分目录。  
改任何 config.yaml 里的 scoring 或 target 参数 → protocol_id 变 → 缓存自动失效。

### Q8：可以换其他靶点吗？

可以，但需要修改 `target` 段，并改 `agents/generator.py::SYSTEM_PROMPT`（当前是 EGFR 专用）。

### Q9：怎么换 LLM？

在 `config.yaml` 加新的 provider 块，然后在 `generators` 和 `judge` 字段里引用。

### Q10：所有缓存会占多少硬盘？

每个分子缓存 1-10 KB。1000 个分子 ≈ 10 MB。

### Q11：跑一次要花多少钱？

5 轮实验约 10-20 次 LLM 调用。按 MiniMax-M3 价格：约 ¥0.03-0.07。

### Q12：模型必须用 MiniMax 吗？

**不必须**。任何 OpenAI 兼容服务都可以。

### Q13：Pareto front 是什么？

"在多目标上不被任何其他解支配的集合"。

### Q14：怎么知道模型学到了什么？

看 `agents/rule_memory.py::RuleStore`：4 类规则记录了模型学到的经验。

### Q15：一次跑多久？

- Mock：几秒；
- 真实 + 5 轮：5-15 分钟；
- 真实 + 12 轮：15-40 分钟。

### Q16：可以加 GPU 加速吗？

可以但用处有限——Vina 是 CPU 密集；LLM 在远程 API；embedding 用 CPU 已够用。

### Q17：怎么并行跑多个实验？

每个实验用不同的 `--output` 目录即可。

### Q18：模型产生的 SMILES 不合法怎么办？

`tools/validate_mol.py` 会捕获并记录为 `invalid_structure`。

### Q19：怎么把结果画成图？

用 `notebooks/01_legality_curve.py` 到 `04_deep_dive_compare.py`。

### Q20：怎么验证实验可以复现？

每个运行都会写 `manifest.json`，包含代码 SHA256、协议 ID 等。

### Q21：持久化模式能跑多久？

理论上**永久**。实际受 max_steps（默认 20）、max_model_calls（默认 40）等限制。

### Q22：怎么调试 LLM 调用？

每个 `model_request` 事件被记录到 `task.json::events`，包含延迟、状态码、异常、token 用量等。

### Q23：怎么审计可重现性？

`db/schema.sql` 的所有事实表都是**只追加**，UPDATE 被触发器拒绝（错误码 55000）。

### Q24：模型会不会"作弊"（输出在训练数据里见过的分子）？

理论上可能。但 catalogue（冻结目录）**对策略隐藏**，模型只能自由生成。

### Q25：项目有 bug 怎么办？

1. 看第 11 章"故障排查"；
2. 跑 `pytest -q`；
3. 联系维护者。

### Q26：为什么 ADMET 是"启发式"？

因为它不是经过训练的机器学习模型，只是 RDKit 描述符的算术组合。每个数字的含义是**可解释的**，但**不是实测的 ADMET**。

### Q27：为什么 vina 必须用特定参数？

因为对接分数依赖搜索强度。`exhaustiveness=16` 比 `exhaustiveness=4` 更精确但慢。改这个参数 → protocol_id 变 → 缓存全部失效。

### Q28：为什么需要"安全门"（safety gate）？

因为对接分数最好的分子，hERG 风险可能也很高（危险）。`safety_gate_pass` 强制要求 `hERG ≤ 0.55 ∧ logP ≤ 4.5`，避免优化对接分数时牺牲安全性。

### Q29：为什么用 Receipt 而不是简单的"全状态快照"？

简单的全状态快照有两个问题：

1. **崩溃时已完成的工具调用会重复执行**——产生副作用；
2. **大状态序列化慢**。

Receipt 机制在每个工具调用执行前写快照，崩溃后从最近的快照恢复，**幂等**且快速。

### Q30：为什么不用 LangChain / AutoGPT 这类框架？

本项目**自己实现**所有基础设施，原因：

- 框架抽象太多，难以审计；
- 错误处理策略不可控；
- 项目需要"4 步 sanitizer"、"重复护栏"、"语义参数比较"等定制化功能，框架不直接提供。

---

## 11. 故障排查清单

| 现象 | 根因 | 排查 / 修复 |
|---|---|---|
| `0/3 connection gate` | API key 缺失 / 429 / 网络 | 重跑 `check_minimax_connectivity.py` |
| `schema_errors > 0` | 模型没严格用 `{tool, arguments, reason}` 信封 | 检查 sanitizer |
| `execution_failure` | 连续 3 个工具调用失败 | 看 events 中连续的 error |
| `decision_loop` | 已合格子代还在 choose_strategy | 检查 `qualifying_candidate_found` |
| `cache miss` 总是 100% | protocol_id 错了 | `provenance.digest` 重算 |
| `Receptor preparation audit failed` | pdbqt 被改 | 重跑 `prepare_receptor.py` |
| `Vina exit 137` | OOM | 调小 cpu/workers |

---

# 第 3 部分：核心模块

> 第 3 部分详细讲每个核心模块。**每一个模块我都会先解释"它解决什么问题"，再讲"怎么实现的"，最后讲"为什么这样设计"**。

---

## 12. LLM 客户端

### 它解决什么问题

**所有和外部 LLM API 通信的代码都集中在一个地方**。  
好处：

- 集中处理 API key、429、网络错误；
- 集中记录"每次 HTTP 请求发生了什么"；
- 测试可以用 Mock 替换。

### 它必须满足的 7 条硬约束

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **OpenAI 兼容协议** | 一个客户端可以连任何兼容服务 |
| 2 | **唯一持有 HTTP 客户端** | 关闭时一起关，避免泄漏 |
| 3 | **禁止 SDK 自重试** | 避免双倍 backoff |
| 4 | **429 自动 fallback** | 提高可用性 |
| 5 | **API key 安全** | 永不写入事件/检查点/日志 |
| 6 | **完整证据落盘** | 每个 HTTP 尝试写一条记录 |
| 7 | **可注入** | 跨多次 `decide()` 复用 |

### 关键代码

```python
class LLMClient:
    def __init__(self, provider_config, api_key=None, evidence=None):
        self.api_key = api_key or os.getenv(provider_config["api_key_env"])
        self.http_client = httpx.Client(trust_env=False, verify=True)
        self.client = OpenAI(
            base_url=provider_config["base_url"],
            api_key=self.api_key,
            http_client=self.http_client,
            max_retries=0,         # ← SDK 不重试
        )
```

**为什么 max_retries=0**：避免 SDK 与 Harness 重复重试导致的双倍 backoff。

### 429 自动 fallback

```python
def chat(self, system, user, json_mode=False, ...):
    try:
        response = self.client.chat.completions.create(**kwargs)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status == 429 and not self._closed:
            switched = self._maybe_swap_key(reason="rate_limit_error")
            if switched:
                # 用新 key 重试 1 次
                response = self.client.chat.completions.create(**kwargs)
            else:
                raise
```

**为什么要双 key fallback**：单 key 在 rate limit 时会失败。手动重试又慢。配置 `api_key_env_fallbacks: [MiniMax_API_KEY_SECONDARY]` 后，遇到 429 自动切换次级 key。

### Mock 客户端

`MockLLMClient` 不真的连 LLM，而是返回固定内容。  
**为什么需要**：CI / 离线测试不应依赖外部 API。

---

## 13. 化学工具层

> 这一章讲 5 个核心工具。每个工具都遵守"**单分子的失败绝不会让整批崩溃**"的设计原则。

### 工具层的设计原则

每个工具的返回格式：

```json
{
  "valid": true/false,
  "status": "ok" / "tool_error" / "invalid_structure" / ...,
  "error": "错误信息（如果有）",
  ...
}
```

任何异常都被捕获并以结构化字段写入候选记录，**绝不抛**。

**为什么要这样设计**：批次评估中如果一个分子让工具抛异常，整个批次就会中断。设计成"永远返回 dict"则即使一个分子失败，其他分子仍能继续评估。

### `validate_mol.py` —— 解析 SMILES + 描述符

输入：单个 SMILES。  
输出：`{valid, smiles, mw, logp, hbd, hba, rotatable_bonds, tpsa, rings, lipinski_pass, sa_score}`。

异常处理：`MolFromSmiles(smiles)` 返回 None → `valid=False, error='RDKit cannot parse SMILES'`。

### `admet_score.py` —— ADMET 启发式

> ⚠️ **重要免责**：函数顶部写明 `method="rdkit_descriptor_heuristic_v2"`、`is_trained_admet_model=False`。**不是**实测 ADMET，**不是**经过训练的分类器。

评分逻辑：

```
absorption    = 1.0 (TPSA≤140) | 0.5 (140<TPSA≤180) | 0.0 (TPSA>180)
bioavail.     = 1.0 (rot≤10 & TPSA≤140) | 0.5 (rot≤15) | 0.0 (rot>15)
herg_risk_score = min(1, basic_term × (0.75 lipo + 0.15 arom) + 0.10 size)
admet_quality_score = 0.375 absorption + 0.375 bioavailability + 0.25 qed
```

**为什么要分"评分"和"安全"**：综合 ADMET 评分 = 0.8 × 质量 + 0.2 × 安全。质量与安全是两个独立目标。

### `dock_score.py` —— AutoDock Vina 包装

完整流水线：

```
SMILES → RDKit 3D → Meeko pdbqt → Vina → kcal/mol
```

**为什么用 subprocess 而不是 Vina Python 包**：Vina Python 包在不同平台安装困难，且版本绑定复杂。subprocess 调 vina 二进制更可控。

**可审计性**：每个产物的 `metadata` 含 `command`、`vina_binary_sha256`、`vina_version`、`score_interpretation='uncalibrated docking score, not measured affinity'`。

### `diversity.py` —— 骨架与 Tanimoto

| 函数 | 作用 |
|---|---|
| `get_scaffold(smi)` | Bemis-Murcko 骨架 SMILES |
| `scaffold_diversity(list)` | 唯一骨架数 |
| `tanimoto_to_reference(smi, ref)` | 单参考相似度 |
| `adoption_stats(list, ref, threshold=0.7)` | "本轮候选是否采用了上轮 focus" 的**结构化真实信号** |

**为什么 `adoption_stats` 是"真实信号"**：它用 Tanimoto 相似度（结构事实）而非 LLM 自报，避免 LLM 幻觉。

### 评估缓存

```text
memory/evaluation_cache/<protocol_id>/<canonical_smiles>.json
memory/docking_cache/<docking_protocol_id>/<canonical_smiles>.json
```

**不可协议混用**：换 protocol 参数 → protocol_id 变 → 缓存自然失效。

---

## 14. 工具 API 速查

### `validate_smiles(smiles: str) -> dict`

```python
{
    "valid": bool,
    "smiles": str,
    "mw": float, "logp": float, "hbd": int, "hba": int,
    "rotatable_bonds": int, "tpsa": float, "rings": int,
    "lipinski_pass": bool, "sa_score": float | None,
}
```

### `estimate_admet(smiles: str) -> dict`

```python
{
    "valid": bool,
    "method": "rdkit_descriptor_heuristic_v2",
    "is_trained_admet_model": False,
    "interpretation": "Screening heuristics, not measured ADMET...",
    "absorption": float, "bioavailability": float,
    "herg_risk": bool, "herg_risk_score": float,
    "qed": float, "logp": float, "mw": float, "tpsa": float,
    "summary_score": float,
    "calibrated_herg_score": float | None,
    "calibrated_herg_features": dict | None,
    "warnings": list[str],
}
```

### `dock_smiles(smiles, receptor_pdb, pocket_center, pocket_size=(22., 22., 22.),
              vina_binary=None, exhaustiveness=8, n_poses=5, seed=2026,
              artifact_dir=None, timeout=180, cpu=2) -> dict`

```python
{
    "smiles": str,
    "score": float | None,
    "valid": bool,
    "status": "ok" | "tool_error" | "invalid_structure" | "screened_out" | "full_cache_hit",
    "error": str | None,
    "provenance": {
        "command": list[str],
        "vina_binary_sha256": str,
        "vina_version": str,
        "receptor_sha256": str,
        "ligand_pdbqt_sha256": str,
        "score_interpretation": "uncalibrated docking score, not measured affinity",
    },
}
```

### `get_scaffold(smiles) -> str | None`

返回 Bemis-Murcko 骨架 SMILES。

### `scaffold_diversity(smiles_list) -> dict`

```python
{
    "n_molecules": int,
    "n_unique_scaffolds": int,
    "scaffolds": list[str],
    "diversity_ratio": float,
}
```

### `tanimoto_to_reference(smiles, reference) -> float | None`

返回 [0, 1] 相似度。

### `adoption_stats(smiles_list, reference, threshold=0.7) -> dict`

```python
{
    "n_total": int, "n_valid_sim": int, "n_adopted": int,
    "adoption_rate": float,
    "max_similarity": float | None, "mean_similarity": float | None,
    "threshold": float, "reference": str,
}
```

### `provenance.digest(protocol: dict) -> str`

把 dict 压缩成 SHA-256 指纹。

### `provenance.file_hash(path) -> str`

计算文件的 SHA-256。

### `EvaluationCache(directory, protocol_id, enabled=True)`

`cache.get(smiles) -> dict | None`、`cache.put(evaluation_result) -> dict`、`cache.materialize(candidate, payload) -> dict`。

---

## 15. 生成器 Agent

### 它解决什么问题

**这个 Agent 是"药物化学家"**。给它一个目标，它设计出一批新分子的 SMILES。

### 它的输入和输出

**输入**：

- 靶点描述（EGFR / 1M17）；
- 上一轮裁判的 `focus`（"下一轮应该做什么"）；
- 上一轮的 `weakness`；
- `WorkingMemory.compress_for_generator()`（最近几轮摘要）；
- `FailedLigandSet.format_for_prompt()`（已知失败清单）。

**输出**：严格 JSON：

```json
{
  "smiles_list": ["CCOc1ccccc1", "NC(=O)c1ccccc1", ...],
  "rationale": "Replace morpholine on atropisomer with piperazine; expect logP -0.8."
}
```

### 为什么需要 system prompt 这么长

```
你是一名资深药物化学家，设计能与 EGFR（PDB: 1M17）ATP 结合口袋结合的类药分子。

已验证的参考抑制剂：
- Erlotinib: C#Cc1ccc2ncnc...
- Gefitinib: ...

设计约束：
- 分子量 280-500 Da
- logP 在 1.0 到 4.5
- H 键供体 ≤ 3，受体 ≤ 8
- TPSA ≤ 110
- 至少一个芳香环（对 hinge 结合至关重要）
- ...

输出必须是严格 JSON：...
```

**为什么这么详细**：LLM 虽然有化学知识，但需要明确约束才能稳定输出"项目想要的"风格。短的 prompt 会让模型飘忽不定。

### 为什么用 `response_format: {type: json_object}`

强制 LLM 输出合法 JSON。否则模型可能在 JSON 周围加些杂文本，要 `_extract_json` 容错处理。

### 异构生成

`generate_candidates(config, providers, n_per_provider, ..., use_mock, max_workers=4)`：用 `ThreadPoolExecutor` 并行调用每个 provider。

**为什么并行**：多个 provider 同时响应，节省时间。

### 重试策略

每个 provider 最多 `max_attempts_per_provider` 次。  
**重试条件**：`result['error'] is None` **且** `len(smiles_list) == n_per_provider`。

**为什么这样设计**：LLM 输出**数量**对不上是常见错误（比如要 15 个给 14 个）。重试比直接放弃更好。

---

## 16. 评估器 Agent

### 它解决什么问题

**这个 Agent 是"化验室"**。给它一批 SMILES，它给每个分子打分。

### 它不是 LLM Agent

虽然名字叫"Agent"，但它**不调 LLM**。它是工具调用 + 公式合成的纯函数集合。

**为什么不调 LLM**：评估需要确定性。LLM 输出有随机性，无法重现。

### 评估流程

```
输入：candidates = [{"smiles": "...", "provider": "...", ...}, ...]
    ↓
1. 并行跑 validate_smiles + estimate_admet
    ↓
2. 对接（funnel 模式：先 fast Vina 筛头部 → full Vina）
    ↓
3. Bemis-Murcko 骨架
    ↓
4. 公式合并：composite_score = 0.55·property + 0.30·vina + 0.15·(1-hERG)
    ↓
5. Pareto 排名
    ↓
6. evaluation_status
```

### evaluation_status 取值

| status | 含义 |
|---|---|
| `complete` | 全部评估成功（含 vina） |
| `screening_only` | dock 关闭但性质有 |
| `invalid_structure` | RDKit 无法解析 |
| `evaluation_error` | 工具异常 |
| `screened_out` | 没被选中走 full Vina |
| `tool_error` | 兜底 |

**为什么要这么多 status**：必须区分"分子本身无效"和"工具异常"。如果是工具异常，重试可能成功；如果是分子无效，重试无用。

### 摘要输出

`summarize_round(enriched)`：

```python
{
    "n_total": int, "n_valid": int, "n_complete": int,
    "valid_ratio": float,
    "n_docked": int, "n_docked_safe": int,
    "n_safety_pass": int, "safety_pass_rate": float,
    "pareto_front_size": int, "safe_pareto_front_size": int,
    "n_unique_scaffolds": int,
    "avg_admet": float | None,
    "best_vina": float | None,                  # legacy
    "best_safe_vina": float | None,             # safety-gated
    "top_candidates": [...],
}
```

**为什么有 best_vina 和 best_safe_vina 两个**：

- `best_vina`：不过滤安全性（legacy）；
- `best_safe_vina`：只在安全性通过的内取最小 Vina。

2026-09-16 confirmatory 实验发现，模型在 `best_vina` 信号下会**奖励**高 hERG 风险的分子。

---

## 17. 裁判 Agent

### 它解决什么问题

**这个 Agent 是"主任医师"**。每轮结束后，它看所有数据，决定下一轮该往哪走。

### 输出必须满足的 3 条规则

裁判的输出 `focus` **必须**包含：

1. **具体结构元素**（"morpholine"、"aniline NH"），禁止"再试试更杂的"；
2. **具体替换或添加**（"换成 piperazine"）；
3. **预期性质变化**（"logP 应下降约 1.0"）。

**为什么这样约束**：模糊的建议（如"再探索多样性"）让模型无所适从。具体建议让模型有明确方向。

### 自反思

裁判必须**自己检查上一轮的建议是否被采纳**：

| 字段 | 来源 |
|---|---|
| `focus` / `weakness` / `expected_change` / `reasoning` | LLM 生成 |
| `reflection` | LLM 生成（强制 ≤300 字符） |
| `confidence` | LLM 自报 |
| `adopted_count` | LLM 自报 |
| `adoption_deterministic` | **离线计算**（Tanimoto） |
| `adoption_llm_vs_det_drift` | **差异** = LLM - 离线 |

**为什么需要 `adoption_deterministic`**：LLM 可能"撒谎"（自报"我采纳了上一轮建议"但实际没有）。结构化真实信号（基于 Tanimoto）能戳穿幻觉。

### Fallback

任何异常 → fallback dict，含：

```python
{"focus": "", "status": "error", "weakness": "(fallback: judge unavailable)", ...}
```

**为什么不抛**：LLM 偶发失败是常态。fallback 让循环继续，不浪费之前的进度。

---

## 18. 记忆系统

### 它解决什么问题

**如果不记忆，模型每一轮都是"金鱼记忆"**——不知道上一轮试过什么、失败过什么、学到什么。

### 三层记忆架构

```
短期（WorkingMemory）          中期（RuleStore）         长期（FailedSet）
最近 N 轮摘要                  4 类规则                  黑名单 + 相似检测
策略链                        NEG/POS/CTX/EVI
best_so_far                   Wilson 下界
```

**为什么分三层**：

- 短期：每轮都用，不能太占 prompt；
- 中期：跨轮累积，分类强；
- 长期：永久记忆所有失败。

### WorkingMemory

```python
class WorkingMemory:
    recent_rounds: list[RoundSummary]   # cap = max_recent
    strategy_chain: list[str]
    best_so_far: dict | None
```

**为什么 recent_rounds 有限制**：避免 prompt 过长导致 LLM 注意力分散。

### FailedLigandSet（黑名单）

**为什么需要 LRF（FIFO）淘汰**：跨 session 累积会导致内存爆。LRF 淘汰最旧的（一般也是最不相关的）。

**为什么需要 embedding 相似检测**：精确匹配只防"完全相同的分子"。但模型可能稍作修改（如加一个 CH2）变成新分子——结构相似但同样失败。embedding 相似能防这种。

### RuleStore（4 类规则）

> 2026-09-16 confirmatory 复盘决议：单黑名单是"抑制器"不是"优化器"，必须拆为四类相互正交的记忆。

```text
NEGATIVE    "do NOT propose these SMILES"
POSITIVE    "attach fragment X at site Y improved property_score by Z"
CONTEXT     "this rule applies to scaffold class C1, C2 (NOT C3)"
EVIDENCE    "rule R observed N times, success S, CI width W"
```

**为什么 4 类**：

- NEGATIVE 抑制无效分子；
- POSITIVE 鼓励有效修改；
- CONTEXT 避免规则误用到错误的骨架；
- EVIDENCE 量化规则的可信度。

**Wilson 95% 下界更新公式**：

```
n = observations, s = successes, z = 1.96
denom = n + z²
evidence_strength = max(0, (s + z²/2)/denom - z·√((s(n-s)+z²/4)/n)/denom)
```

**为什么用 Wilson 下界**：避免"早期一次成功就过度自信"。Wilson 下界是保守估计，需要多次观察才能达到高强度。

### 记忆的协作方式

Harness 每步后调 `update_memory_from_events(events, state)`：

- `evaluate_options + outcome=supported` → POSITIVE；
- `evaluate_options + outcome=tradeoff_exceeded/inconclusive` → NEGATIVE；
- `evaluate_options + outcome=insufficient_evidence` → EVIDENCE；
- `compare_parent_child + passed=True` → 对应 POSITIVE 规则 evidence 增强。

下次 `decide()` 的 prompt 注入 `rule_memory` 字段。

---

## 19. 持久化 Harness

### 它解决什么问题

**loop.py 是"批处理"（一次跑完），Harness 是"长会话"（按步推进、可暂停、可恢复、可 HITL）**。

### 核心数据结构：TaskState

```python
@dataclass
class TaskState:
    goal: str
    task_id: str = uuid4().hex
    status: str = "paused"             # paused | running | completed | cancelled
    revision: int = 0
    candidates: dict = {}              # cid -> enriched-like
    events: list = []                  # 单调追加
    pending: dict | None = None        # 当前正在执行的工具调用
    steps_used: int = 0
    consecutive_errors: int = 0
    final: dict | None = None
    max_model_calls: int = 40
    max_evaluations: int = 100
    ...
```

### 状态机

`available_actions(state)` 计算当前阶段、合法工具：

| 阶段 | 触发 | 暴露工具 |
|---|---|---|
| `no_candidates` | 无 candidate | `generate` / `pause` |
| `candidate_evaluation` | 有未评估的 | `evaluate` / `pause` |
| `evaluation_recovery` | 有 evaluation_error | `retry_evaluation` / `pause` |
| `execute_selected_edit` | 有 selected selection | `execute_selected_edit` / `pause` |
| `parent_child_comparison` | 有 edit_executed hypothesis | `compare_parent_child` / `pause` |
| **`qualifying_candidate_found`** ⭐ | 存在合格子代 | **`finish` / `pause`** |
| `strategy_decision` | 有 assessed 但未决定的 | `choose_strategy` / `finish` / `pause` |
| `edit_budget_exhausted` | max_edits 用完 | `finish` / `pause` |
| `select_edit` | 有可行 option | `select_edit` / `finish` / `pause` |
| `propose_edits` | 起步 | `propose_edits` / `pause` |
| `terminal` | 已 completed/cancelled | （空） |

### ToolRegistry 4 步 sanitizer

这是项目最重要的工程纪律之一。  
在严格校验之前恢复常见的模型漂移：

1. `operation → tool`：若顶层缺 `tool` 但有 `operation`，复制；
2. `rationale → reason`：若顶层缺 `reason`，从 arguments.rationale 复制；
3. 删除多余顶层键（`step`/`revision`/`basis`/`status`/`next_hypothesis_id` 等）；
4. 删除多余 arguments 键（`expected_benefit`/`allowed_cost` 等 verbose metadata）。

**为什么需要 sanitizer**：模型经常在 `tool` 字段写错（如 `operation`）或缺少 `reason`。直接拒绝会让模型"猜三次然后死掉"。

### Receipt 机制（崩溃恢复）

```python
store.receipt(call_id, state)        # 执行前快照
try:
    tool.handler(state, ...)
except:
    state.consecutive_errors += 1
```

崩溃后重启：找到对应 `receipts/<call_id>.json` → 校验 → 用 recovered 覆盖主存。

**为什么需要 Receipt**：简单的"全状态快照"会导致**崩溃时已完成的工具调用重复执行**（产生副作用）。Receipt 在执行前快照，崩溃后从快照恢复，**幂等**。

---

## 20. 状态机百科

### 13 个状态详解

#### State 1: `no_candidates`

**触发**：`state.candidates == {}`  
**合法动作**：`generate`、`pause`、`import_candidates`。

#### State 2: `candidate_evaluation`

**触发**：存在未评估的 candidate。  
**合法动作**：`evaluate`、`pause`。

#### State 3: `evaluation_recovery`

**触发**：存在 `evaluation_error` 的 candidate。  
**合法动作**：`retry_evaluation`、`pause`。

#### State 4: `execute_selected_edit`

**触发**：有 `status='selected'` 的 selection。  
**合法动作**：`execute_selected_edit`、`pause`。

#### State 5: `parent_child_comparison`

**触发**：有 `edit_executed` 的 hypothesis。  
**合法动作**：`compare_parent_child`、`pause`。

#### State 6: `qualifying_candidate_found` ⭐

**触发**：存在合格子代（`current_improvement.outcome == 'supported'`）。  
**合法动作**：`finish`、`pause`。

**为什么这是关键状态**：v10 修复前，模型会"已合格还在选策略"，陷入死循环。现在这个状态**只暴露 finish**，强制收尾。

#### State 7: `strategy_decision`

**触发**：有 assessed 但未决定的 hypothesis。  
**合法动作**：`choose_strategy`、`finish`、`pause`。

#### State 8: `edit_budget_exhausted`

**触发**：`count(candidate_role=='deterministic_edit') >= max_edits`。  
**合法动作**：`finish`、`pause`。

#### State 9: `screening_no_qualifying_option`

**触发**：proposal 中所有 option 都不通过 screening。  
**合法动作**：`propose_edits`、`finish`、`pause`。

#### State 10: `select_edit`

**触发**：有可行 option。  
**合法动作**：`select_edit`、`propose_edits`、`finish`、`pause`。

#### State 11: `option_screening`

**触发**：proposal 存在但 screening 未完成。  
**合法动作**：`evaluate_options`、`pause`。

#### State 12: `propose_edits`

**触发**：没有 consumed proposal。  
**合法动作**：`propose_edits`、`pause`。

#### State 13: `terminal`

**触发**：`status in {completed, cancelled}`。  
**合法动作**：无。

---

# 第 4 部分：辅助系统

---

## 21. 数据库

### 两种数据库

| 数据库 | 用途 | 部署 |
|---|---|---|
| **SQLite** | Harness 任务状态 | 本地文件 |
| **PostgreSQL + RDKit + pgvector** | 长期、可搜索 | 远程数据库 |

### SQLite 仓储

```sql
CREATE TABLE IF NOT EXISTS runs (
    task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    state_json TEXT NOT NULL, state_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (...);
CREATE TABLE IF NOT EXISTS rounds (...);
CREATE TABLE IF NOT EXISTS evaluations (...);
CREATE TABLE IF NOT EXISTS evaluation_attempts (...);
CREATE TABLE IF NOT EXISTS agent_events (...);
CREATE TABLE IF NOT EXISTS artifacts (...);
CREATE TABLE IF NOT EXISTS human_approvals (...);
```

**为什么 SHA-256 校验**：保证 task.json 与 SQLite 一致。任何一个被改都会被发现。

### PostgreSQL Schema

8 张事实表 + 不可变触发器 + HNSW 索引：

- `campaigns` —— 战役表
- `runs` —— 运行表
- `molecules` —— 分子本体
- `proposals` —— 提议表
- `evaluations` —— 评估表
- `filter_events` —— 筛选事件表
- `strategy_memories` —— 策略记忆表
- `artifacts` —— 工件表

**不可变性**：

```sql
CREATE OR REPLACE FUNCTION aidd.reject_fact_mutation() RETURNS trigger ...
    RAISE EXCEPTION 'append-only table: UPDATE is not allowed' USING ERRCODE = '55000';
```

**为什么这样设计**：合规和科学诚信。如果允许改写历史数据，研究者可能"事后调参"让结果看起来更好。

### 计算列

molecules 的 `mol`、`mfp2`、`inchikey`、`formula`、`molecular_weight`、`logp`、`tpsa`、`hbd`、`hba`、`rotatable_bonds`、`rings` 都是 GENERATED 列。调用方只需写 `canonical_smiles`。

**为什么要 GENERATED 列**：化学计算统一在数据库中执行，避免 Python 侧和数据库侧结果不一致。

---

## 22. 数据库 ERD 图与查询示例

### 实体关系图

```
campaigns (1) ─── (N) runs (1) ─── (N) proposals (N) ─── (1) molecules
                                  └── (N) evaluations
                                  └── (N) artifacts
                                  └── (N) strategy_memories
```

### 常用查询

#### 找一个分子的所有评估历史

```sql
SELECT e.tool_name, e.status, e.value, e.metrics, e.created_at
FROM aidd.evaluations e
JOIN aidd.proposals p ON e.proposal_id = p.id
WHERE p.molecule_id = (
  SELECT id FROM aidd.molecules WHERE canonical_smiles = 'CCOc1ccccc1'
)
ORDER BY e.created_at;
```

#### Tanimoto 相似度 top-10

```sql
SELECT m2.canonical_smiles,
       aidd.mfp2_tanimoto(m1.mfp2, m2.mfp2) AS similarity
FROM aidd.molecules m1
CROSS JOIN aidd.molecules m2
WHERE m1.canonical_smiles = 'CCOc1ccccc1' AND m1.id != m2.id
ORDER BY similarity DESC LIMIT 10;
```

#### 哪些分子通过了 safety gate？

```sql
SELECT DISTINCT m.canonical_smiles, e.value, e.metrics
FROM aidd.molecules m
JOIN aidd.proposals p ON p.molecule_id = m.id
JOIN aidd.evaluations e ON e.proposal_id = p.id
WHERE e.tool_name = 'composite'
  AND (e.metrics->>'safety_gate_pass')::boolean = true;
```

---

## 23. HITL 与控制流

### HITL 检查点

`HITLCheckpoint` 提供 `pre_loop`、`pre_round`、`pre_finish` 三个钩子，询问用户是否继续。

CLI 模式：`--hitl` 标志启用。  
API 模式：`POST /runs/{id}/approvals`。

### LoopController 终止条件

```python
def should_stop(self, state):
    if state.hitl_veto: return True, "human_veto"
    if state.round >= max_rounds: return True, "max_rounds_reached"
    if state.tokens_used >= token_budget: return True, "token_budget_exceeded"
    if rounds_without_improvement(state) >= patience: return True, "no_improvement"
    return False, ""
```

**为什么需要多重终止条件**：单靠 max_rounds 太死板。加 token budget 防止 API 成本失控；加 patience 让没有进展的循环自动停止。

### 终止原因字典

```python
REASONS = {
    "max_rounds_reached": "达到最大轮数",
    "token_budget_exceeded": "token 用完",
    "no_improvement": "无进展",
    "human_veto": "人否决",
    "consecutive_errors": "连续 3 个工具错误",
    "decision_loop": "已合格还在选策略",
    "execution_failure": "执行失败",
    "budget_exhausted": "预算用完",
    "user_paused": "用户暂停",
    "user_cancelled": "用户取消",
}
```

---

## 24. 脚本与基准测试

### 顶层脚本

| 脚本 | 职责 |
|---|---|
| `prepare_receptor.py` | 处理受体蛋白 |
| `extract_ligand_center.py` | 提取结合位点 |
| `check_minimax_connectivity.py` | API 连通性闸门 |
| `compare_2d_policies.py` | 2D 目录对比 |
| `scout_parents.py` | 离线母体侦察 |
| `run_2d_with_docking.py` | dock-aware 多目标 |
| `migrate_runs_to_db.py` | 迁移到 Postgres |
| `selfcheck_db.py` | 数据库自检 |

### 关键发现（截至 2026-09-23）

1. 网络层从 v4 起彻底干净；
2. 决策死循环缺陷被稳定修掉；
3. 甲苯在修复版上 5/5 goal_met；
4. 苯酚/苯胺 2/4 goal_met（不构成稳定结论）；
5. 强 baseline 在大目录能赢 agent；
6. 规则臂 0/12 仍只是弱对照；
7. schema sanitizer 把 5 个 schema 错误全吸收；
8. catalogue_summary 把 schema 错误率从 6 降到 1；
9. 测试套件 301 passed, 1 skipped。

---

# 第 5 部分：设计与决策

> 这一部分解释**每一个核心架构决策背后的"为什么"**。
> 每个决策都有：背景、决策、理由、代价、触发事件。

---

## 25. 12 个核心架构决策（每个都是完整的故事）

| # | 决策 | 一句话总结 |
|---|---|---|
| 26 | 用 LLM 而不是传统机器学习 | LLM 已读过大量化学文献，能自主设计 |
| 27 | 禁用 SDK 自重试 | 避免双倍 backoff |
| 28 | 4 步 schema sanitizer | 模型小错误不该让它"猜三次死掉" |
| 29 | 4 类记忆而不是单一黑名单 | 黑名单抑制，黑名单不能优化 |
| 30 | 进度信号区分 safety | 避免奖励高 hERG 的分子 |
| 31 | API key 双 fallback | 429 不再让任务卡住 |
| 32 | Receipt 而非全状态快照 | 幂等恢复，避免重执行 |
| 33 | 数据库只追加 | 事实不可篡改 |
| 34 | Mock 和真实客户端共享接口 | 测试无缝切换 |
| 35 | Catalogue summary 修复 SMILES | 减少无效 SMILES |
| 36 | LRF 淘汰失败集合 | 内存可控 |
| 37 | 状态机分阶段 | 避免越界 |

---

## 26. 为什么用 LLM 而不是传统机器学习

### 背景

传统 CADD 用 QSAR、分子指纹相似性搜索等方法生成候选分子。这些方法依赖专家设计的特征。

### 决策

使用 LLM 作为生成器，让模型利用在训练中学到的化学知识自主设计分子。

### 理由

- LLM 已读过大量化学文献，知道什么样的分子可能是好的；
- 不需要为每个新靶点重新训练；
- 可以用 prompt engineering 控制输出风格。

### 代价

- 输出不保证 RDKit 可解析；
- 可能输出训练数据中的已知分子（"记忆"问题）；
- 单次推理成本高。

### 触发

早期尝试基于规则的生成器（SMILES 重排、片段组合）效果差；LLM 在 prompt 明确约束下能稳定输出 SMILES。

---

## 27. 为什么禁用 SDK 自重试

### 背景

OpenAI SDK 默认会自动重试网络错误和 429。但 SDK 的重试策略与 Harness 重试策略可能冲突，导致**双倍 backoff**。

### 决策

在 `LLMClient.__init__` 中设 `max_retries=0`，所有重试由 Harness 控制。

### 理由

- 集中控制重试行为（指数 backoff + Retry-After 头解析）；
- 避免双倍 backoff；
- 重试期间能落 `model_request` 证据。

### 触发

v3 观察到 6 重试 + 3 失败，但实际只有 3 次有效请求。

---

## 28. 为什么需要 4 步 schema sanitizer

### 背景

早期模型经常在 `tool` 字段写错（如 `operation`）或缺少 `reason`，导致动作被直接拒绝。

### 决策

在严格校验之前先做 4 步恢复（operation→tool、rationale→reason、删多余键、删多余 args）。

### 理由

- 模型的"小错误"是常见且可预测的；
- 直接拒绝会让模型"猜三次然后死掉"，触发 `consecutive_errors`；
- 自动恢复更友好。

### 代价

- sanitizer 自己也是代码，必须有充分测试；
- 可能掩盖真正的 schema 错误。

### 触发

benzonitrile P1 r1 出现 5 个 schema 错误导致 `execution_failure`，但实际并不该失败。

---

## 29. 为什么需要 4 类记忆而不是单一黑名单

### 背景

早期只用 `FailedLigandSet`（黑名单）。但 confirmatory 实验显示黑名单只能"抑制"不能"优化"——它告诉模型不要做哪些，但不说应该做哪些。

### 决策

拆为 NEGATIVE / POSITIVE / CONTEXT / EVIDENCE 四类。

### 理由

- 不同类型的规则有不同的应用场景；
- CONTEXT 避免把"在骨架 A 上成功"误用到骨架 B；
- EVIDENCE 用 Wilson 下界做保守估计，避免早期一次成功就过度自信。

### 触发

2026-09-16 confirmatory 复盘决议。

---

## 30. 为什么进度信号要区分 safety 与否

### 背景

原 `progress_signal: vina` 看的是所有 docking 候选中最佳的 Vina。但高 Vina 可能伴随高 hERG 风险——这与项目目标矛盾。

### 决策

默认 `progress_signal: safe_vina`，只看 safety-gate-passing 候选的最佳 Vina。

### 理由

- 避免"奖励"危险分子；
- 让 patience counter 反映"真实进展"。

### 触发

2026-09-16 confirmatory 复盘发现 all-candidate 最优是 herg_risk=0.830 的分子。

### 注意

配置信号**不**写入 `protocol_id`（保持缓存兼容）。

---

## 31. 为什么需要 API key 双 fallback

### 背景

单 API key 在 rate limit 时会失败。手动重试又慢。

### 决策

在 `provider_config.api_key_env_fallbacks` 中列次级 key。遇到 429 自动切换。

### 理由

- 提高可用性；
- 不污染正常请求（只在 429 时切换）；
- 切换事件有 `type=key_swap` 证据，可审计。

### 触发

2026-09-21 token plan 耗尽时。

---

## 32. 为什么用 Receipt 而不是简单的全状态快照

### 背景

长任务可能崩溃。简单的"全状态快照"有两个问题：

1. **崩溃时已完成的工具调用会重复执行**——产生副作用；
2. **大状态序列化慢**。

### 决策

每个工具调用执行前写 receipt（含 call_id）。崩溃后从最近 receipt 恢复。

### 理由

- 幂等（不会重复执行）；
- 快速（只写一份）；
- 可审计（每个 call 都有对应 receipt）。

### 触发

持久化模式下需要"暂停—重启"工作流。

---

## 33. 为什么数据库要"只追加"

### 背景

传统数据库支持 UPDATE/DELETE，但事实不应被改写。

### 决策

PostgreSQL 装触发器拒绝任何 UPDATE（错误码 55000）；SQLite 用 UPSERT 但带 SHA-256 校验。

### 理由

- 事实不可篡改（合规性）；
- 自然支持审计（每次"修改"都是新事实）；
- 与 receipt 机制一致。

---

## 34. 为什么 Mock 和真实 LLM 客户端共享接口

### 背景

CI / 离线测试不应依赖外部 API。

### 决策

`MockLLMClient` 与 `LLMClient` 实现相同的接口（`chat`/`chat_json`/`close`/`model`/`last_usage`）。

### 理由

- 测试不依赖网络；
- 关键：mock 必须满足真实客户端的 lifecycle 契约（`close()` 幂等、`__enter__`/`__exit__`），否则调用方代码要分两套。

---

## 35. 为什么 Catalogue summary 能修复 SMILES 错误

### 背景

4-fluorophenol agent 出现 6 schema + 6 state_machine 错误——模型把片段拼成了不合法的芳香环。

### 决策

在 `LlmPolicy.decide()` 的 prompt 加 `PARENT = X / VALID FRAGMENTS / VALID SITES` 段。

### 理由

- 强制模型从合法集合中选；
- 大幅降低无效 SMILES 产生率（schema errors -83%）；
- 不改变算法，只是约束 prompt。

---

## 36. 为什么用 LRF 淘汰失败集合

### 背景

早期 `FailedLigandSet` 没有上限，跨 session 累积导致内存爆。

### 决策

超 `max_size`（默认 500）时按插入顺序淘汰最旧。

### 理由

- 内存可控；
- 老的失败不太相关（模型已学到）；
- `max_size=0` 可恢复无界行为（向后兼容）。

---

## 37. 为什么状态机要分阶段而不是一锅端

### 背景

早期把"什么时候能 evaluate、什么时候能 finish、什么时候能 choose_strategy"混在 prompt 里，模型容易越界。

### 决策

用 `available_actions(state)` 计算当前阶段、合法工具、合法引用，并只把这些暴露给模型。

### 理由

- 模型只能选"现在能做的事"；
- 错误信息明确（"请先 evaluate 再 choose_strategy"）；
- 单元测试可以单独测试每个状态。

---

# 第 6 部分：项目历史

---

## 38. 项目演进史

### 5 阶段路线图

| Phase | 内容 |
|---|---|
| 0 | 环境：rdkit / meeko / Vina / MiniMax key / 1M17 |
| 1 | 4 个独立工具 |
| 2 | 2-Agent MVP（generator + evaluator） |
| 3 | Judge + 异构生成 |
| 4 | 实验图表 + 失败案例 |
| 5 | 收尾 + 架构文档 |
| 4.1 | HITL + WorkingMemory + FailedLigandSet |
| 4.2 | Judge 自反思 |
| 4.3 | 跨会话持久化 + LRF 容量 |
| 4.4 | 12 轮 + safety_gated progress |
| 4.5 | 持久化 Harness + 双 key + schema sanitizer |

### 关键时间线

- **v2-v4**：各种失败（传输、schema）；
- **v10**：**首次 goal_met**——智能体自行找到合格分子；
- **2026-09-20**：修复"苯胺决策死循环"；
- **2026-09-21**：多母体扩展（r2/r3/r4）；
- **2026-09-22**：双 key fallback + schema sanitizer；
- **2026-09-23**：dock-aware 多目标对比 + catalogue_summary；
- **当前**：测试套件 **301 passed, 1 skipped**。

---

## 39. 常见误解与"不要做的事"

### 常见误解

| ❌ 误解 | ✅ 真相 |
|---|---|
| "AI 自动设计了能治癌症的药" | 只做计算评估，不做合成、实验 |
| "Vina 分数就是药物的亲和力" | Vina 是估算，不是实测 |
| "ADMET 分数就是药物真的安全性" | 是描述符启发式，不是经过训练 |
| "hERG 风险就是心脏毒性实测值" | 是 7 特征 logistic 模型 |
| "Judge 的 adopted_count 是事实" | 必须对照 adoption_deterministic |
| "Mock LLM 和真实一样" | Mock 是循环常量，**仅**用于 CI / 离线测试 |

### 不要做的事

| 不要做 | 理由 |
|---|---|
| 把 `max_retries` 设给 SDK | 与 Harness 重复重试 |
| 在 `evaluator` 内调 LLM | 失去 evaluation_cache 的可重放性 |
| 改 `protocol_id` 的 hash 函数 | 现有 cache 与 manifest 全部失效 |
| 改 `_safe_call` 把异常当无效 | 把工具异常与分子无效混淆 |

---

## 40. 性能与成本估算

### 单次运行耗时（mock 模式）

| 阶段 | 耗时 |
|---|---|
| 加载 config + 计算 protocol_id | 0.1s |
| generator（mock） | 0.05s |
| evaluator（5 个 SMILES + parallel dock） | 2-3s |
| judge（mock） | 0.05s |
| **每轮总计** | **约 3 秒** |
| **5 轮总计** | **约 15 秒** |

### 单次运行耗时（真实模式）

| 阶段 | 耗时 |
|---|---|
| generator LLM 调用 | 1-3s |
| evaluator（Vina 瓶颈） | 5-15s |
| judge LLM 调用 | 1-3s |
| **每轮总计** | **约 10-20s** |
| **5 轮总计** | **约 1-2 分钟** |
| **12 轮总计** | **约 3-5 分钟** |

### Token 消耗估算

每轮约 6000 tokens（2000 prompt + 500 completion）。  
5 轮约 30000 tokens，按 MiniMax-M3 价格约 ¥0.03。  
12 轮约 72000 tokens，约 ¥0.07。

### 存储需求

| 文件 | 大小 |
|---|---|
| `task.json` | 10-100 KB |
| `receipts/*.json` | 5-50 KB |
| `runs/<run>/` | 1-10 MB |
| Postgres（每 1000 run） | 100 MB-1 GB |

### 内存占用

| 阶段 | 内存 |
|---|---|
| 启动 | 50 MB |
| 5 轮 running | 200-300 MB |
| 持久化模式长时间 | 300-500 MB |

### 优化建议

| 优化方向 | 收益 | 风险 |
|---|---|---|
| 启用 evaluation_cache | 同 SMILES 第二次起几乎免费 | 改 scoring 必破 cache |
| 启用 docking_cache | 同 SMILES docking 不再算 | 改 protocol 必破 cache |
| 启用 FailedLigandSet | 避免已知失败分子 | LRF 淘汰丢失老记忆 |
| 提高 Vina workers | 多分子并行对接 | OOM / 系统卡顿 |
| 提高 Vina exhaustiveness | 分数更精确 | 慢 2-4 倍 |
| 启用 funnel | 用 cheap Vina 筛 | funnel 阈值需调 |

---

# 附录

---

## 附录 A：完整目录树

```text
AIDD agent
├── README.md
├── PLAN.md
├── LICENSE
├── requirements.txt
├── config.yaml                          ← 主配置
├── env_before_torch_user.json
├── env_before_torch.txt
├── agent_dashboard.py
├── agent_task.py                        ← 持久化任务 CLI
├── api.py                               ← FastAPI
├── loop.py                              ← 基准模式入口
├── agents/
│   ├── __init__.py
│   ├── agent_metrics.py
│   ├── evaluator.py
│   ├── failed_set.py
│   ├── generator.py
│   ├── hitl.py
│   ├── judge.py
│   ├── llm.py
│   ├── loop_controller.py
│   ├── redaction.py
│   ├── rule_memory.py
│   ├── working_memory.py
│   └── harness/
│       ├── __init__.py
│       ├── attribution.py
│       ├── dashboard.html
│       ├── dashboard.py
│       ├── editor.py
│       ├── evidence.py
│       ├── molecule_ops.py
│       ├── planning.py
│       ├── presentation.py
│       ├── reliability.py
│       ├── runtime.py
│       ├── schema.py
│       ├── screening.py
│       ├── state.py
│       └── tools.py
├── benchmarks/
├── data/
│   ├── 1M17.pdbqt
│   ├── reference_compounds.json
│   └── docking/
│       ├── prepared/
│       └── raw/
├── db/
│   ├── __init__.py
│   ├── access.py
│   ├── patch_constraints.sql
│   ├── repository.py
│   ├── schema.sql
│   └── verification_output.txt
├── docs/
│   ├── AGENT_HARNESS.md
│   ├── ...（其他文档）
│   └── PROJECT_HANDBOOK.md              ← 本文档
├── experiments/
├── memory/
├── notebooks/
├── runs/
├── scripts/
├── tests/
└── tools/
    ├── __init__.py
    ├── admet_score.py
    ├── calibrated_herg.py
    ├── diversity.py
    ├── dock_score.py
    ├── docking_cache.py
    ├── evaluation_cache.py
    ├── fpscores.pkl.gz
    ├── provenance.py
    ├── references.py
    ├── sascorer.py
    ├── validate_mol.py
    └── vina.exe
```

---

## 附录 B：术语速查表

### AI / 软件工程术语

| 术语 | 含义 |
|---|---|
| **LLM** | Large Language Model，大语言模型 |
| **Agent** | 智能体，能"观察 → 思考 → 行动"循环的 AI |
| **Multi-Agent** | 多智能体 |
| **Tool Registry** | 工具注册表 |
| **HITL** | Human-in-the-loop |
| **Checkpoint** | 检查点 |
| **Cache** | 缓存 |
| **Hash** | 哈希/指纹 |
| **Protocol ID** | 协议指纹 |
| **Append-only** | 只追加 |
| **Sanitizer** | 清洗器 |
| **Mock** | 模拟 |
| **Tanimoto** | 分子相似度 |
| **Wilson Lower Bound** | Wilson 95% 下界 |
| **LRF / FIFO** | 最旧先删 |

### 药物 / 化学术语

| 术语 | 含义 |
|---|---|
| **AIDD** | AI-Driven Drug Design |
| **EGFR** | 表皮生长因子受体 |
| **PDB ID** | 蛋白质数据库的结构 ID |
| **SMILES** | 分子的字符串身份证 |
| **Docking** | 分子对接 |
| **kcal/mol** | 结合自由能单位 |
| **ADMET** | 吸收/分布/代谢/排泄/毒性 |
| **logP** | 油水分配系数 |
| **hERG** | 心脏离子通道 |
| **SA Score** | 合成难度评分 |
| **MW** | 分子量 |
| **HBD / HBA** | 氢键供体/受体 |
| **TPSA** | 拓扑极性表面积 |
| **QED** | 类药性定量估计 |
| **Lipinski** | 五规则 |
| **Scaffold** | 分子骨架 |
| **Pareto Front** | 多目标最优集 |
| **ATP Binding Pocket** | ATP 结合口袋 |

### 项目内术语

| 术语 | 含义 |
|---|---|
| **protocol_id** | `digest(evaluation_protocol(...))` |
| **docking_protocol_id** | `digest(docking_protocol(...))` |
| **Receipt** | `runs/<task>/receipts/<call_id>.json` |
| **WorkingMemory** | 短期记忆 |
| **RuleStore** | 4 类规则记忆 |
| **FailedLigandSet** | 黑名单 |
| **Catalogue** | 冻结的 2D 派生目录 |
| **Catalogue summary** | `PARENT / VALID FRAGMENTS / VALID SITES` 段 |
| **Schema sanitizer** | 4 步恢复 |
| **Guardrail** | 重复动作护栏 |
| **Supported / Tradeoff exceeded / Insufficient evidence** | screening 3 种判定 |
| **Property score** | 0.55·ADMET + 0.15·Lipinski + 0.30·SA |
| **Composite score** | 0.55·property + 0.30·vina + 0.15·(1-hERG) |
| **Safety gate** | `hERG ≤ 0.55 ∧ logP ≤ 4.5` |
| **Funnel** | 两段式对接 |
| **Goal met** | 找到合格分子 |
| **Decision_loop** | 已合格还在选策略 |

---

## 附录 C：实验结论摘要

### v10（2026-09-20）：首次完整成功

| 指标 | rule 臂 | **agent 臂** |
|---|---|---|
| 终止结果 | `budget_exhausted` | **`goal_met`** |
| 合格产物数 | 0 | **2** |
| 最佳合规增量 | 0.000195 | **0.014776** |
| 实际编辑 | 0 | **2** |
| 父子比较 | 0 | **2** |
| 策略切换 | 0 | **1** |
| 网络重试 / 失败 | 0 / 0 | **0 / 0** |

### 多母体稳定性

| 母体 | n | goal_met 占比 | best Δ 范围 |
|---|---|---|---|
| 甲苯 | 5 | **5/5 (100%)** | 0.01092 - 0.01874 |
| 苯酚 | 4 | 2/4 (50%) | 0.01478 - 0.01744 |
| 苯胺 | 4 | 2/4 (50%) | 0.01469 - 0.01698 |
| catechol | 3 | 3/3 (100%) | 0.01911 - 0.02293 |
| resorcinol | 4 | 3/4 (75%) | 0.00030 - 0.02962 |
| 4-methylphenol | 3 | 3/3 (100%) | 0.01065 - 0.01738 |
| 4-fluorophenol | 3 | 3/3 (100%) | 0.01102 - 0.01989 |
| benzonitrile | 2 | 0/2 (0%) | 0.00774 |

### 测试套件

```
301 passed, 1 skipped
```

---

## 文档结束

本手册完成于 **2026-09-23**，对应 `aidd-multi-agent` 项目 origin/main 末段。

**最终统计**：

- 总章节数：**6 部分 + 3 附录**
- 子章节数：**120+**
- 文档大小：**约 190 KB**
- 适用读者：**任何人——不需要任何前置知识**

**阅读方式**：从头读到尾。每一章都从"这是什么"开始讲，到"为什么这样设计"结束。  
任何时候你累了，可以停；回来从上次的地方继续读。

— GitHub Copilot · MiniMax M3 · 2026-09-23

---

# 41. Phase 4.4 — 校准、可视化、立体化学（2026-09-27）

> 本章是**追加章**，记录 2026-09-27 完成的四个优先级闭环。每个子节都对应 README 顶部「2026-09-27」一节的内容，但这里展开实现细节、动机、设计取舍、API 合约、回归测试。

## 41.1 校准系统（calibration）：把"agent 是否自信过头"变成可测量

### 动机

前 10 个修复中有 8 个是**错误信息缺陷**（"不允许"而不说"应该是什么"）。但有一类失败**完全发生在输出正确之后**：

- 模型给 `min_change=0.020` 的预测；
- 实际 screening 给出 `observed_delta=0.005`；
- 评分脚本判定 `outcome="insufficient_evidence"`（差距小于 `min_effect` 阈值）；
- agent **继续**生成更多这种"自信过头"的产物。

之前这条链路被当成普通 negative 规则丢掉——但**这不是失败**，这是**校准误差**（calibration error）。系统性地高估效应量的 agent 跟失败 agent 是两码事：前者可能找到好分子，只是不会"宣告"自己找到了。

### 设计：第 4 类规则 — EVIDENCE

把 memory 从 3 类扩到 4 类：

| 类别 | 含义 | 触发条件 |
|---|---|---|
| `negative_constraint` | "禁止提议这个 SMILES" | screening `tradeoff_exceeded` / `inconclusive` |
| `positive_transformation` | "此 edit 在此 scaffold 上有效应" | screening `supported` |
| `applicable_context` | "此规则适用于 scaffold class C" | 上下文标签 |
| **`evidence_strength`** | **"模型预测 X，实测 Y"** | **screening `insufficient_evidence`** |

`agents/rule_memory.py` 的 `add_prediction_error()` 是新增的入口：

```python
def add_prediction_error(self, *, parent_smiles: str, child_smiles: str,
                            predicted_delta: float, observed_delta: float,
                            context_scaffolds=None) -> str:
    rid = f"pred::{parent_smiles}::{child_smiles}::{direction}"
    rule = Rule(
        rule_id=rid,
        category=EVIDENCE,
        description=f"prediction error: predicted {predicted_delta:+.4f} "
                    f"observed {observed_delta:+.4f} (calibration "
                    f"error {calibration_error:.4f}, direction={direction})",
        pattern={
            "parent_smiles": parent_smiles, "child_smiles": child_smiles,
            "direction": direction,
            "predicted_delta": float(predicted_delta),
            "observed_delta": float(observed_delta),
            # Phase 4.4 关键设计：保留所有观测的历史，
            # 跨 run 累积的 (predicted, observed, utc) 元组列表。
            "observations_log": [(float(predicted_delta),
                                   float(observed_delta),
                                   now_iso)],
        },
        ...
    )
```

注意两个细节：

1. **`rule_id` 含 `direction`**——同一个 parent/child 对，如果模型先高估再低估，会产生**两条独立规则**（`pred::X::Y::under` 与 `pred::X::Y::over`），分别累积各自方向的证据。
2. **`observations_log` 保留全历史**——不只存最新一次。这样 `compute_calibration_metrics` 能算整段时间的 `mean_abs_error`，而不是只看最后一次。

### Runtime 集成

`agents/harness/runtime.py::update_memory_from_events` 现在为 `evaluate_options` 工具结果中每一条 `outcome=="insufficient_evidence"` 的行做三件事：

1. 从 `action.arguments.options[].predictions[]` 找出 `metric=="property_score"` 的 `min_change`（predicted）；
2. 从 `result.screening_comparison.rows[].effect.observed_delta` 取出实测值；
3. 调 `rule_store.add_prediction_error(parent, child, predicted, observed)`。

```python
# 关键代码（agents/harness/runtime.py:180）
elif outcome == "insufficient_evidence":
    if not smi or delta is None:
        continue
    predicted = opts_predictions.get(opt_index)
    if predicted is None:
        continue
    try:
        self.rule_store.add_prediction_error(
            parent_smiles=parent_smiles, child_smiles=smi,
            predicted_delta=predicted, observed_delta=float(delta),
            context_scaffolds=[scaffold],
        )
    except Exception as exc:
        # Phase 4.4: 永远不再静默。校准链断掉必须能被审计发现。
        try:
            from agents.redaction import sanitize_exception
            msg = sanitize_exception(exc, self._secrets)
        except Exception:
            msg = "<unreadable>"
        print(f"[WARN] prediction_error storage failed for "
              f"{parent_smiles}->{smi}: {msg}", flush=True)
        sink = getattr(self, "evidence", None)
        if callable(sink):
            try:
                sink({
                    "type": "rule_store_error",
                    "rule_category": "evidence_strength",
                    "operation": "add_prediction_error",
                    "parent_smiles": parent_smiles,
                    "child_smiles": smi,
                    "exception": msg,
                })
            except Exception:
                pass
```

**之前**：`except Exception: pass`。**为什么改**：校准链是新增的、没被真实 run 验证过的代码路径；任何意外（KeyError、JSON 序列化错误、磁盘满）都会让整个校准链在无声中断，且**没有任何指标**告诉你它断了。stderr + 审计事件双通道是当前最便宜、破坏面最小的可见性。

### Metrics 聚合

`agents/agent_metrics.py::compute_calibration_metrics(rule_store)` 把 `RuleStore.by_category(EVIDENCE)` 转成 JSON-safe 字典：

```python
{
  "n_evidence_rules": 5,
  "n_observations": 12,        # 含 observations_log 展开
  "mean_abs_error": 0.0175,
  "median_abs_error": 0.0175,
  "max_abs_error": 0.02,
  "under_claim_rate": 0.6,      # 60% 的预测高估了效应
  "over_claim_rate": 0.4,
  "by_direction": {"under": 3, "over": 2},
  "worst_pair": {                # 误差最大的 pair
    "parent_smiles": "Oc1ccccc1",
    "child_smiles": "CCOc1ccccc1",
    "predicted_delta": 0.04,
    "observed_delta": 0.06,
    "abs_error": 0.02,
    "direction": "over",
    "observations": 1,
  }
}
```

`compute_agent_metrics()` 新增 `rule_store` 参数，把它输出到 `metrics.json["calibration"]`，并 mirror 到 `summary.json["agent_metrics"]["calibration"]`。

### 设计取舍

| 决策 | 选项 A | 选项 B | 选 A 的理由 |
|---|---|---|---|
| `n_unspecified` 算不算 error | 算 | 不算 | "模型预测 X，没给 Y" 也是 calibration 误差的一种 |
| EVIDENCE 类别是否参与 `format_for_prompt` | 是 | 否 | 是（已经在第 29 章描述）；模型下一轮可以参考"上次预测偏了多少" |
| 累积阈值 | Wilson lower bound | 简单计数 | 一致：4 类规则统一使用 Wilson（详见第 29 章） |
| `direction` 是否进 `rule_id` | 是 | 否 | 是：让 under/over 各自累积，避免相互抵消 |
| `observations_log` 是否存盘 | 是 | 否 | 是：跨 run 累积是项目核心价值；存盘让 history 可重现 |
| 写盘失败处理 | 静默 | 审计 + stderr | 审计 + stderr（见上方代码块） |

### 回归测试

- `tests/test_runtime_calibration.py` — 7 个测试：覆盖 insufficient_evidence 流转、方向判定、累积、storage 失败审计、不支持的 metric 跳过、其他 tool 不污染 EVIDENCE；
- `tests/test_calibration_metrics.py` — 8 个测试：空 store、单一 under/over、累积、持久化 round-trip、balance、JSON-safe；
- `tests/test_prediction_error.py` — 17 个测试：`RuleStore.add_prediction_error` 单元行为。

## 41.2 内存可视化（P3-4）：让"agent 学到什么"对人类可见

### 动机

`memory/rule_memory_<task_id>.json` 是机器友好的，但对人完全不可读。当你想回答"agent 这轮到底是哪类规则生效了？"时，唯一办法是 `jq` 或肉眼扫 200 行 JSON。

### 设计

`scripts/visualize_memory.py` 是一个**只读**CLI：

```bash
# 默认：扫描 ./runs 和 ./memory 下所有 rule_memory*.json
python scripts/visualize_memory.py

# 指定文件
python scripts/visualize_memory.py --memory path/to/rules.json

# 只输出 JSON，不画图（CI / cron 友好）
python scripts/visualize_memory.py --json-only
```

输出（`docs/figures/`）：

| 文件 | 内容 |
|---|---|
| `memory_categories_pie.png` | 4 类规则占比饼图（红/绿/紫/蓝固定配色） |
| `calibration_scatter.png` | predicted vs observed 散点 + `y=x` 参考线 |
| `calibration_drift.png` | 误差直方图 + over/under 平衡条 |
| `top_rules_evidence.png` | 按 `evidence_strength` 排序的 top-15 规则（水平条） |
| `memory_visualization.json` | 机读汇总（top-20 规则 + calibration 指标） |

### 依赖降级

```python
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except Exception:
    HAS_MPL = False
```

缺失 matplotlib 时**自动只输出 JSON**，并 stderr 提示安装命令。脚本不会因为环境差异崩掉。

### Auto-discovery 路径

```python
DEFAULT_SEARCH_ROOTS = (Path("runs"), Path("memory"))

def discover_rule_memory_paths(roots):
    out = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("rule_memory*.json"):
            if path.is_file():
                out.append(path)
    return sorted(out)
```

三种路径都覆盖：

- `runs/samples/rule_memory_<task_id>.json` — harness 路径
- `runs/<run_dir>/_memory/rule_memory.json` — 旧 loop.py 路径
- `memory/v2/<target>/<protocol>/<ns>/rule_memory.json` — Phase 4.1 协议级隔离

### 回归测试

`tests/test_visualize_memory.py` — 9 个测试：纯函数（`category_counts` / `evidence_pairs` / `load_rules` / `write_json_report`）、CLI 端到端、`--json-only` 降级、空输入退出码 1。

## 41.3 立体化学（P3-3）：从 2D 升级到 3D 感知

### 动机

ISSUES.md 的 P3-3（仍是 OPEN）写道：

> 当前所有 SMILES 都被当作 2D 处理；立体化学被忽略。真实药物是手性的。

实际影响：

- `(S)-alanine` 写 `N[C@@H](C)C(=O)O`；
- 现有 `validate_smiles()` 返回 `smiles="C[C@H](N)C(=O)O"`（canonical 含 stereo）；
- 但**没有字段**告诉调用者"这个分子有 1 个手性中心，已指定"；
- 于是下游（生成器、adopt 决策、judge 反思）无法区分 "achiral phenol" 与 "specified (S)-enantiomer of phenol"——后者在没有指定 chirality 时会变成外消旋混合物，ADMET 性质可能差几倍。

### 设计

`tools/validate_mol.py` 新增 `_stereochemistry_block(mol)`：

```python
def _stereochemistry_block(mol):
    n_total = int(rdMolDescriptors.CalcNumAtomStereoCenters(mol))
    n_unspecified = int(rdMolDescriptors.CalcNumUnspecifiedAtomStereoCenters(mol))
    n_specified = max(0, n_total - n_unspecified)
    has_directional = any(
        bond.GetBondType() == Chem.BondType.DOUBLE
        and bond.GetStereo() != Chem.BondStereo.STEREONONE
        for bond in mol.GetBonds()
    )
    canonical_with_stereo = Chem.MolToSmiles(mol, isomericSmiles=True)
    canonical_without_stereo = Chem.MolToSmiles(mol, isomericSmiles=False)
    if n_total == 0 and not has_directional:
        chirality = "achiral"
    elif n_unspecified > 0:
        chirality = "racemic_mix"
    elif canonical_with_stereo == canonical_without_stereo:
        chirality = "achiral"
    else:
        chirality = "chiral"
    return {
        "n_stereocenters": n_total,
        "n_specified": n_specified,
        "n_unspecified_stereocenters": n_unspecified,
        "has_double_bond_geometry": bool(has_directional),
        "canonical_with_stereo": canonical_with_stereo,
        "chirality": chirality,
    }
```

返回 6 个新字段，全部并入 `validate_smiles()` 的输出：

```json
{
  "valid": true,
  "smiles": "C[C@H](N)C(=O)O",
  "n_stereocenters": 1,
  "n_specified": 1,
  "n_unspecified_stereocenters": 0,
  "has_double_bond_geometry": false,
  "canonical_with_stereo": "C[C@H](N)C(=O)O",
  "chirality": "chiral"
}
```

### 为什么 `n_unspecified_stereocenters` 是关键字段

一个分子可以有**手性中心但没有 `@`/`@@` 标注**——`(R)` 还是 `(S)` 都没说。RDKit 用 `CalcNumUnspecifiedAtomStereoCenters` 计数这种"原子层面是手性的但 SMILES 没标"的中心数。

- `n_unspecified > 0` ⇒ **外消旋混合物**（除非实验者指定）；
- `n_unspecified == 0 && n_total > 0` ⇒ **纯对映体**；
- `n_total == 0 && !has_directional` ⇒ **无手性、无 E/Z**，完全 achiral。

药物化学里**外消旋 vs 纯对映体的 ADMET 性质可能差 10 倍**（沙利度胺事件就是这个原因）。给下游一个明确的 `chirality` 字段比"看 SMILES 字符串猜"安全得多。

### 设计取舍

| 决策 | 选择 | 理由 |
|---|---|---|
| 用 RDKit 哪个 API | `CalcNumAtomStereoCenters` / `CalcNumUnspecifiedAtomStereoCenters` | 这两个稳定（2020.09+）；`CalcMolAtomStereoInfo` 返回 dataclass，跨版本字段不兼容 |
| 调 `AssignStereochemistryFrom3D` 吗 | 否 | 没有 3D 坐标，调了也没用；只在已经有 2D 嵌入的场景调 |
| E/Z 几何算进 `n_stereocenters` 吗 | 不算（只用 `has_double_bond_geometry` 布尔） | RDKit 的 `CalcNumAtomStereoCenters` 只数原子级手性；E/Z 单独用 `bond.GetStereo()` 检测 |
| `canonical_with_stereo` 是否冗余 | 保留 | 与 `Chem.MolToSmiles(mol)` 默认行为相同；但显式字段让调用者不用知道 RDKit 默认值 |
| 是否给 `chirality` 加新枚举 | 是 | 4 个值够用：`achiral` / `chiral` / `racemic_mix` / `unknown`（保留位） |

### 回归测试

`tests/test_validate_mol_stereo.py` — 9 个测试：

| 测试 | 验证内容 |
|---|---|
| `test_achiral_molecule_has_zero_stereocenters` | 苯酚 → 0 centers, `chirality="achiral"` |
| `test_achiral_with_no_double_bonds_keeps_zero` | 乙醇 → 同上 |
| `test_chiral_round_trip_preserves_at_annotation` | `(S)` 与 `(R)` alanine canonical **必须**不同 |
| `test_unspecified_stereocenter_is_flagged` | 无 `@` 的 alanine → `n_unspecified=1`, `chirality="racemic_mix"` |
| `test_invalid_smiles_returns_no_stereo_block` | 非法 SMILES 不产出 stereo 字段 |
| `test_validate_batch_returns_independent_blocks` | batch 不会互相污染 |
| `test_lipinski_unaffected_by_stereo` | Lipinski 仍然正确（不要回归） |
| `test_double_bond_geometry_is_detected` | `C/C=C/C` 与 `C/C=C\C` canonical 不同 |
| `test_double_bond_without_geometry_not_flagged` | `CC=CC` → `has_double_bond_geometry=False` |

## 41.4 文件变更与测试统计

### 改动的文件

| 文件 | 变化 | 说明 |
|---|---|---|
| `agents/harness/runtime.py` | `+27 / -3` | `add_prediction_error` 异常处理改为审计 + stderr |
| `agents/rule_memory.py` | `+32 / -3` | `add_prediction_error` 保留 `observations_log` 完整历史 |
| `agents/agent_metrics.py` | `+98 / -1` | 新增 `compute_calibration_metrics`；`compute_agent_metrics` 接受 `rule_store` 参数 |
| `tools/validate_mol.py` | `+60 / -1` | 新增 `_stereochemistry_block`；导入 `rdMolDescriptors` |
| `loop.py` | `+14 / -1` | 读 `memory_base / "rule_memory.json"` 喂给 metrics；写 `calibration` 到 `summary.json` |
| `scripts/visualize_memory.py` | 新增（247 行） | 4 类 PNG + JSON 报告；matplotlib 缺失自动降级 |
| `tests/test_runtime_calibration.py` | 新增（243 行） | 7 个集成测试 |
| `tests/test_calibration_metrics.py` | 新增（140 行） | 8 个聚合测试 |
| `tests/test_visualize_memory.py` | 新增（149 行） | 9 个可视化测试 |
| `tests/test_validate_mol_stereo.py` | 新增（128 行） | 9 个立体化学测试 |
| `tests/test_phase4_3.py` | `+1 / -1` | `schema_version == 2` → `3` |
| `docs/PROJECT_HANDBOOK.md` | `+295` | 新增第 41 章 |
| `README.md` | `+90` | 新增「2026-09-27：Phase 4.4」节 |

### 测试统计

```
Before: 336 passed, 1 skipped
After:  369 passed, 1 skipped   (+33 tests, all four priorities green)
```

新增覆盖：

| 类别 | 测试 | 数量 |
|---|---|---|
| Runtime EVIDENCE 路径 | `test_runtime_calibration.py` | 7 |
| Calibration metrics 聚合 | `test_calibration_metrics.py` | 8 |
| 内存可视化（CLI + 函数） | `test_visualize_memory.py` | 9 |
| 立体化学 round-trip | `test_validate_mol_stereo.py` | 9 |

### 不在本章范围

- **MD 动力学验证**（P3-2）：GROMACS 集成是大工程，留到有 top 候选值得做动力学时启动；
- **ADMET 模型升级**（P3-1）：换 admetSAR/SwissADME 是研究投入，目前 RDKit 描述符对趋势观察够用；
- **跨 run 的 calibration drift 检验**：当前 `metrics.json` 只报告本次 run 的 calibration 累积值；要做跨 run 趋势需要单独工具。