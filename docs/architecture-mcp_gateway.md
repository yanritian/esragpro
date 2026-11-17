# MCP Gateway 架构

## 定位

`esragpro-mcp` 是一个 Rust 实现的薄协议适配层，目标是让 Codex、Claude Code、Pi、TUI 或应用 Agent 通过 MCP 使用知识和记忆能力。它不应成为第二套企业数据真源，也不应在 Gateway 内复制 PostgreSQL、ES 或 Neo4j 业务逻辑。

## 运行模式

| 模式 | 用途 | 授权边界 |
|---|---|---|
| stdio/local | 本地 TUI、开发 Agent、Local Edge | Memory Plane 合同本身无 Principal；宿主进程负责隔离 |
| Streamable HTTP | 企业远程 Agent 或应用集成 | 保留传输层 OAuth/Principal/Session/Scope |
| embedded/local-edge | 未来边缘部署 | 当前只保留接口和功能开关，不作为企业默认真源 |

## 工具分层

### 无权限 Memory Tools

- `memory.remember`：保存带证据引用的记忆项。
- `memory.query`：按文本和数据维度查询记忆项。
- `memory.create_context`：把 supplied items 和 evidence 打包成 LLM-ready context。

P1-09 开始，这三个工具的生产路径通过 `EnterpriseHttpBackend` 调用 FastAPI
`POST /api/v1/memory-plane/{remember,query,context}`；FastAPI 侧使用
`PostgresMemoryRepository` 作为权威存储，并在同一事务中写入 P1-03
projection-outbox 行。`handlers_unpermissioned.rs` 中保留的进程内参考存储仅
服务于隔离的单元测试，不再被 MCP 生产路径调用。

### 传统受权限工具

- `kb.search_evidence`
- `kb.answer_with_evidence`
- `graph.lookup_entity`
- `graph.expand`
- `graph.find_paths`
- `graph.verify_claim`

这组工具依赖知识库、图谱和授权合同，不能因为无权限 Memory Tools 存在就全部去除。

### 管理/运维工具

MCP 还保留 ingestion、job、deletion、projection 和评测相关的管理合同；默认不应暴露破坏性操作，需通过审批、scope 和审计边界开放。

## 当前真实数据流

P1-09 之后，MCP Memory Tools 的生产链路为：

```text
MCP memory.remember / memory.query / memory.create_context
  -> McpServer (server.rs)
  -> EnterpriseHttpBackend::memory_* (backend/http.rs)
  -> FastAPI POST /api/v1/memory-plane/*
  -> PostgresMemoryRepository (+ P1-03 outbox)
```

`handlers_unpermissioned.rs` 中的进程内参考存储仅服务于隔离的单元测试
（`handlers_unpermissioned::tests::*`）。MCP 生产路径不再调用它。

## 当前质量证据

- `cargo test --locked --all-targets`：140 passed，1 failed；失败项为既有的 `test_session_ttl` 零毫秒边界断言。
- Schema、session、rate limit、OAuth、R4A/R4B 合同测试存在。
- `cargo fmt --check` 通过；`cargo clippy -- -D warnings` 当前被 25 个既有 dead-code 警告阻断。
- MCP Gateway 通过编译和单测，不代表 Claude Code、Codex、Pi 等真实客户端已完成端到端兼容验收。
