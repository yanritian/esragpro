# MCP Gateway 组件清单

## 协议层

- `server.rs`：rmcp Server、Tools、Resources、Prompts、生命周期。
- `schema/*.rs`：知识库、图谱、管理、Principal、资源、Prompt、无权限 Memory schema。
- `http.rs`：HTTP transport 边界。
- `session_store.rs`：会话绑定、TTL 和状态。
- `size_limits.rs`、`whitelist.rs`：输出大小和 DTO 字段白名单。

## 处理层

- `handlers.rs`：受权限知识/图谱工具。
- `handlers_unpermissioned.rs`：`memory.remember/query/create_context` 参考处理器。
- `backend/http.rs`：未来调用 Backend HTTP 的边界。
- `backend/embedded.rs`：Local Edge 预留边界。

## 内部能力层

- `engine/fulltext.rs`、`vector.rs`、`rrf.rs`：检索引擎抽象。
- `engine/graph_query.rs`、`reasoning.rs`：图查询和推理预留。
- `extract/*`：文档、chunk、实体、关系抽取。
- `guard/*`：Prompt、事实核查和拒答。
- `audit.rs`、`eval.rs`、`oauth.rs`、`rate_limit.rs`：审计、评测、OAuth、限流。

## 约束

MCP 必须保持薄：协议合同、输入验证、错误和审计在 Gateway；事实、存储、索引、图谱一致性在 Backend/Memory Core。不能在 Gateway 内创建第二套 Enterprise 真源。
