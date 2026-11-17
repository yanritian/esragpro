# Frontend 架构

## 定位

Frontend 是 Vue 3 + Vite 的旧管理界面，服务于文档管理、知识检索、知识图谱、评测和 Agentic Chat。它不是 Agent Memory Plane 的必要组成部分，也不是 TUI/MCP Agent 的运行时依赖。

## 结构

```text
frontend/src/
├── main.js              # Vue 应用入口
├── App.vue              # 根布局
├── api/index.js         # Axios API 客户端
├── router/index.js      # Vue Router
└── views/
    ├── Home.vue         # 首页/概览
    ├── Documents.vue    # 文档上传和列表
    ├── Search.vue       # 知识检索
    ├── KnowledgeGraph.vue # 图谱可视化
    ├── Evaluation.vue   # 评测界面
    └── AgenticChat.vue  # Agentic RAG 对话
```

## 与 Backend 的关系

Frontend 只通过 HTTP API 调用 Backend，未形成独立的 Memory SDK，也未承担 MCP 协议职责。它依赖的后端范围包括：

- `/api/v1/kb/*`
- `/api/v1/graph/*`
- `/api/v1/validate/*`
- `/api/v1/agentic/*`
- `/api/v1/auth/*`

新 `/api/v1/memory-plane/*` 尚未成为前端主要交互面。

## Frontend 去留决策

推荐采用两阶段决策：

1. **短期保留**：作为旧知识库管理、图谱检查、评测和运维观察面，避免在 Memory Core 未有替代运维面前删除诊断入口。
2. **核心收敛后再拆除**：当 MCP/TUI/API 已覆盖记忆写入、检索、证据审查、指标和生命周期操作，并且有 CLI smoke 与运维文档后，再从默认部署中移除 Frontend。

结论：Frontend 可以从未来的 Memory Plane 产品核心中去除，但当前仓库尚未执行删除；直接删除会同时删除现有管理和评测入口，不应与“记忆体不需要前端”混为一谈。
