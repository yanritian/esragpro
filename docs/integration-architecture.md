# 集成架构与数据流

## 当前系统

```mermaid
flowchart TB
    subgraph AgentClients["Agent 客户端"]
      C1["Codex / Claude Code"]
      C2["Pi / TUI / 应用 Agent"]
    end
    subgraph DevPlane["开发调度平面，不是运行时"]
      Org["Codex Agent Org"]
      Claude["Claude Code / Qwen 执行"]
    end
    subgraph Runtime["产品运行时"]
      MCP["Rust MCP Gateway"]
      API["FastAPI Backend"]
      RAG["传统 RAG / Graph / Evidence"]
      Mem["Memory Core"]
      Ref["进程内参考 Repository"]
      ES["Elasticsearch"]
      PG["PostgreSQL"]
      Redis["Redis"]
      Neo["Neo4j"]
    end
    UI["Vue Frontend 旧管理面"]

    Org --> Claude
    C1 --> MCP
    C2 --> MCP
    MCP --> API
    UI --> API
    API --> RAG
    API --> Mem
    Mem --> Ref
    RAG --> ES
    RAG -. 部分图谱路径 .-> Neo
    API -. 传统企业模型 .-> PG
    API -. 配置存在，Memory 热状态尚未接通 .-> Redis
```

## 目标系统

```mermaid
flowchart LR
    Agent["Agent/TUI/Application"] --> MCP["MCP Gateway"]
    MCP --> API["Memory Plane API"]
    API --> Router["Query Router"]
    Router --> Hot["Redis hot state"]
    Router --> History["PostgreSQL history/source"]
    Router --> Hybrid["ES BM25/vector/RRF"]
    Router --> Graph["Neo4j temporal graph"]
    History --> Project["Outbox + projection worker"]
    Project --> Hybrid
    Project --> Graph
    Evidence["Original source / MinIO"] --> API
    Hybrid --> Context["Evidence Context Pack"]
    Graph --> Context
    Hot --> Context
    History --> Context
```

## 集成边界

| 边界 | 当前负责人 | 不能越界的内容 |
|---|---|---|
| Agent → MCP | Agent Runtime / MCP | MCP 不负责 Agent 规划和设备动作 |
| MCP → Backend | Gateway | Gateway 不复制企业数据真源 |
| Backend → Memory Core | Backend | Memory 返回事实、状态、证据和拒答，不执行任务 |
| Memory Core → Stores | 持久化/投影 worker | PostgreSQL 是权威记录，ES/Neo4j/Redis 是投影或缓存 |
| Memory → Agent Plane | Agent Runtime | Agent 自己决定推理、计划、人工审批和调度 |
| Frontend → Backend | 旧管理面 | Frontend 不应成为 Memory Plane 必需依赖 |
| Codex Agent Org → Claude/Qwen | 开发控制平面 | 不得把开发调度状态当成无人机/企业 Agent 运行状态 |

## 故障与一致性策略

1. 权威记忆写入成功后，必须产生可重放的 Outbox 事件。
2. ES、Neo4j、Redis 投影失败时，查询要返回 freshness/projection 状态，而不是静默当作最新事实。
3. 删除、撤销和法律保留必须传播到所有投影，并保留审计事件。
4. 图谱关系和模型抽取结果只能作为候选或检索线索，经过证据与审核后才成为可发布事实。
5. 没有足够证据时，Memory 返回 `insufficient_evidence`，不生成猜测。
