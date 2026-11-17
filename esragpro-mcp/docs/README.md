# esRAGPro MCP 规划文档索引

本目录是 Enterprise Knowledge Core、知识图谱增强检索与 MCP Gateway 的实施事实源。

当前状态：**设计与实施规划阶段，生产实现尚未开始。** `src/` 中的 Rust 文件是早期骨架，不能视为已完成功能。

## 推荐阅读顺序

1. [`DECISION-REVIEW.md`](DECISION-REVIEW.md)：原方案审计、保留项、修正项和否决项。
2. [`PRD.md`](PRD.md)：产品边界、Enterprise/Local Edge 两种交付形态和验收标准。
3. [`ARCHITECTURE.md`](ARCHITECTURE.md)：系统记录、投影、信任边界、数据契约和 MCP 协议设计。
4. [`MCP-V1-CAPABILITY-MATRIX.md`](MCP-V1-CAPABILITY-MATRIX.md)：MCP 的声明能力、显式不支持项、数量、协议和验收矩阵。
5. [`RETRIEVAL-DESIGN.md`](RETRIEVAL-DESIGN.md)：混合召回、条件图检索、重排、证据包与回答防护。
6. [`PLAN.md`](PLAN.md)：按门禁拆分的 WP0-WP9（含 WP7A 管理 Profile）实施计划。
7. [`QWEN3.7-PLUS-EXECUTION-PROMPTS.md`](QWEN3.7-PLUS-EXECUTION-PROMPTS.md)：Claude Code + Qwen3.7 Plus 总控、分阶段、审查和续跑提示词。
8. [`EXECUTION-STATUS.md`](EXECUTION-STATUS.md)：append-only 的跨会话执行、门禁与审查状态。

## 已冻结的核心决策

- MCP 是安全协议适配层，不是知识库或数据真源。
- Enterprise Edition 复用并修复 `D:\dingsun\esRAGPro`：PostgreSQL/对象存储是系统记录，Elasticsearch 和 Neo4j 是可重建投影。
- Rust `esragpro-mcp` 在 Enterprise 默认路径中只实现薄 Gateway，不重复实现检索、图谱和事实核验。
- Local Edge 是后续可选后端，不能阻塞 Enterprise 交付，也不能被宣传为多租户企业版。
- 文档证据、approved ClaimCandidate 发布出的 active Claim、身份/ACL、模型与索引版本必须贯穿摄取、检索、回答和删除链路。
- 质量和性能结论必须来自版本化黄金集，禁止使用硬编码或未经验证的宣传数字。

## 执行方式

每次只执行 `PLAN.md` 的一个工作包。启动 Claude Code 后，先粘贴提示词包中的“总控提示词”，完成预检后再粘贴对应 WP 提示词。工作包结束必须运行“交付审查提示词”；存在 P0/P1 发现时不得进入下一工作包。

执行代理应把每次状态追加到 `EXECUTION-STATUS.md`。当前文件只声明 `not_started`；不得把规划内容写成已完成功能，也不得覆盖历史运行记录。
