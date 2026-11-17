# Backend 数据模型与存储投影

## 数据源分层

当前代码中存在两套相关但尚未完全收敛的数据层：

1. 企业知识核心的 PostgreSQL ORM、Alembic、ES 文档块和 Neo4j 图谱。
2. Memory Core 的领域合同和目标适配器；新无权限纵向切片暂时使用进程内 Repository。

## PostgreSQL 领域模型

### 身份和租户

- `tenants`
- `users`
- `memberships`
- `knowledge_bases`
- Principal、授权上下文和撤销屏障相关记录

### 文档和摄取

- `documents`
- `document_versions`
- `document_chunks`
- `ingestion_jobs`
- `ingestion_job_outbox`
- `deletion_jobs`
- `upload_sessions`

关键约束包括 tenant、knowledge base、document/version 外键，幂等 key、重试状态、删除状态和 source reference。

### Claim、实体和关系

- `entities`
- `entity_aliases`
- `claim_candidates`
- `candidate_evidence`
- `claims`
- `claim_evidence`
- `claim_review_decisions`
- `claim_conflicts`
- ontology version 相关表

Claim 具有 predicate 白名单、对象类型、时间有效期、confidence、revision、publication state、superseded/revoked 信息，并通过 evidence 关联文档版本和 chunk。

### Memory、会话和任务

- Working/mid-term memory 迁移文件存在于 `backend/alembic/versions/wp10_*`。
- `sessions`、消息和上下文数据支撑旧会话 Memory API。
- `tasks`、`task_steps`、`task_results` 记录任务过程，但这些记录不等于 Agent Plane 的动作执行器。

## Elasticsearch

传统知识库使用 `knowledge_base` 类索引投影，文档块包含：

- `doc_id`
- `title`
- `content`
- `content_embedding`
- `source`
- `doc_type`
- `chunk_index`
- 时间字段和知识库维度

传统 `ESClient.hybrid_search` 执行 BM25 和 kNN，再用 RRF 合并。Memory 专用 `ElasticsearchIndexAdapter` 当前仍是适配器接口和 mock 行为，不能视为 Memory 数据已写入 ES。

## Neo4j

知识图谱服务按实体节点和关系边组织，支持实体、关系、子图、路径和健康检查。Memory 目标图投影还需要把 Memory Item、Evidence、Entity、Claim 和 temporal relation 以稳定 ID 投影，并实现删除/撤销传播。

## Memory Core 合同

### 受治理合同

`backend/app/memory_core/contracts.py` 定义：

- `ExternalIdentity`
- `InternalPrincipal`
- `AgentAuthorizationContext`
- `MemoryAccessContext`
- `MemoryObservation`
- `ExtractedCandidateDraft`
- `MemoryItem`
- `EvidencePacket`
- `MemoryContextPack`
- `AgentContextBundle`

### Phase 2-A 无权限合同

`unpermissioned_contracts.py` 定义：

- `EvidenceRef`
- `ProvenanceInfo`
- `MemoryItem`
- `Observation`
- `UnpermissionedMemoryQuery`
- `MemoryQueryResult`
- `EvidencePacket`
- `MemoryContextPack`
- `QueryStatus`

这些合同允许 Agent 在没有授权上下文时使用记忆体，但没有消除企业部署的外层安全责任。

## 生命周期与一致性

目标状态链：

```text
observation -> candidate -> review -> active -> expired/archived -> deleted
```

写入时应先保存权威记录和 Outbox，再异步投影 ES、Neo4j、Redis、MinIO。任何投影滞后都必须可观测，不能返回看似最新但实际未同步的事实。

## 当前数据模型风险

- ORM/迁移存在不代表运行时已经连接并迁移数据库。
- Memory 专用 adapter 仍大量返回 mock/空结果。
- 新无权限 Repository 的数据不会自动进入传统 tenant/ACL 模型。
- GraphRAG 结果和 Claim 候选必须区分“候选关系”和“已发布事实”。
