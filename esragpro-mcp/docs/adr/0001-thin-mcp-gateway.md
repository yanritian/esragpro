# ADR 0001: 薄 MCP Gateway

## 状态

已批准 (Approved)

## 上下文

esRAGPro 需要向 Claude Code、Qwen 等智能体暴露 MCP 接口。

原始方案（PRD.md E-01, E-07）在 Rust MCP 中维护独立的企业数据面（LanceDB、SQLite、ONNX），导致：
- 双写问题：两套数据源需要同步
- 权限漂移：两套 ACL 规则不一致
- 审计困难：两套审计记录难以关联

## 决策

MCP Gateway 是**薄协议适配层**，不维护独立数据面：

### 1. 职责边界

**MCP Gateway 负责**：
- MCP 生命周期管理（initialize, ping, shutdown）
- 传输适配（stdio, Streamable HTTP）
- Schema 校验（inputSchema, outputSchema）
- PrincipalContext 构造（从 OAuth/stdio 凭据）
- Scope 检查和授权
- 限流和审计关联
- 结果包装（structuredContent, resource_link）
- 错误映射（后端错误 → MCP Tool Error）

**MCP Gateway 明确禁止**：
- 自建知识库
- 绕过领域 API 直写 ES/Neo4j
- 信任工具参数中的 tenant_id/subject_id
- 维护独立的文档/向量/图谱存储
- 执行 LLM 推理或嵌入计算

### 2. 调用流程

```
智能体 → MCP Gateway → 领域 API → PostgreSQL/ES/Neo4j
         ↓
    PrincipalContext
    Scope 检查
    限流
    审计
```

**关键**：
- Gateway 只调用领域 API，不直接访问存储
- 所有身份信息来自服务端构造的 PrincipalContext
- 所有业务逻辑在领域 API 层执行

### 3. 只读 Tools (6 个)

| Tool | 作用 | Scope |
|------|------|-------|
| kb.search_evidence | 证据检索 | knowledge:read |
| kb.answer_with_evidence | 证据回答 | knowledge:answer |
| graph.lookup_entity | 实体查询 | graph:read |
| graph.expand | 邻居扩展 | graph:read |
| graph.find_paths | 路径查询 | graph:read |
| graph.verify_claim | 声明核验 | graph:verify |

### 4. 管理 Tools (4 个，独立 profile)

| Tool | Scope | 约束 |
|------|-------|------|
| admin.create_ingest_job | ingest:create | 只接受 source_ref |
| admin.cancel_ingest_job | ingest:cancel | 只取消授权范围任务 |
| admin.request_projection_rebuild | projection:rebuild | 只创建审批请求 |
| admin.request_document_deletion | document:delete:request | 只创建审批请求 |

**管理 Tools 默认关闭**，仅在 Streamable HTTP 远程 profile 中启用。

### 5. Resources (1 + 8)

**静态 Resource**：
- `esrag://server/capabilities`

**Resource Templates** (8 个)：
- `esrag://kb/{kb_id}/documents/{doc_id}/versions/{version_id}`
- `esrag://kb/{kb_id}/evidence/{evidence_id}`
- `esrag://kb/{kb_id}/entities/{entity_id}`
- `esrag://kb/{kb_id}/claims/{claim_id}`
- `esrag://kb/{kb_id}/ontology/{schema_version}`
- `esrag://kb/{kb_id}/jobs/{job_id}`
- `esrag://kb/{kb_id}/catalog`
- `esrag://kb/{kb_id}/stats`

### 6. Prompts (4 个)

- answer-with-citations
- compare-evidence
- trace-requirement
- verify-claim

## 理由

### 为什么选择此方案

1. **单一责任**：Gateway 只负责协议适配，不承担业务逻辑
2. **数据一致**：只有一套数据源，避免同步问题
3. **权限集中**：ACL 在领域 API 层统一管理
4. **审计完整**：所有操作都有完整审计链

### 替代方案及为何拒绝

**替代方案 1：MCP 直连 ES/Neo4j**
- ❌ 绕过领域授权
- ❌ 数据一致性问题
- ❌ 审计不完整

**替代方案 2：MCP 维护独立数据面**
- ❌ 双写复杂
- ❌ 权限漂移
- ❌ 运维负担加倍

## 后果

### 正面后果

1. 架构清晰，职责明确
2. 数据一致性有保证
3. 权限管理集中
4. 易于测试和维护

### 负面后果

1. Gateway 必须依赖领域 API 可用
2. 性能受领域 API 影响
3. 需要维护 HTTP 客户端逻辑

### 缓解措施

1. 提供健康检查和降级机制
2. 使用异步调用和超时控制
3. 提供本地缓存（短期）

## 遵守要求

后续所有工作包必须遵守：

1. **WP6-WP7A**：Gateway 只调用领域 API，不直连存储
2. **所有 WP**：Gateway 不信任工具参数中的身份字段
3. **WP7A**：管理 Tools 通过 source_ref 和审批闭环，不直接操作存储

## 参考

- [DECISION-REVIEW.md](../DECISION-REVIEW.md) X-01, X-02
- [ARCHITECTURE.md](../ARCHITECTURE.md) 第 2 节
- [PLAN.md](../PLAN.md) WP6, WP7A

## 决策历史

- **2026-07-10**: 初始版本，由 WP0 创建