# Frontend 组件清单

## 视图组件

| 组件 | 用途 | 对应后端 |
|---|---|---|
| `Home.vue` | 项目首页/系统概览 | 健康、统计类 API |
| `Documents.vue` | 文档上传、列表和管理 | `/api/v1/kb/*` |
| `Search.vue` | 知识检索和答案展示 | `/api/v1/kb/search`、smart search |
| `KnowledgeGraph.vue` | 实体、关系和图谱可视化 | `/api/v1/graph/*` |
| `Evaluation.vue` | RAG 评测运行和报告 | `/api/v1/evaluation/*`、`/validate/*` |
| `AgenticChat.vue` | Agentic RAG 对话 | `/api/v1/agentic/*` |

## 基础组件和依赖

- Vue Router：路由组织。
- Pinia：前端状态管理依赖。
- Axios：API 调用。
- Element Plus：表单、布局和管理组件。
- vis-network：知识图谱可视化。
- Vite：开发服务器和生产构建。

## 当前缺失

- 没有独立 Memory Plane 操作台。
- 没有证据审查、Outbox 重放、投影滞后和记忆生命周期的可视化管理面。
- 没有 MCP/TUI 作为前端替代品的自动化验收入口。
