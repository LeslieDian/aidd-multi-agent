# 长期数据库选型与准备

推荐 **PostgreSQL + pgvector**。数据库保存全部事实，向量索引用于检索。
本阶段没有连接或部署数据库；本地 JSON、姿势文件后续通过迁移器入库。

## 你现在需要建立的实例

- 一个受支持版本的 PostgreSQL 实例，服务端已安装 pgvector。
- 一个名为 `aidd` 的数据库、独立应用账号、持久化存储及自动备份。
- 本机开发可以使用 pgvector 官方 Docker 镜像；长期在线可选明确支持
  pgvector 的托管 PostgreSQL。仅安装 PostgreSQL 不代表已经安装扩展。
- 在 `aidd` 数据库执行：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
SELECT extversion FROM pg_extension WHERE extname = 'vector';
```

连接信息放在本机 `.env` 的 `DATABASE_URL`，不要发送密码到聊天或提交到 Git。
先不创建固定 embedding 维度的列：维度取决于选定的 embedding 模型与版本。
连接、建表、自动写入和迁移属于后续数据库接入工作，不会因为建立实例自动完成。

## 哪些记录需要保存

| 数据 | 是否保存 | 是否需要 embedding |
|---|---|---|
| LLM 原始输出、每次提议与模型来源 | 全部保存 | 通常不需要 |
| 无法解析的 SMILES | 保存原始文本与错误，不能假装有效分子 | 不生成分子指纹 |
| 有效但被规则过滤的候选 | 保存结构、规则、阈值和理由 | 可计算分子指纹 |
| docking 超时/工具异常 | 保存评估尝试、错误和重试关系 | 不标记为生物学失败 |
| 完整评估但未达标的候选 | 保存全部原始指标与协议 | 可计算分子指纹 |
| 达标候选 | 保存全部指标与来源；达标是版本化状态 | 可计算分子指纹 |
| 经证据支持的策略经验、文献 | 保存来源、关联记录和适用条件 | 适合语义 embedding |

相同分子结构可以只存一次，但每次由谁提出、在哪个任务出现、为什么被筛掉、
执行了哪些评估都要另存记录。分子和评估是“一对多”，不是一行不断覆盖。
“达标”依赖靶点、受体状态、目标和评分协议，不是永久标签。

## 后续表设计

`campaigns`, `runs`, `molecules`, `proposals`, `evaluations`,
`filter_events`, `strategy_memories`, `artifacts`。

无效提议的 `molecule_id` 可为空；重复提议共享 molecule_id 但保留各自 proposal_id。
筛选和评估写追加记录。姿势和大文件放文件系统/对象存储，数据库保存 URI、哈希、类型。
只有有效标准化分子计算 Morgan 指纹；分子检索使用 Tanimoto/Jaccard，
策略与文献使用经过验证的文本 embedding，不能混为一个相似度指标。

向量是可重建的索引，事实记录是长期资产。更换 embedding 模型时保留模型名、版本、
维度与原文哈希；旧错误协议数据要标记历史/不可信，不能混入当前有效经验检索。

官方安装与使用说明：https://github.com/pgvector/pgvector
