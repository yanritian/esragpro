# Backend API 合同目录

> 默认 API 前缀：`/api/v1`。以下是从当前 FastAPI 路由实际扫描出的合同目录；详细 Pydantic 字段以对应源文件为准。

## 路由总览

| 路由组 | 主要端点 | 用途 | 授权状态 |
|---|---|---|---|
| `auth` | `POST /auth/register`、`POST /auth/login`、`GET /auth/me`、`POST /auth/create-admin` | 身份和用户 | 认证链 |
| `kb` | `POST /kb/upload`、`POST /kb/search`、`POST /kb/search/raw`、`GET /kb/documents`、`DELETE /kb/documents/{doc_id}` | 企业知识库 | 企业授权 |
| `graph` | `GET /graph/entities`、`GET /graph/entities/{name}`、`PUT /graph/entities`、关系/子图/健康/质量接口 | 图谱管理和查询 | 企业授权/管理边界 |
| `agentic` | `POST /agentic/ask`、`GET /agentic/strategy/{query}` | Agentic RAG | 传统应用边界 |
| `memory` | `/memory/sessions*` | 会话、工作记忆、上下文 | 传统后端会话边界 |
| `memory-plane` | `POST /memory-plane/remember`、`POST /memory-plane/query`、`POST /memory-plane/context` | 通用 Agent Memory | 当前无 Principal/ACL，数据维度仅用于范围过滤 |
| `smart-search` | `POST /smart-search`、`GET /smart-search/stats` | 智能检索 | 传统应用边界 |
| `validate` | `POST /validate/run`、`GET /validate/quick`、`POST /validate/compare` | RAG 质量验证 | 运维/评测 |
| `evaluation` | `/evaluation/test-cases*`、`/evaluation/run*`、`/evaluation/report*` | 评测和报告 | 运维/评测 |
| `tasks` | `/tasks*`、`/tasks/steps`、`/tasks/results` | 任务、步骤、结果记录 | Agent/应用边界，不是 Memory 执行器 |

## Memory Plane 合同

### `POST /api/v1/memory-plane/remember`

输入包含记忆内容、证据引用、来源/溯源信息和可选数据维度：`project_id`、`session_id`、`agent_id`、`knowledge_base_id`。返回生成的 Memory Item 和证据引用。

约束：

- 不要求 Principal、OAuth 或 scope。
- 证据字段必须能稳定指向来源。
- 数据维度只做组织和查询过滤。
- P1-09：通过 `PostgresMemoryRepository` 原子写入记忆、证据和 P1-03
  projection-outbox 行。

### `POST /api/v1/memory-plane/query`

输入包含自然语言查询、最大条数、token budget、证据要求、最低置信度和数据维度。返回候选记忆、证据、状态和查询耗时信息。

约束：

- 无证据时必须返回 refused/no-match/insufficient-evidence 类状态。
- 不得把空结果改写为模型猜测。
- P1-09：从 PostgreSQL 权威存储读取，跨进程一致。

### `POST /api/v1/memory-plane/context`

将调用方提供的记忆项和证据整理为 LLM-ready Context Pack。该接口负责上下文结构和证据引用，不负责调用 LLM，也不负责生成答案。

## MCP 对应合同

| MCP Tool | 对应能力 | 当前实现 |
|---|---|---|
| `memory.remember` | 写入记忆和证据 | FastAPI `POST /memory-plane/remember` → PostgreSQL（P1-09） |
| `memory.query` | 查询记忆 | FastAPI `POST /memory-plane/query` → PostgreSQL（P1-09） |
| `memory.create_context` | 创建证据上下文 | FastAPI `POST /memory-plane/context`（P1-09） |
| `kb.search_evidence` | 企业知识证据检索 | 受权限工具 |
| `kb.answer_with_evidence` | 证据约束回答 | 受权限工具 |
| `graph.lookup_entity` / `expand` / `find_paths` | 图谱查询 | 受权限工具/领域边界 |
| `graph.verify_claim` | 声明验证 | 受权限工具/证据边界 |
