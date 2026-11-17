# Backend 架构

## 架构定位

Backend 是一个以 FastAPI 为入口的分层服务，当前同时承载两条产品链：

1. 已有企业知识库链：文档 → 分块/Embedding → Elasticsearch 混合检索 → RAG/证据/拒答。
2. 目标记忆链：观察/记忆合同 → 证据引用 → 检索 → Context Pack → MCP/Agent 消费。

两条链目前共享服务和数据边界，但还没有统一成一个真实持久化的 Memory Plane 运行时。

## 请求流

### 传统企业 RAG

```text
POST /api/v1/kb/search
  -> kb.py
  -> rag.py / smart_retrieval.py
  -> EmbeddingService
  -> ESClient.hybrid_search
  -> BM25 + kNN + RRF
  -> Evidence / HallucinationGuard
  -> SearchResponse
```

`ESClient` 中的混合检索和 RRF 是当前真实的传统检索实现。

### Agent Memory Plane Phase 2-A

```text
POST /api/v1/memory-plane/remember
POST /api/v1/memory-plane/query
POST /api/v1/memory-plane/context
  -> memory_plane.py
  -> UnpermissionedSmartRetrievalService
  -> UnpermissionedMemoryRepositoryPort
  -> InMemoryUnpermissionedMemoryRepository
```

该路径强制携带证据引用，并在无证据时返回拒答/无匹配状态；但是参考 Repository 是进程内实现，未接入 PostgreSQL、Elasticsearch、Redis 或 Neo4j。

## 领域模块

| 模块 | 职责 | 生产完成度 |
|---|---|---|
| `memory_core/contracts.py` | ExternalIdentity、MemoryObservation、MemoryItem、EvidencePacket、ContextPack | 合同完成，需接入运行时 |
| `memory_core/ingestion.py` | consent、DLP、不可变观察、Outbox | 有服务和单测，真实消费者需补齐 |
| `memory_core/review.py` | 候选事实审核、证据验证、冲突处理、晋级 | 有服务和单测 |
| `memory_core/lifecycle.py` | 过期、归档、删除、法律保留、审计 | 逻辑有实现，跨存储删除仍是模拟 |
| `memory_core/retrieval.py` | 分层记忆检索、权限过滤、token budget | 受治理骨架，候选召回仍有 mock 入口 |
| `memory_core/repositories/*` | PostgreSQL、ES、Redis、Neo4j、MinIO ports | 当前主要为 mock/stub |
| `services/evidence_packet.py` | 证据块、声明支持、检索轨迹 | 传统 RAG 证据链已有实现 |
| `services/knowledge_graph.py` | Neo4j 实体、关系、子图和路径 | 有真实驱动交互，但 GraphRAG 调用链不完整 |
| `security/*` | Principal、JWT、ACL、tenant 隔离、撤销屏障 | 传统企业链存在；无权限 Memory Plane 明确绕过该层 |
| `claims/*` | 实体解析、Claim 候选、冲突、审核和发布状态 | 领域模型和迁移已存在 |

## 权限边界

传统企业接口使用 Principal、tenant、knowledge base 和 scope/permission 约束。新 Memory Plane 的 `project_id`、`session_id`、`agent_id`、`knowledge_base_id` 当前只作为数据维度和查询范围，不是 ACL。

这符合“记忆体不承担 Agent 权限编排”的方向，但要注意：企业共享部署仍必须在外层 Gateway、宿主应用或 Agent Runtime 建立授权边界，不能把无权限接口直接暴露给跨租户网络。

## 主要架构缺口

1. 参考 Repository 与真实数据库之间没有可替换的生产 wiring。
2. Outbox 具备数据结构和内存实现，但还缺少可靠的持久化 worker、重放和投影确认。
3. GraphRAG `_query_graph` 当前存在降级实现，关系检索不能作为稳定质量门禁。
4. 后端 import/单测通过不等于 Docker Compose 的 backend、Redis、Neo4j、frontend 全链路可用。
5. 目前没有热路径 P50/P95/P99、证据覆盖率、拒答正确率和新鲜度的持续基准。
