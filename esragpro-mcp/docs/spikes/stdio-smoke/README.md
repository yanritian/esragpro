# MCP stdio SDK Spike

## 目的

验证 Rust MCP SDK (rmcp) 的 stdio 传输实现。

## 状态

⏳ 待验证

## 目标

1. 基于 modelcontextprotocol/rust-sdk 官方仓库
2. 验证确切的 rmcp 版本和 feature flags
3. 实现最小 stdio MCP server
4. 记录编译命令和 Cargo.lock SHA-256
5. 验证与 Claude Code/Qwen 兼容性

## 技术栈

- Rust: 1.96.0
- rmcp: 待验证（通过 cargo search 或官方文档确定）
- Transport: stdio

## 预期输出

- 最小可编译的 stdio MCP server
- Cargo.lock 文件
- 编译日志
- 版本锁定文档

## 验证方法

```bash
# 1. 检查 rmcp 版本
cargo search rmcp

# 2. 创建最小项目
cargo new stdio-smoke --bin

# 3. 添加依赖
cd stdio-smoke
echo '[dependencies]
rmcp = { version = "验证后填写", features = ["server"] }' >> Cargo.toml

# 4. 编译
cargo build --release

# 5. 验证 stdout 输出
cargo run --release < test_input.json
```

## 依赖约束

根据 ADR 0002，Enterprise 默认构建必须排除：
- LanceDB
- SQLite
- ONNX
- CUDA
- 嵌入式模型

只允许：
- rmcp (MCP SDK)
- reqwest (HTTP 客户端)
- schemars (JSON Schema)
- tokio (异步运行时)

## 下一步

1. 运行 cargo search rmcp 确定版本
2. 创建最小实现
3. 记录 Cargo.lock hash
4. 写入 ADR

## 参考

- [MCP 2025-11-25 Specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [modelcontextprotocol/rust-sdk](https://github.com/modelcontextprotocol/rust-sdk)
- [ADR 0002: MCP 协议、SDK 和认证](../../adr/0002-mcp-protocol-sdk-and-auth.md)