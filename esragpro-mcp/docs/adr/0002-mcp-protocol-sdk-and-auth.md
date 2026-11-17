# ADR 0002: MCP 协议、SDK 和认证

## 状态

已批准 (Approved)

## 上下文

esRAGPro MCP 需要选择 MCP 协议版本、Rust SDK、传输方式和认证机制。

原始方案（PRD.md E-02, E-06）使用旧式 HTTP+SSE，且未明确协议版本和 SDK 选择策略。

当前 Rust 骨架（esragpro-mcp/Cargo.toml）存在依赖问题：
- `ort = "^2"` 无法编译（只有 2.0.0-rc.* 候选版）
- 声明了 LanceDB、SQLite、ONNX 等嵌入式存储依赖
- 缺少 MCP SDK spike 验证

## 决策

### 1. 协议版本锁定

**目标协议**: MCP 2025-11-25（规划日最新正式版）

**版本策略**:
- WP0 必须：复核实施日最新正式版及 Claude Code/Qwen/Inspector 支持矩阵
- 候选版不得直接用于生产
- 破坏性升级必须有 ADR、双版本 contract tests 和客户端兼容门禁

**禁止**:
- 混用两版会话语义
- 使用未锁定的候选版特性

### 2. Rust SDK 选择

**必须**: 基于 MCP 官方 Rust SDK (modelcontextprotocol/rust-sdk)

**WP0 SDK spike 要求**:
1. 验证确切 rmcp 版本、MSRV、feature flags
2. 记录示例来源链接、编译命令、Cargo.lock SHA-256
3. 分别创建 stdio 和 Streamable HTTP spike
4. 确认与 Claude Code/Qwen/Inspector 兼容

**禁止**:
- 使用本计划、模型记忆或搜索摘要臆测版本号
- 在 WP0 spike 完成前写死 Cargo.toml 版本

### 3. 传输方式

**Enterprise Edition**:
- 主传输: Streamable HTTP（单一 `/mcp` 端点）
- 支持: POST/GET、协议版本头、Mcp-Session-Id（如规范要求）
- 重连: SSE 重连、会话终止与取消
- 禁止: 旧式独立 HTTP+SSE 作为默认传输

**Local Edge Edition**:
- 唯一传输: stdio
- 要求: stdout 只能输出 MCP JSON-RPC，日志必须写 stderr

### 4. 认证机制

**Enterprise (Streamable HTTP)**:
- 使用 OAuth 2.1 / OIDC
- 验证: issuer, audience, expiry, signature, scope
- 禁止: token passthrough（下游使用不同 audience 的短期凭据）
- 元数据: `/.well-known/oauth-protected-resource` 固定公布 8 个 Scope

**Local Edge (stdio)**:
- 凭据来自: 受控环境、系统密钥链或企业启动器
- 映射为 `local-stdio` PrincipalContext

**禁止**:
- 普通 bearer 的 jti 当作一次性请求 ID
- 声称"强防重放"但未使用 DPoP 或 mTLS（如需要，必须 ADR 验证）

### 5. 依赖清理

**Enterprise 默认构建禁止包含**:
- LanceDB、SQLite 图存储
- ONNX、CUDA、本地嵌入模型
- jieba 等中文分词（使用 ES analyzer）

**只允许**（通过 feature 或独立 crate）:
- reqwest（HTTP 客户端）
- rmcp（MCP SDK，经 spike 验证的版本）
- schemars（JSON Schema）
- tokio（异步运行时）

**WP0 spike 后**:
- 删除或 feature-gate 不需要的依赖
- 在 ADR 中锁定确切版本和 feature flags

## 理由

### 为什么选择此方案

1. **正式版优先**: 避免候选版的不稳定性
2. **官方 SDK**: 减少维护负担，保证兼容性
3. **传输清晰**: Enterprise 用 HTTP，Edge 用 stdio
4. **依赖最小**: Enterprise 不需要嵌入式存储

### 替代方案及为何拒绝

**替代方案 1：使用候选版 MCP**
- ❌ 不稳定，可能破坏性变更
- ❌ 客户端兼容性未知

**替代方案 2：自己实现 MCP 协议**
- ❌ 维护负担重
- ❌ 容易出现兼容性问题

**替代方案 3：Enterprise 包含嵌入式存储**
- ❌ 增加构建复杂度
- ❌ 性能和部署体积受影响
- ❌ 违反薄 Gateway 原则

## 后果

### 正面后果

1. 协议版本明确，避免混用
2. SDK 有官方支持，兼容性好
3. 依赖清晰，构建简单
4. 传输方式明确，易于测试

### 负面后果

1. 需要完成 SDK spike（耗时）
2. 协议升级需要 ADR 和兼容测试
3. 认证需要 OAuth 基础设施

### 缓解措施

1. WP0 创建详细 spike 文档
2. 提供 version negotiation 测试
3. 提供 OAuth 集成指南和示例

## 遵守要求

后续所有工作包必须遵守：

1. **WP0**: 完成 stdio 和 HTTP SDK spike，锁定版本
2. **WP6**: 实现基于 spike 的 Gateway
3. **WP7**: 实现 OAuth 认证和 Streamable HTTP
4. **所有 WP**: stdio stdout 只能输出 MCP 消息

## 参考

- [DECISION-REVIEW.md](../DECISION-REVIEW.md) C-06, E-04, E-07
- [ARCHITECTURE.md](../ARCHITECTURE.md) 第 6 节
- [MCP-V1-CAPABILITY-MATRIX.md](../MCP-V1-CAPABILITY-MATRIX.md)
- [PLAN.md](../PLAN.md) WP0, WP6, WP7

## 决策历史

- **2026-07-10**: 初始版本，由 WP0 创建