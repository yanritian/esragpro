<div align="center">

# esRAGPro

**企业可信知识库 · 可信检索 + 时序记忆 + MCP 网关，三合一**

基于 Elasticsearch 混合检索（BM25 + 向量 + RRF）与三层幻觉防护，
附带一套事件溯源的 Agent 时序记忆平面和 Rust MCP 网关。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](backend/)
[![Rust](https://img.shields.io/badge/MCP%20Gateway-Rust-orange.svg)](esragpro-mcp/)
[![Docker](https://img.shields.io/badge/Docker-8%20services-blue.svg)](docker-compose.yml)
[![Vue 3](https://img.shields.io/badge/Frontend-Vue%203-green.svg)](frontend/)

</div>

---

## 这是什么

一个回答**每个结论都有出处、超出范围就承认不知道**的企业知识库，
外加一层**知道"什么时候是真实的"**的 Agent 时序记忆系统——
全部能力通过一个 9.6MB 的 Rust MCP 网关暴露给你手上的每一个编码 Agent。

三个子系统，对应市面上开源方案各自只做一半的三件事：

| 子系统 | 对标 | esRAGPro 的差异点 |
|--------|------|------------------|
| 可信知识库 | RAGFlow / Dify / FastGPT | 三层幻觉防护（安全提示词 → 逐声明事实核查 → 拒答降级）作为一等子系统，而非"引用来源" |
| 时序记忆平面 | Mem0 / Zep / Letta | 事务性 outbox → 多存储投影（PG 权威 + Redis 热 + ES + Neo4j + MinIO），有效期状态快照 + 事件史 + 决策回放 |
| MCP 网关 | 各类 MCP 代理 | Rust 单二进制，多协议翻译（Anthropic ↔ OpenAI ↔ Gemini），OAuth / 静态令牌双认证，限流 + 审计 |

## 核心特性

- **混合检索**：BM25 + 向量 + RRF 融合（k=60），可插拔重排
- **三层幻觉防护**：安全提示词 → 逐声明事实核查 → 无证据拒答（fail-closed）
- **答案可追溯**：逐条来源引用 + 置信度标注 (0-1) + 证据包
- **智能拒答**：超出文档范围时主动拒绝，不编造
- **时序世界记忆**：不可变事件史 + 有效期状态快照——"5 月的制度"不会返回 6 月的版本
- **Agent 记忆入库**：自动采集 Claude Code / Codex / PI 的对话进记忆平面（增量、零丢失）
- **知识图谱**：Neo4j 实体/关系/多跳查询/实体合并/质量评分
- **MCP 网关**：Rust 单二进制（9.6MB），stdio + Streamable HTTP，OAuth / 限流 / 审计
- **四模式嵌入**：Ollama（推荐）/ 通义 API / 智谱 API / 本地 torch，可插拔不锁死
- **私有部署**：全栈 Docker，数据不出企业

## 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                    客户端层                                          │
│   浏览器 (Vue 3)          │   Claude Code / Codex / PI / Orca …      │
└──────────┬────────────────┴──────────┬──────────────────────────────┘
           ▼                           ▼
┌────────────────────┐    ┌───────────────────────────────┐
│  Nginx 前端入口      │    │  Rust MCP Gateway (9.6MB)      │
│  http://localhost   │    │  127.0.0.1:8421/mcp           │
└──────────┬─────────┘    │  静态令牌/OIDC · 限流 · 审计    │
           ▼              └──────────┬────────────────────┘
┌─────────────────────────────────────▼──────────────────────────────┐
│                     FastAPI 后端 :8000                              │
│   混合检索(BM25+向量+RRF) ─► 三层幻觉防护 ─► LLM 生成 ─► 证据包       │
│   + 记忆平面 (remember / query / context)                           │
└──────┬──────────┬──────────┬──────────┬─────────────────────────────┘
       ▼          ▼          ▼          ▼
   ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐     投影 Workers（事件溯源）
   │  ES  │  │  PG  │  │ Redis│  │ Neo4j│  ◄── outbox → ES/Neo4j/MinIO
   └──────┘  └──────┘  └──────┘  └──────┘
```

完整架构说明（含记忆平面写路径、时序世界记忆、协议翻译细节）见
[docs/architecture.md](docs/architecture.md)。

## 快速开始

```bash
git clone https://github.com/yanritian/esragpro.git
cd esragpro
cp .env.example .env        # 填入 MinIO 凭据 / LLM Key / 嵌入模式

docker compose up -d --build
docker exec esragpro-backend alembic upgrade head   # 首次部署执行一次
```

访问：前端 http://localhost · API 文档 http://127.0.0.1:8000/docs

向量嵌入推荐接本地 Ollama（数据不出本机）：

```bash
ollama pull bge-m3
# .env: EMBEDDING_PROVIDER=ollama
```

完整步骤（含四种嵌入模式对比、MCP 接入每个 Agent、对话采集器、故障排查）见
**[docs/INSTALL.md](docs/INSTALL.md)**。

## 把 esRAGPro 接进你的 Agent

构建 Rust 网关后，任何 MCP 客户端一行接入：

```json
{ "mcpServers": {
    "esragpro": { "url": "http://127.0.0.1:8421/mcp",
                  "headers": { "Authorization": "Bearer <token>" } } } }
```

Agent 即获得 6 个工具：`kb.search_evidence` / `kb.answer_with_evidence` /
`graph.lookup_entity` / `graph.expand` / `graph.find_paths` / `graph.verify_claim`。
详细步骤（Claude Code / Codex / PI / WorkBuddy / Orca）见
[docs/INSTALL.md §3](docs/INSTALL.md)。

## 实测数据

| 指标 | 数字 | 环境 |
|---|---|---|
| MCP 网关二进制 | 9.6 MB（Rust 单文件） | `cargo build --release --features http` |
| 瘦身后端镜像 | ~600 MB（torch 解耦前 ~1.3GB） | python:3.12-slim + CPU 推理 |
| 本地嵌入（bge-m3） | 1024 维 · 显存 664MB · 峰温 77°C | Ollama，GTX 1060 笔记本 |
| Agent 记忆入库 | 106 条真实对话，零丢失 | 三源增量采集 |
| 幻觉率 | 3-5%（目标） | ⚠️ 公开基准待跑 |

更多实测与诚实的差距清单见 **[docs/RESULTS.md](docs/RESULTS.md)**。

## 文档

| 文档 | 内容 |
|------|------|
| [docs/INSTALL.md](docs/INSTALL.md) | 完整安装：Docker / 嵌入模式 / MCP 接入 / 采集器 / 排障 |
| [docs/architecture.md](docs/architecture.md) | 全系统架构：检索管线 / 记忆平面 / 网关 / 部署拓扑 |
| [docs/design-rationale.md](docs/design-rationale.md) | 设计思路：九个关键决策的 why 与被否方案 |
| [docs/RESULTS.md](docs/RESULTS.md) | 实测成果：性能数字 / 端到端验证 / 诚实差距清单 |
| [docs/hallucination-protection.md](docs/hallucination-protection.md) | 三层幻觉防护详解 |
| [docs/world-memory-roadmap.md](docs/world-memory-roadmap.md) | 时序记忆路线图（P1-P9） |
| [docs/security-guide.md](docs/security-guide.md) | 安全配置指南 |

## 路线图

- [ ] 公开基准评测（幻觉率 / 检索准确率，真实数据集）
- [ ] 文档解析升级：接入 DeepDoc / MinerU 级别的版面分析
- [ ] 网关 `memory.search` 工具（Agent 直接检索对话记忆）
- [ ] 多租户管理面 UI
- [ ] World Memory P2-P9（输入适配器 / 查询路由 / 质量门 / 企业发布）

## 为什么开源

企业知识库产品市面不少，但"**可信**"（幻觉防护 + 审计级记忆）+ "**时序**"
（知识会变，答案不该穿越）+ "**个人可控**"（数据不出门 + MCP 网关）三件事
叠在一起的开源方案目前是空的。esRAGPro 把这三件事做成了一套可以自己跑起来的架构，
并诚实地公布哪些做完了、哪些还没有（见 [docs/RESULTS.md](docs/RESULTS.md)）。

## Contributing

Issue / PR 均欢迎。开发环境与代码规范见
[docs/development-guide-backend.md](docs/development-guide-backend.md)。

## License

[MIT](LICENSE)
