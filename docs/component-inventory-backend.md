# Backend 组件清单

## API 组件

| 组件 | 主要职责 |
|---|---|
| `auth.py` | 注册、登录、当前用户、管理员创建 |
| `kb.py` | 文档上传、列表、搜索、删除、健康检查 |
| `memory.py` | 会话、消息、上下文和工作记忆 |
| `memory_plane.py` | Phase 2-A remember/query/context |
| `graph.py` | 实体、关系、子图、图谱质量和健康 |
| `agentic.py` | Agentic RAG 问答和策略查看 |
| `smart_search.py` | 智能检索入口和统计 |
| `validate.py` / `evaluation.py` | 质量验证、评测和报告 |
| `task.py` | 任务、步骤、结果记录 |

## 检索与回答组件

- `core/elasticsearch.py`：传统 ES 客户端、kNN、BM25、RRF。
- `services/embedding.py`：Embedding 模型/远程 embedding 配置。
- `services/rag.py`：传统 RAG 生成和来源输出。
- `services/smart_retrieval.py`：工作记忆、长期记忆、知识库融合入口。
- `services/retrieval_strategy.py`：查询意图和策略选择。
- `services/agentic_rag.py`、`agentic_router.py`：Agentic RAG 编排。
- `services/evidence_packet.py`：证据块、声明支持、检索轨迹。
- `services/hallucination_guard.py`、`refusal_service.py`：声明核查、拒答、置信度。
- `services/graph_rag.py`、`knowledge_graph.py`：图增强和 Neo4j 交互。

## Memory Core 组件

- 合同：`contracts.py`、`unpermissioned_contracts.py`。
- 写入：`ingestion.py`、`outbox.py`、DLP 和同意校验。
- 治理：`review.py`、claims、冲突检测和晋级。
- 生命周期：`lifecycle.py`、retention、legal hold、审计。
- 检索：`retrieval.py`、`unpermissioned_retrieval.py`、token budget。
- 质量：`evaluator.py`、`holdout.py`、`release.py`、`metrics.py`、`health.py`。
- 存储边界：PostgreSQL、ES、Redis、Neo4j、MinIO adapter。

## 安全组件

- `security/principal.py`：主体、认证方法、clearance、role、scope、permission。
- `security/middleware.py`：JWT/Principal 解析和 FastAPI dependencies。
- `security/acl.py`：tenant、knowledge base、visibility、legal hold 过滤。
- `security/revocation.py`：撤销屏障。
- `guardrails/injection.py`：Prompt injection 防护。

## 共享和可复用组件

- 稳定 ID/版本：`services/stable_id.py`。
- 文档解析：`services/document.py`。
- 上传和 Blob：`services/upload_session.py`、`storage/blob.py`。
- 数据库模型：`db/models/*`。
- 投影：`projectors/elasticsearch.py`、`projectors/neo4j.py`。
