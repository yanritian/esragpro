# MCP Gateway 开发指南

## 前置条件

- Windows Rust stable MSVC toolchain。
- Cargo.lock 必须保持同步。
- Backend HTTP 端点准备好后才能做真实跨进程集成测试。

## 构建和测试

```powershell
cd D:\dingsun\esRAGPro\esragpro-mcp
cargo fmt --check
cargo check --locked --all-targets
cargo test --locked --all-targets
cargo clippy --locked --all-targets --all-features -- -D warnings
cargo build --locked --release
```

## 工具开发流程

1. 先在 `src/schema` 固定输入输出合同。
2. 再在 handler 中实现边界验证和错误映射。
3. 通过 Backend port/HTTP client 调用领域服务，不在 Gateway 新建数据真源。
4. 为 stdio、Streamable HTTP、session、timeout、大小限制和未知字段添加合同测试。
5. 最后用真实 MCP 客户端做协议 smoke，不以 Rust 单测代替客户端验收。

## 当前警告

新无权限 handler 中仍有未来 Backend HTTP 函数未被调用、schema re-export 未使用和局部变量可去除等编译警告；不影响当前测试通过，但生产前应清理。
