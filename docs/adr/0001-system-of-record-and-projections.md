# ADR 0001: 系统记录与投影

## 状态

已批准 (Approved)

## 上下文

esRAGPro 需要明确数据所有权和职责边界，避免多数据源导致的不一致、权限漂移和审计困难。

原始方案（PRD.md E-01）同时维护 esRAGPro Web 和 Rust MCP 两个平行产品，会产生双写、权限漂移、删除不一致和重复评测问题。

## 决策

Enterprise Edition 采用**单一权威记录**架构：

1. **PostgreSQL** 和**对象存储**构成系统记录（System of Record）：
   - PostgreSQL：租户、用户、知识库、文档元数据、版本、ACL、摄取任务、候选事实、审核记录、审计事件
   - 对象存储：原始文件、规范化文本、布局/OCR 制品（不可变，按版本）

2. **Elasticsearch** 和 **Neo4j** 是可重建的查询投影（Query Projections）：
   - Elasticsearch：文本/向量检索投影，包含 tenant_id、kb_id、document_id、version_id、ACL 分区
   - Neo4j：active Claim 图投影，每条边有证据引用和 ACL 分区

3. **投影损坏或丢失**：可从 PostgreSQL + 对象存储重建，不损失权威数据

4. **MCP Gateway**：是协议适配层，不维护独立知识数据面，不直连 ES/Neo4j，只调用领域 API

## 理由

### 为什么选择此方案

1. **单一责任**：权威记录只有一份，避免双写和同步复杂性
2. **可恢复性**：投影损坏可重建，不影响业务数据
3. **审计完整性**：所有写操作在 PostgreSQL 事务中完成，有完整审计链
4. **权限一致**：ACL 在 PostgreSQL 中集中管理，投影通过 outbox 同步

### 替代方案及为何拒绝

**替代方案 1：ES/Neo4j 作为真源**
- ❌ 无法保证跨系统事务一致性
- ❌ ACL 难以集中管理和审计
- ❌ 删除传播困难

**替代方案 2：双写 PostgreSQL + ES/Neo4j**
- ❌ 同步复杂，容易产生不一致
- ❌ 权限漂移风险
- ❌ 审计困难

## 后果

### 正面后果

1. 数据一致性有保证（单事务提交）
2. 投影可重建，故障恢复简单
3. ACL 集中管理，权限收紧立即可见
4. 审计完整，可追溯每个操作

### 负面后果

1. 需要维护 outbox 和投影器，增加复杂度
2. 投影可能短暂滞后（eventual consistency）
3. 需要 PostgreSQL 运维能力

### 缓解措施

1. Outbox 采用事务性消息，保证至少一次投递
2. 投影器幂等，支持重试和断点续跑
3. 提供 Reconciler 巡检工具，自动发现和修复不一致
4. 提供 RPO/RTO 承诺和恢复演练

## 遵守要求

后续所有工作包必须遵守：

1. **WP1-WP8**：不得让 MCP 或其他组件绕过领域 API 直写 ES/Neo4j
2. **WP2**：ACL 必须在召回前执行，投影只能包含已授权数据
3. **WP3**：删除采用 tombstone + outbox，投影确认后才置 deleted
4. **WP4**：只有 approved ClaimCandidate 才能投影为 active Claim
5. **WP8**：必须验证投影可从权威记录重建

## 参考

- [DECISION-REVIEW.md](../esragpro-mcp/docs/DECISION-REVIEW.md) C-01, C-02
- [ARCHITECTURE.md](../esragpro-mcp/docs/ARCHITECTURE.md) 第 2、4 节
- [PLAN.md](../esragpro-mcp/docs/PLAN.md) WP0, WP2, WP3

## 决策历史

- **2026-07-10**: 初始版本，由 WP0 创建