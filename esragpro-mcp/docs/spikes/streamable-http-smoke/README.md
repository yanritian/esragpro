# MCP Streamable HTTP SDK Spike

## 目的

验证 Rust MCP SDK (rmcp) 的 Streamable HTTP 传输实现。

## 状态

⏳ 待验证

## 目标

1. 基于 modelcontextprotocol/rust-sdk 官方仓库
2. 验证 Streamable HTTP transport 支持
3. 实现最小 HTTP MCP server
4. 验证 OAuth/OIDC 集成
5. 记录编译命令和 Cargo.lock SHA-256
6. 验证与 MCP Inspector 兼容性

## 技术栈

- Rust: 1.96.0
- rmcp: 待验证
- Transport: Streamable HTTP
- HTTP Framework: 待确定（axum/tower/warp）

## Streamable HTTP 特性（MCP 2025-11-25）

1. 单一 `/mcp` 端点
2. 支持 POST/GET 方法
3. 协议版本头：`MCP-Protocol-Version: 2025-11-25`
4. 会话管理：`Mcp-Session-Id`（如果规范要求）
5. SSE 重连支持
6. 会话终止与取消

## 预期输出

- 最小可编译的 HTTP MCP server
- Cargo.lock 文件
- 编译日志
- 版本锁定文档
- OAuth 集成示例

## 验证方法

```bash
# 1. 确定依赖版本
cargo search rmcp
cargo search axum  # 或其他 HTTP framework

# 2. 创建项目
cargo new http-smoke --bin

# 3. 添加依赖
[dependencies]
rmcp = { version = "验证后填写", features = ["server", "transport-http"] }
axum = "验证后填写"
tokio = { version = "1", features = ["full"] }

# 4. 编译
cargo build --release

# 5. 启动服务
cargo run --release
# 应该在 http://localhost:3000/mcp 提供服务

# 6. 验证
curl -X POST http://localhost:3000/mcp \
  -H "MCP-Protocol-Version: 2025-11-25" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1}'
```

## OAuth 集成要求

1. 验证 JWT issuer, audience, expiry, signature
2. 从 JWT 提取 subject, tenant, scopes
3. 构造 PrincipalContext
4. 支持多种 IdP (企业 OIDC)

## 依赖约束

根据 ADR 0002，Enterprise 默认构建必须排除：
- LanceDB
- SQLite
- ONNX
- CUDA

允许：
- rmcp (MCP SDK)
- axum/tower (HTTP)
- reqwest (HTTP 客户端)
- schemars (JSON Schema)
- tokio (异步运行时)

## 安全检查

1. ✅ 验证 Origin header
2. ✅ 验证 Host header
3. ✅ 只在 HTTPS 后暴露
4. ✅ 请求体大小限制（1 MiB）
5. ✅ 超时控制（30s）
6. ✅ 速率限制（60 req/min per subject）

## 下一步

1. 运行 cargo search 确定版本
2. 创建最小 HTTP 实现
3. 测试 initialize/call 流程
4. 记录 Cargo.lock hash
5. 写入 ADR

## 参考

- [MCP 2025-11-25 Specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [modelcontextprotocol/rust-sdk](https://github.com/modelcontextprotocol/rust-sdk)
- [ADR 0002: MCP 协议、SDK 和认证](../../adr/0002-mcp-protocol-sdk-and-auth.md)