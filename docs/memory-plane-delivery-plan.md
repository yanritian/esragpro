# Agent Memory Plane 完整交付计划

## 目标

把当前“进程内参考存储的 remember/query/context 纵向切片”升级为可被 Codex、Claude Code、Pi、TUI 和应用 Agent 复用的通用记忆组件：具备实时热状态、历史权威记录、混合检索、图关系、证据上下文、可重放同步和可验证质量门禁。

## 阶段计划

### P0：边界冻结和基线（当前）

交付：

- 固定 Memory Plane 与 Agent Plane 的职责边界。
- 固定 `MemoryItem`、`EvidenceRef`、`ContextPack` 和 Query 状态合同。
- 固定无权限本地调用与企业远程调用的安全边界。
- 标记所有 mock/stub，不再把历史“完成”文档当作生产证据。
- 建立 Rust、Python、Compose、MCP 客户端的验收命令。

门禁：合同测试通过；当前目标范围和非目标写入 ADR/PRD；工作区变更可追踪。

### P1：真实权威存储和投影

顺序：PostgreSQL → Redis → Elasticsearch → Neo4j → MinIO。

- PostgreSQL：记忆、观察、证据、版本、生命周期审计和 Outbox。
- Redis：session/hot state、TTL、短期上下文和查询缓存。
- Elasticsearch：BM25、向量、metadata filter、RRF、时间新鲜度。
- Neo4j：实体、关系、Claim、Evidence 和时间边。
- MinIO/Blob：原始证据和稳定 source reference。

门禁：进程重启后数据仍存在；跨进程 MCP 与 Backend 能读到同一条记忆；投影失败可重试和回放；删除传播可审计。

### P2：实时摄取和 Outbox Worker

- 将 Observation 写入变成 PostgreSQL 事务和 Outbox 原子操作。
- 实现 worker、checkpoint、retry、DLQ、replay 和 projection lag。
- 接入对话、工具调用、企业文档、Webhook、设备遥测等输入适配器。
- 保持 DLP、consent、稳定 ID、幂等和时间有效性。

门禁：同一事件重复投递不产生重复记忆；worker 崩溃可恢复；删除/撤销事件最终到达所有投影；新鲜度指标可观测。

### P3：混合检索和图增强

- Query Router 根据查询意图选择热状态、历史、语义、关键词和图检索。
- 并行召回，再进行 RRF/重排、token budget 和证据去重。
- 把图查询从当前降级实现接到真实 Neo4j projection。
- Claim 必须区分 extracted、reviewed、approved、revoked、superseded。
- 证据不足时返回拒答，不让图路径或相似度直接成为事实。

门禁：检索结果可追踪到 source/chunk/version；图扩展有关系类型和时间边；冲突查询能返回冲突证据和状态。

### P4：MCP 跨进程和客户端兼容

- Rust `memory.*` 通过 Backend HTTP 调用真实 Memory Plane。
- stdio 先完成 Codex/Claude/Pi/TUI smoke。
- Streamable HTTP 再完成 session、OAuth、Principal、scope、超时和取消。
- MCP 输入输出合同、错误码、大小限制和字段白名单保持冻结。
- 任何管理/破坏性工具默认关闭，采用审批和审计。

门禁：同一 remember 由 MCP 写入后，另一进程 query 可读；真实客户端至少完成一套 stdio 和一套 HTTP 合同测试。

### P5：质量、性能和发布门禁

建立固定 holdout 数据集和可复现报告：

| 指标 | 目标定义 |
|---|---|
| Evidence coverage | 所有 `trusted` 结果 100% 有稳定证据引用 |
| Refusal correctness | 应拒答问题不生成无证据事实；报告 precision/recall |
| Freshness | 实时事件进入 hot path 的 P95 延迟可测 |
| Hot query latency | 不含 LLM 生成，P50/P95/P99 分别记录，目标 P99 ≤ 100ms |
| Retrieval quality | BM25、vector、RRF、graph 各自和融合后的 recall/nDCG |
| Idempotency | 相同 event/query 在固定数据版本下结果稳定 |
| Projection health | ES/Neo4j/Redis lag、失败、DLQ、重放成功率 |

“0 幻觉”对外应表述为“证据约束和无证据拒答”，不能表述为对任意 LLM 输出的数学保证。

### P6：企业发布和无人机参考场景

- 企业模式：在 Agent Runtime/Gateway 外层接入租户和权限，不把无权限合同暴露给跨租户网络。
- Local Edge：允许本地 stdio、离线模型和有限缓存，但仍需数据版本和证据状态。
- 无人机救援：Memory Plane 只存遥测、地图、历史救援记录、风险事实、无人机状态和证据；Agent Plane 负责搜索规划、队形调度、人工审批、设备动作和安全策略。
- 每个动作结果重新写回 Memory，形成可追溯闭环。

## 调度流程

```text
Codex 负责人
  1. 读取项目图、Git 和当前门禁
  2. 拆分单一有界任务
  3. 交给 Claude Code/Qwen 执行
  4. 要求提交实现、测试、迁移、回滚说明
  5. Codex 做代码图影响分析和 adversarial review
  6. 运行 Python/Rust/Compose/MCP 质量门禁
  7. 观察真实运行链路，再进入下一阶段
```

每个任务必须包含：目标、禁止越界、输入输出合同、涉及文件、测试命令、验收指标、回滚策略和证据产物。Agent 不能递归调度其他 Agent；调度权属于 Codex 控制平面。

## 第一条应执行的生产化任务

优先实现 P1 的 PostgreSQL Memory Repository 和 Outbox 持久化，然后接通 FastAPI 与 Rust MCP 的跨进程 remember/query。不要先删除 Frontend，也不要先重写 Rust 领域核心；先让记忆真源和真实读写链成立。
