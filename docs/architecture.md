# 系统架构（Architecture）

> 本文是全系统的一页式架构总览。分模块深挖见
> [world-memory-architecture.md](world-memory-architecture.md)（时序记忆）、
> [architecture-backend.md](architecture-backend.md)（后端）、
> [architecture-mcp_gateway.md](architecture-mcp_gateway.md)（Rust 网关）、
> [design-rationale.md](design-rationale.md)（为什么这样设计）。

---

## 1. 全景图

```
┌────────────────────────────────────────────────────────────────────────────┐
│                              客户端层                                       │
│   浏览器 (Vue 3 + Element Plus)   │   Claude Code / Codex / PI / Orca …     │
│        │ 人类操作界面              │        │ MCP 协议 (stdio 或 HTTP)        │
└────────┼──────────────────────────┘        ▼                             │
         │                          ┌──────────────────────────┐            │
         │                          │  Rust MCP Gateway        │            │
         │                          │  127.0.0.1:8421/mcp      │            │
         │                          │  · 静态令牌 / OIDC 双认证 │            │
         │                          │  · 速率限制 + 审计日志    │            │
         │                          │  · 协议翻译 Anthropic ↔  │            │
         │                          │    OpenAI ↔ Gemini       │            │
         │                          └──────────┬───────────────┘            │
         ▼                                     │ HTTP (回环)                 │
┌──────────────────────────────────────────────▼───────────────────────────┐ │
│                        FastAPI 后端  (127.0.0.1:8000)                     │ │
│                                                                           │ │
│  ┌─────────────┐  ┌────────────────────────────────────────────────────┐ │ │
│  │ API 路由层   │  │                 服务层                              │ │ │
│  │ /auth       │  │  rag.py ──────────► 混合检索编排                     │ │ │
│  │ /kb         │◄─┤  hallucination_guard ► 三层幻觉防护                  │ │ │
│  │ /agentic    │  │  knowledge_graph ──► 图谱服务                       │ │ │
│  │ /evaluation │  │  smart_retrieval ──► 检索策略选择                    │ │ │
│  │ /graph      │  │  reranker ─────────► 重排                           │ │ │
│  │ /validate   │  │  evidence_packet ──► 证据包组装                     │ │ │
│  │ /memory-plane  │  refusal_service ──► 拒答与置信度                 │ │ │
│  └─────────────┘  └────────────────────────────────────────────────────┘ │ │
└───────────────┬───────────────────────────────────────────────────────────┘ │
                │                                                             │
     ┌──────────┼──────────────┬──────────────┬──────────────┐               │
     ▼          ▼              ▼              ▼              ▼               │
┌─────────┐ ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐            │
│  ES 8   │ │ Postgres│   │  Redis  │   │  MinIO  │   │  Neo4j  │            │
│ 混合检索 │ │ 记忆权威 │   │  热状态  │   │  证据blob│   │  知识图谱│            │
│ +向量+RRF│ │ +outbox │   │         │   │         │   │         │            │
└─────────┘ └────┬────┘   └─────────┘   └─────────┘   └─────────┘            │
                 │ outbox 事件溯源                                            │
                 ▼                                                            │
┌────────────────────────────────────────────┐                               │
│        投影 Workers（事件驱动派生）           │                               │
│  minio-worker ──► 证据文件入 MinIO           │                               │
│  ES 投影 ──────► 记忆项可检索化               │                               │
│  Neo4j 投影 ───► 实体/关系入图               │                               │
│  (事务性 outbox 保证最终一致)                 │                               │
└────────────────────────────────────────────┘───────────────────────────────┘
```

---

## 2. 检索管线（Retrieval Pipeline）

一次 `/api/v1/kb/search` 的完整路径：

```
用户问题
   │
   ├─► [1] 查询理解与策略选择（smart_retrieval / agentic_rag）
   │        决定：关键词为主？向量为主？是否需要图多跳？
   │
   ├─► [2] 双路并行检索
   │        BM25（ES 全文）  ──┐
   │        kNN（ES 向量）  ──┤
   │                          ▼
   │        [3] RRF 融合:  score(d) = Σ 1/(k + rank_i(d)),  k=60
   │                          │
   │        [4] 重排（reranker，可选）
   │                          │
   ├─► [5] 幻觉防护第一层：安全提示词
   │        系统提示注入"必须基于文档、禁止编造、不确定时声明"
   │                          │
   ├─► [6] LLM 生成（流式）
   │                          │
   ├─► [7] 幻觉防护第二层：事实核查
   │        逐声明拆解 → 每条声明在证据集中验证支撑度
   │                          │
   └─► [8] 幻觉防护第三层：拒绝/降级
            无证据 → 拒绝回答；证据不足 → 置信度降级 + 警告标注
            输出：答案 + 逐条来源引用 + 置信度 (0-1)
```

**为什么 RRF 而不是加权线性融合**：BM25 分数与余弦相似度分数量纲完全不同，
任何固定权重都需要按语料调参；RRF 只消费排名，天然免调参、跨语料稳定。

---

## 3. 记忆平面（Memory Plane）

记忆子系统是 esRAGPro 区别于普通 RAG 框架的核心。它是一个**事件溯源 +
多存储投影**的架构，而不是"一张表存对话"。

### 3.1 写路径（Write Path）

```
POST /memory-plane/remember
        │
        ▼
  PostgreSQL memory_items（权威存储，唯一事实来源）
        │
        ├──► 事务性 outbox 事件（同一事务内写入，永不丢失）
        │
        ▼  投影 Worker（异步、最终一致）
  ┌───────────┬───────────────┬──────────────┐
  │  MinIO    │  Elasticsearch │  Redis 热状态 │
  │  证据 blob │  检索投影       │  高频热键     │
  └───────────┴───────────────┴──────────────┘
```

关键性质：
- **权威单源**：PostgreSQL 是唯一事实来源，所有派生存储可随时重建
- **事务性 outbox**：记忆写入与事件写入在同一事务提交，投影崩溃不丢数据
- **最终一致**：投影延迟秒级，查询可容忍（记忆检索不需要毫秒级新鲜度）

### 3.2 时序世界记忆（World Memory）

对话记忆的进阶形态——不仅"记了什么"，还回答"**什么时候是真实的**"：

| 概念 | 说明 |
|------|------|
| 不可变事件史 | 追加式事件日志（append-only），带 event-time 与 captured-time 双时钟 |
| 有效期状态快照 | valid-time 状态快照：每个实体在任意历史时刻的状态可精确重放 |
| 事件时间截断 | 查询可指定"截至 T 时刻的世界状态"，拒绝时间穿越答案 |
| 决策核 + 回放 | 决策过程可序列化（sealed replay dataset），带质量门，可复现审计 |

这是"组织级记忆"的语义：员工 A 在 3 月改了制度，6 月又改回来——
查询"5 月的制度"必须返回 3 月版本，而不是最新版本。普通 RAG 做不到这一点。

### 3.3 Agent 对话自动入库

```
Claude Code ~/.claude/projects/**/*.jsonl  ─┐
Codex      ~/.codex/sessions/**/*.jsonl    ─┼─► 采集器（增量 tail，字节偏移 state）
PI         ~/.pi/agent/sessions/**         ─┘         │
                                                      ▼
                                        POST /memory-plane/remember
                                        （metadata 标注 agent/project/session）
```

- 字节偏移增量：只读新增，永不重复
- 后端不可用时**偏移不推进**——零丢失，恢复后自动补齐
- 单次限流 100 条，防止首次运行洪水

---

## 4. Rust MCP 网关

`esragpro-mcp/` 是一个独立的 Rust 二进制（约 9.6MB），把 esRAGPro 的能力
以 MCP 协议暴露给任意 Agent。

| 设计点 | 实现 |
|--------|------|
| 双传输 | stdio（每 Agent 一进程）与 Streamable HTTP（多 Agent 单实例） |
| 协议翻译 | 网关内部把 Anthropic Messages / OpenAI Chat / Gemini 语义互译，Agent 不被供应商 API 绑定 |
| 认证双模式 | 生产：OIDC RS256 JWT（kid/JWKS/iss/aud 全校验）；本机：静态共享令牌（恒时比较） |
| 限流 | 按 subject 的 RPM + 并发双限流 |
| 审计 | 每次工具调用结构化审计日志 |
| 工具集 | kb.search_evidence / kb.answer_with_evidence / graph.lookup_entity / graph.expand / graph.find_paths / graph.verify_claim |

**为什么用 Rust 写网关**：它是唯一直接面对不可信输入的网络组件；内存安全、
单文件分发（9.6MB 无运行时依赖）、以及多协议流式翻译的确定性延迟都指向 Rust。
业务编排留在 Python——两边各干各自擅长的事（见 [design-rationale.md](design-rationale.md)）。

---

## 5. 部署拓扑与端口

| 端口 | 绑定 | 服务 | 说明 |
|------|------|------|------|
| 80 | 0.0.0.0 | frontend (nginx) | 唯一公网入口 |
| 127.0.0.1:8000 | 回环 | backend | API；网关与采集器走此入口 |
| 9200 / 5432 / 6379 / 7474 / 7687 / 9000 | 容器网络内 | ES/PG/Redis/Neo4j/MinIO | 不对宿主暴露（compose 默认），调试时可临时映射 |
| 127.0.0.1:8421 | 回环 | MCP Gateway | Agent MCP 入口 |

**回环绑定原则**：所有内部服务的宿主暴露一律绑 127.0.0.1——网关与采集器
是本机进程，走回环即可；对外只留 nginx 一条路（安全版 compose 里是 18083/18446）。
