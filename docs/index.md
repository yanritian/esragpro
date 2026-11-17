# esRAGPro 项目文档索引

> 这是 Brownfield 项目的主入口，优先阅读本文件，再进入对应部分。
> 最近扫描：2026-07-22，Exhaustive Scan。
> 当前状态：Agent Memory Plane Phase 2-A 参考纵向链路已验证，真实持久化和生产化仍在后续阶段。

## 项目概览

- **类型：** Multi-part Backend + Web UI + CLI/MCP Gateway
- **主要语言：** Python、Rust、JavaScript/Vue、Node.js
- **核心演进：** 企业可信知识库 RAG → 通用 Agent Memory Plane
- **架构：** FastAPI 领域后端 + ES 混合检索 + PostgreSQL 权威模型 + Neo4j 图谱 + Rust MCP 适配器
- **前端决策：** Frontend 属于旧管理面，可在 API/TUI/MCP 运维能力成熟后从默认产品中移除；当前尚未删除

## 首先阅读

- [项目总览](./project-overview.md)：当前到底做什么、已完成什么、还有什么风险
- [集成架构](./integration-architecture.md)：Agent、MCP、Backend、Memory、数据库和 Frontend 的边界
- [Memory Plane 交付计划](./memory-plane-delivery-plan.md)：分阶段开发、测试、调度和验收流程
- [源码树分析](./source-tree-analysis.md)：目录、入口和关键集成点
- [多部分元数据](./project-parts.json)：机器可读的部分和接口关系

## 分部分架构

- [Backend 架构](./architecture-backend.md)
- [Frontend 架构](./architecture-frontend.md)
- [MCP Gateway 架构](./architecture-mcp_gateway.md)

## 分部分开发和组件

- [Backend 组件清单](./component-inventory-backend.md)
- [Frontend 组件清单](./component-inventory-frontend.md)
- [MCP Gateway 组件清单](./component-inventory-mcp_gateway.md)
- [Backend 开发指南](./development-guide-backend.md)
- [Frontend 开发指南](./development-guide-frontend.md)
- [MCP Gateway 开发指南](./development-guide-mcp_gateway.md)
- [部署指南](./deployment-guide.md)

## API 和数据

- [Backend API 合同](./api-contracts-backend.md)
- [Backend 数据模型](./data-models-backend.md)
- [Agent Memory Plane 原始架构说明](./agent-memory-plane.md)

## 现有项目文档

- [根 README](../README.md)
- [原有文档 README](./README.md)
- [MCP Agent Memory System 规划](../esragpro-mcp/docs/AGENT-MEMORY-SYSTEM.md)
- [MCP 架构基线](../esragpro-mcp/docs/ARCHITECTURE.md)
- [MCP 产品需求文档](../esragpro-mcp/docs/PRD.md)
- [方案决策审查](../esragpro-mcp/docs/DECISION-REVIEW.md)
- [执行状态历史](../esragpro-mcp/docs/EXECUTION-STATUS.md)
- [剩余工作计划](../esragpro-mcp/docs/REMAINING-WORK-PLAN-2026-07-14.md)
- [传统 RAG 与幻觉防护](../README.md#幻觉防护)
- [Backend 安装和测试](./development-guide-backend.md)
- [部署和安全注意事项](./deployment-guide.md)
- [MCP 安全与协议架构](../esragpro-mcp/docs/ARCHITECTURE.md)
- [Memory Plane 技术路线图](./memory-plane-delivery-plan.md)
- [产品介绍](./商业推广-产品介绍简版.md)
- [定价方案](./商业推广-定价方案.md)
- [技术文章](./商业推广-技术文章.md)

## 当前验证证据

- Python：Memory Plane 定向测试 → 57 passed。
- Rust：`cargo test --locked --all-targets` → 140 passed，1 failed；失败项为既有的 `test_session_ttl` 零毫秒边界断言。
- Rust 格式与静态检查：`cargo fmt --check` 通过；`cargo clippy -- -D warnings` 被 25 个既有 dead-code 警告阻断。
- Code graph：`graphify-out/graph.json` 已存在，本次使用查询追踪 Backend、Memory Core、GraphRAG、MCP 和测试关系。
- 扫描状态：见 [project-scan-report.json](./project-scan-report.json)。

## 动态世界记忆与执行治理

- [动态世界记忆愿景](./world-memory-vision.md)：产品定位、场景阶梯和对外口径
- [动态世界记忆架构](./world-memory-architecture.md)：三层平面隔离、数据模型、存储职责和接入边界
- [动态世界记忆路线图](./world-memory-roadmap.md)：P0-P9 阶段计划、验收门禁和原子提交
- [ADR 0005: 动态世界记忆边界](./adr/0005-dynamic-world-memory-boundary.md)：六类对象、职责边界和设计原则
- [ADR 0006: Codex 监督的交付流程](./adr/0006-codex-supervised-delivery.md)：角色职责、提交纪律和证据记录
- [Evidence 记录规范](../.agent-team/control-plane/evidence/README.md)：执行包的证据记录格式

## 使用本索引的方式

1. 计划新功能：先读项目总览、集成架构和交付计划。
2. 修改 Backend API：读 Backend 架构、API 合同、数据模型和开发指南。
3. 修改 MCP：读 MCP 架构、Backend API 合同和 MCP 开发指南。
4. 判断是否删除 Frontend：读 Frontend 架构、集成架构和部署指南，不要只看目录大小。
5. 进入 Agent/无人机应用：把本项目当作 Memory Plane，另行定义 Agent Plane 的计划、审批、调度和设备控制合同。
