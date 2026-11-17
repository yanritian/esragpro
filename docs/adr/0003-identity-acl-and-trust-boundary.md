# ADR 0003: 身份、ACL 和信任边界

## 状态

已批准，部分被 ADR 0005 取代 (Approved, partially superseded by ADR 0005)

> 适用范围说明：本 ADR 中 `PrincipalContext`、OAuth/scope、租户和 ACL 预过滤的合同**仅约束 Enterprise Gateway、远程 adapter、企业领域 API 和企业存储查询边界**。通用无权限 Memory Core 的公开领域合同与本地 MCP stdio **不要求** `PrincipalContext`、认证字段或领域授权。企业路径的检索前存储层 ACL 过滤、撤权屏障、审计和 fail-closed 要求不得削弱。详见 [ADR 0005](./0005-dynamic-world-memory-boundary.md)。

## 上下文

esRAGPro Enterprise 需要支持多租户、知识库级授权、文档级授权和审计。现有实现（backend/app/api/auth.py）存在安全漏洞：
- 注册接口允许客户端提交 `role`
- 缺少统一的认证和授权中间件
- 查询未执行 ACL 预过滤

原始方案（PRD.md E-13, E-14）未明确定义信任边界和权限传播机制。

## 决策

采用**端到端企业授权**架构：

### 1. 信任边界

系统存在四个信任边界：
1. **客户端与 Gateway**：MCP 客户端不可信，所有输入需要验证
2. **Gateway 与领域 API**：Gateway 必须通过受认证通道调用领域 API
3. **真源与投影**：投影必须从真源重建，不得反向修改
4. **企业域与外部模型**：文档内容、查询和 LLM 输出都是不可信输入

### 2. PrincipalContext

`PrincipalContext` 仅约束**企业远程路径**（Enterprise Gateway、远程 adapter、企业领域 API 和企业存储查询边界）。通用无权限 Memory Core 的公开领域合同和本地 MCP stdio 调用**不要求** `PrincipalContext`、认证字段或领域授权。

企业远程路径的所有领域操作必须接收以下上下文（仅由服务端构造）：

```json
{
  "context_version": 1,
  "request_id": "uuidv7",
  "trace_id": "w3c-trace-id",
  "tenant_id": "uuidv7",
  "subject_id": "stable-subject-id",
  "subject_type": "user|service",
  "client_id": "oauth-client-id",
  "authn": {
    "issuer": "https://idp.example.com",
    "audience": "https://mcp.example.com/mcp",
    "method": "oauth|workload|local-stdio"
  },
  "authorization": {
    "scopes": ["knowledge:read"],
    "roles": ["reader"],
    "group_ids": ["uuidv7"],
    "knowledge_base_ids": ["uuidv7"],
    "clearance": "internal",
    "policy_version": "v1",
    "authorization_revision": 42,
    "authorization_fingerprint": "sha256",
    "access_partition_ids": ["u:subject", "g:group", "r:reader"]
  },
  "delegation": {
    "on_behalf_of": "subject-id",
    "chain": ["client-id"]
  }
}
```

**禁止**：
- 工具参数包含 `tenant_id`、`subject_id`、`roles`、`group_ids`
- 客户端提交的身份字段覆盖服务端 PrincipalContext

### 3. ACL 预过滤

企业远程路径的所有召回必须在存储层执行 ACL 预过滤（由 Enterprise Gateway/adapter 在调用 Memory Core 之前把租户/ACL 约束编译为存储或查询过滤）：

**Elasticsearch**：
```json
{
  "bool": {
    "filter": [
      {"term": {"tenant_id": "current_tenant"}},
      {"terms": {"knowledge_base_id": ["authorized_kbs"]}},
      {"terms": {"access_partition_ids": ["allowed_partitions"]}}
    ]
  }
}
```

**Neo4j**：
```cypher
MATCH (c:Claim)
WHERE c.tenant_id = $tenant_id
  AND c.knowledge_base_id IN $kb_ids
  AND c.publication_state = 'active'
  AND c.access_partition_ids IN $partitions
  AND datetime() >= c.valid_from
  AND datetime() <= c.valid_to
```

**禁止**：
- 全局召回后在应用层删除无权结果
- 查询后才检查权限

### 4. 撤权屏障

权限收紧必须立即生效，投影滞后期间使用 `revocation_barrier`：

1. PostgreSQL 事务递增 `authorization_revision`
2. 同一事务写入 `revocation_barrier`（包含受影响的 KB/文档/Claim）
3. 查询前读取当前 revision 和未确认的 barrier
4. 将 barrier 编译为查询的 `must_not` 条件
5. 投影确认新 revision 后清除 barrier

**关键**：
- Barrier 不可用时 `fail closed`（拒绝查询）
- 不得形成撤权泄漏窗口

### 5. 外部 MCP Scopes 映射

| Tool | Scope |
|------|-------|
| kb.search_evidence | knowledge:read |
| kb.answer_with_evidence | knowledge:answer |
| graph.lookup_entity / expand / find_paths | graph:read |
| graph.verify_claim | graph:verify |
| admin.create_ingest_job | ingest:create |
| admin.cancel_ingest_job | ingest:cancel |
| admin.request_projection_rebuild | projection:rebuild |
| admin.request_document_deletion | document:delete:request |

**内部权限**：
- kb:read, document:read, document:write, document:delete
- claim:review, audit:read

## 理由

### 为什么选择此方案

1. **最小权限**：每个操作只授予必需权限
2. **纵深防御**：Gateway、领域 API、投影层都执行授权
3. **审计完整**：每个操作都有可追溯的身份上下文
4. **撤销立即**：通过 barrier 机制保证权限收紧立即可见

### 替代方案及为何拒绝

**替代方案 1：查询后过滤**
- ❌ 泄漏文档存在性
- ❌ 统计数据不准确
- ❌ 缓存污染

**替代方案 2：相信客户端身份**
- ❌ 安全漏洞（客户端可冒充管理员）
- ❌ 无法审计

## 后果

### 正面后果

1. 跨租户数据完全隔离
2. 权限收紧立即生效
3. 所有操作可审计
4. 符合企业安全合规要求

### 负面后果

1. 查询性能可能下降（需要加入 ACL filter）
2. 索引需要包含 ACL 字段
3. 需要维护 barrier 机制

### 缓解措施

1. ACL 字段包含在索引 mapping 中
2. 使用缓存减少重复计算（缓存键包含 revision）
3. 提供 ACL 检查工具和测试

## 遵守要求

后续所有工作包必须遵守：

1. **WP1**：修复注册漏洞，移除客户端 role 提交
2. **WP2**：在**企业远程路径**实现 PrincipalContext 和 ACL 预过滤
3. **WP2-WP8**：**企业路径**的所有查询必须包含 ACL filter
4. **所有 WP**：企业路径不得相信工具参数中的身份字段
5. **豁免**：通用无权限 Memory Core 的公开领域合同与本地 MCP stdio 不要求 `PrincipalContext`、认证字段或领域授权
6. **不得削弱**：企业路径的检索前存储层 ACL 过滤、撤权屏障、审计和 fail-closed 要求继续生效

## 参考

- [DECISION-REVIEW.md](../../esragpro-mcp/docs/DECISION-REVIEW.md) C-07, E-13, E-14
- [ARCHITECTURE.md](../../esragpro-mcp/docs/ARCHITECTURE.md) 第 3 节
- [PLAN.md](../../esragpro-mcp/docs/PLAN.md) WP2

## 决策历史

- **2026-07-10**: 初始版本，由 WP0 创建
- **2026-07-23**: ADR 0005 对 `PrincipalContext` 的适用范围作了部分取代。`PrincipalContext`、OAuth/scope、租户和 ACL 预过滤继续约束 Enterprise Gateway、远程 adapter、企业领域 API 和存储查询边界；但通用无权限 Memory Core 的公开领域合同与本地 MCP stdio 不要求 `PrincipalContext`、认证字段或领域授权。企业路径的检索前存储层 ACL 过滤、撤权屏障、审计和 fail-closed 要求不得削弱。详见 [ADR 0005](./0005-dynamic-world-memory-boundary.md)。