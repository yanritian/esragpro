# 源码树与关键目录分析

## 仓库形态

这是一个多部分仓库：`backend`、`frontend` 和 `esragpro-mcp` 是产品运行部分；`.agent-team` 是开发调度辅助部分；根目录 Docker Compose 和脚本负责本地部署。

## 关键源码树

```text
esRAGPro/
├── backend/                         # Python FastAPI 后端
│   ├── app/main.py                  # FastAPI 入口、路由注册、ES 生命周期
│   ├── app/api/                     # HTTP API
│   │   ├── kb.py                    # 传统知识库上传、搜索、文档管理
│   │   ├── graph.py                 # 知识图谱管理接口
│   │   ├── agentic.py               # Agentic RAG 接口
│   │   ├── memory.py                # 会话/工作记忆接口
│   │   ├── memory_plane.py          # 无权限 Memory Plane 三个端点
│   │   ├── smart_search.py          # 智能检索接口
│   │   ├── auth.py                  # 注册、登录、用户主体
│   │   ├── validate.py              # RAG 快速验证和对比
│   │   ├── evaluation.py            # 评测运行和报告
│   │   └── task.py                  # 任务、步骤、结果接口
│   ├── app/core/                    # 配置和传统 ES 客户端
│   ├── app/memory_core/             # Memory bounded context
│   │   ├── contracts.py              # 受治理记忆合同
│   │   ├── unpermissioned_contracts.py # 无 ACL 记忆合同
│   │   ├── ingestion.py              # 同意、DLP、观察摄取
│   │   ├── review.py                 # 候选审核、冲突、晋级
│   │   ├── retrieval.py              # 受治理智能检索骨架
│   │   ├── unpermissioned_retrieval.py # Phase 2-A 检索服务
│   │   ├── lifecycle.py              # 过期、归档、删除、法律保留
│   │   ├── outbox.py                 # 幂等 Outbox 和重试状态
│   │   ├── evaluator.py/holdout.py  # 评测和封闭数据集
│   │   └── repositories/             # 目标存储适配器，目前多数为 mock
│   ├── app/services/                # RAG、Embedding、图谱、证据、Agentic 服务
│   ├── app/security/                # Principal、ACL、JWT、撤销屏障
│   ├── app/claims/                  # Claim、实体解析、冲突和审核工作流
│   ├── app/db/models/               # PostgreSQL ORM 模型
│   ├── app/projectors/              # ES/Neo4j 投影边界
│   ├── alembic/                     # 数据库迁移
│   ├── tests/                       # 单测、API 测试、集成测试和评测
│   └── pyproject.toml               # Python 包和工具配置
├── frontend/                        # Vue 3 旧管理界面
│   └── src/
│       ├── App.vue/main.js          # 前端入口
│       ├── api/index.js             # Axios API 客户端
│       ├── router/index.js          # 路由
│       └── views/                   # Home、Documents、Search、Graph、Evaluation、AgenticChat
├── esragpro-mcp/                    # Rust MCP Gateway
│   ├── src/main.rs                  # CLI 入口和 stdio/HTTP 模式
│   ├── src/server.rs                # rmcp Server、工具、资源、Prompt
│   ├── src/handlers.rs              # 受权限工具处理器
│   ├── src/handlers_unpermissioned.rs # Memory Plane 参考处理器
│   ├── src/schema/                  # MCP 输入输出合同
│   ├── src/backend/                 # HTTP/embedded 后端边界
│   ├── src/engine/                  # fulltext、vector、RRF、graph pipeline
│   ├── src/extract/                 # 文档、分块、实体、关系抽取
│   └── src/guard/                   # Prompt、事实核查、拒答
├── .agent-team/                     # Codex-owned 调度和 CLI adapters
│   ├── control-plane/               # scheduler、lease、JSONL 事件、smoke
│   ├── adapters/                    # Codex/Claude/Pi/Kimi/ZCode 能力注册
│   ├── memory/                      # 开发任务共享记忆投影
│   └── orca/                        # 观察、回放和项目 smoke
├── docs/                            # 产品、架构、安装、评测和本次生成文档
├── docker-compose*.yml              # ES、PG、Redis、Neo4j、Backend、Frontend
├── start-dev.* / deploy.*           # 本地开发和部署脚本
└── _bmad/ .agents/ .claude/         # 工作流、技能和 Agent 指令资产
```

## 入口与集成点

| 入口 | 下游 | 说明 |
|---|---|---|
| `backend/app/main.py` | 所有 FastAPI routers | 后端唯一应用入口，默认 API 前缀 `/api/v1` |
| `/api/v1/kb/*` | `rag.py`、`core/elasticsearch.py` | 传统企业文档 RAG |
| `/api/v1/memory-plane/*` | `memory_core/unpermissioned_*` | Phase 2-A 无权限记忆合同 |
| `esragpro-mcp/src/main.rs` | `server.rs`、handlers | Agent 的 MCP 协议入口 |
| `frontend/src/api/index.js` | FastAPI | 旧管理界面调用后端 |
| `.agent-team/control-plane/launch.mjs` | adapters/Claude/Codex | 开发任务调度，不进入产品运行时 |

## 生成和缓存目录

以下目录不属于产品源码树的发布内容，应从架构判断和提交审查中单独处理：`graphify-out/`、`backend/graphify-out/`、`esragpro-mcp/graphify-out/`、`.omc/`、`.omx/`、`target/`、`node_modules/`、`.pytest_cache/`。
