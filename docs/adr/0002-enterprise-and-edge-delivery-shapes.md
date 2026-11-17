# ADR 0002: Enterprise 与 Edge 交付形态

## 状态

已批准 (Approved)

## 上下文

esRAGPro 需要支持两种部署场景：
1. **Enterprise Edition**：多租户、高可用、在线访问、企业集成
2. **Local Edge Edition**：单用户、离线、隔离网、现场设备

原始方案（PRD.md E-01）试图在一个产品中同时满足两种场景，导致架构冲突：
- LanceDB/SQLite 与 ES/Neo4j 双数据面
- 单二进制与企业多服务混用
- 本地模型与云端 LLM 选择困难

## 决策

采用**一个知识核心、两种交付形态**：

### 1. Enterprise Edition（首要交付）

**目标用户**：企业知识工作者、IT、安全、知识管理员、审计人员

**部署特征**：
- PostgreSQL + 对象存储（权威记录）
- Elasticsearch + Neo4j（查询投影）
- FastAPI 领域 API
- Rust MCP Gateway（薄协议层）
- 支持高可用、多租户、OAuth/OIDC
- 通过 Streamable HTTP 暴露 MCP

**数据面**：
- 真源：PostgreSQL、对象存储
- 投影：Elasticsearch、Neo4j
- 缓存：Redis（可选）

**优先级**：首要交付，WP0-WP8 必须完成

### 2. Local Edge Edition（后续交付）

**目标用户**：单人、隔离网、现场设备、离线工作站

**部署特征**：
- Rust 单二进制
- 嵌入式存储（LanceDB、SQLite 等）
- 离线模型
- 通过 stdio 暴露 MCP
- 从 Enterprise 导入签名知识包

**数据面**：
- 本地存储（与 Enterprise 物理不兼容）
- 复用相同的领域契约、MCP Schema 和评测集

**优先级**：后续交付，仅在 WP8 通过且 GO-EDGE 批准后启动

### 3. MCP Gateway 共享契约

两种形态共享：
- 相同的 6 个只读 Tools
- 相同的 1 个静态 Resource + 8 个 Resource Templates
- 相同的 4 个 Prompts
- 相同的 JSON Schema (mcp-v1, domain-api-v1, principal-context-v1)
- 相同的证据模型、Claim 生命周期

**差异**：
- Enterprise：额外提供管理 Tools（独立 profile）、Streamable HTTP、OAuth
- Edge：只提供 stdio、不提供管理 Tools、不调用云 LLM

## 理由

### 为什么选择此方案

1. **职责清晰**：Enterprise 负责企业数据管理，Edge 负责离线查询
2. **复用契约**：避免维护两套 MCP Schema 和领域模型
3. **循序渐进**：先稳定 Enterprise，再考虑 Edge
4. **数据隔离**：Edge 不反向修改 Enterprise 数据，避免同步复杂性

### 替代方案及为何拒绝

**替代方案 1：一个产品支持两种场景**
- ❌ 架构冲突，难以维护
- ❌ 性能和部署复杂度难以权衡
- ❌ 增加测试和发布负担

**替代方案 2：Edge 作为 Enterprise 的子集**
- ❌ Edge 需要嵌入式存储，与 Enterprise 架构不兼容
- ❌ 部署和运维复杂度增加

## 后果

### 正面后果

1. Enterprise 可以专注企业需求，不受 Edge 限制
2. Edge 可以独立演进，不影响 Enterprise 稳定性
3. 共享契约减少重复开发
4. Edge 可以从 Enterprise 导出数据，支持离线场景

### 负面后果

1. Edge 需要单独维护嵌入式存储实现
2. 需要维护导入导出契约
3. Edge 功能受限于 Enterprise 契约

### 缓解措施

1. Edge 使用 Enterprise 已验证的契约和评测集
2. 导入导出采用签名包，确保数据完整性
3. Edge 不提供写操作，降低复杂度

## 遵守要求

后续所有工作包必须遵守：

1. **WP0-WP8**：专注 Enterprise Edition，不得引入 Edge 特定依赖（LanceDB、SQLite、ONNX）
2. **WP0**：在 ADR 中明确 Enterprise 默认不编译 Edge 依赖
3. **WP9**（可选）：仅在 WP8 通过且 GO-EDGE 批准后启动
4. **所有 WP**：MCP Schema 和领域契约必须保持一致

## 参考

- [DECISION-REVIEW.md](../esragpro-mcp/docs/DECISION-REVIEW.md) C-01, R-06
- [PRD.md](../esragpro-mcp/docs/PRD.md) 第 2 节
- [ARCHITECTURE.md](../esragpro-mcp/docs/ARCHITECTURE.md) 第 1 节

## 决策历史

- **2026-07-10**: 初始版本，由 WP0 创建