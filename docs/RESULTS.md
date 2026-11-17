# 实测成果（Measured Results）

> 本文所有数字均来自真实运行环境（Windows 11 + GTX 1060 6GB 笔记本 +
> Docker Desktop + 本地 Ollama），测试日期 2026-11-17。复现方法见各节。

---

## 1. 平台规模

| 项 | 数字 |
|---|---|
| 后端 Python 源文件 | 204 个（API 12 路由 / 服务 27 / memory_core ~60） |
| 测试文件 | 197 个（单元 + API + e2e + 集成，覆盖 P1-01~P1-09 全阶段） |
| Rust MCP 网关 | WP0-WP9 全阶段完成，6 个工具，双传输 |
| 数据库迁移 | Alembic 全 head 链（p1 至 p9_d3） |
| Docker 服务 | 8 个（ES/PG/Redis/MinIO/Neo4j/backend/minio-worker/frontend） |
| API 端点 | 42 个（auth/kb/agentic/evaluation/graph/validate/memory-plane/world-memory） |

---

## 2. 幻觉防护：架构目标 vs 实测状态

| 防护配置 | 幻觉率（架构目标） | 实测状态 |
|------|-----------|------|
| 无防护 | 20-40% | — |
| 仅安全提示词 | 10-20% | ✅ 已实现 |
| 三层完整防护 | **3-5%** | ⚠️ 框架就绪，公开基准数字待跑 |

三层防护的每一层都是独立可关闭的（测试覆盖见
`backend/tests/unit/`），逐声明核查的证据引用与拒答路径由
`refusal_service` 与 `evidence_packet` 支撑。

> 诚实说明：3-5% 是架构推算目标。评测框架（6 类测试用例、报告生成、
> 指标解释）已就绪，公开数据集的实测数字见 [第 6 节](#6-待办与差距)。

---

## 3. 端到端记忆入库（实测 2026-11-17）

模拟真实场景：把本机三个编码 Agent（Claude Code / Codex / PI）的历史会话
自动采集进记忆平面。

| 步骤 | 结果 |
|---|---|
| 采集器增量扫描三类会话文件 | ✅ JSONL 逐行解析，字节偏移增量 |
| 推送 memory-plane API | ✅ 106 条，全部 201 Created |
| PostgreSQL 权威存储 | ✅ 106 行，状态 active |
| 查询接口 | ✅ 返回真实对话内容 + agent/project 元数据 |
| 零丢失保证 | ✅ 后端不可用时偏移不推进，恢复自动补齐 |

单次限流 100 条（防首次洪水），积压由计划任务每 5 分钟自动排空。

---

## 4. MCP 网关（实测）

| 指标 | 结果 |
|---|---|
| 二进制体积 | 9.6 MB（Rust 单文件，零运行时依赖） |
| 构建时间 | 5-10 min（`cargo build --release --features http`） |
| MCP 握手 | initialize → serverInfo (rmcp 1.8.0) ✅ |
| 认证 | 无令牌 → 401 ✅；静态令牌 → 200 ✅ |
| 工具暴露 | 6 个（kb ×2 / graph ×4） |

认证双模式实测通过：
- 生产模式：OIDC RS256 JWT（kid/JWKS/iss/aud 全校验，代码路径不变）
- 本机模式：`ESRAGPRO_AUTH_TOKEN` 静态共享令牌（恒时比较）

---

## 5. 部署形态实测（消费级硬件）

环境：Windows 11 笔记本，i7-7700HQ (4C/8T)，GTX 1060 6GB，32GB RAM。

### 5.1 镜像与内存（torch 解耦前后）

| 配置 | 镜像体积 | 后端进程 RSS |
|------|---------|-------------|
| 含本地 torch embedding（旧） | ~1.3GB+ | +400-500MB |
| **Slim + 远程/无 torch（现默认）** | **~600MB** | **~150-250MB** |

### 5.2 本地 Ollama 嵌入（bge-m3，1024 维）

| 指标 | 实测 |
|---|---|
| 显存占用 | 664MB（按需加载，空闲 5 分钟自动卸载） |
| 150 条长文本持续压测（29s） | 显存峰值 2477MB/6144，温度峰值 77°C，利用率均值 57% |
| 语义 sanity | 同题余弦 1.0 ✓，跨题 0.43 ✓，维度 1024 ✓ |

### 5.3 本地 LLM 推理（对照参考，非 esRAGPro 组件）

GTX 1060 6GB 全量 offload Q4_K_M 9B 模型：16 t/s（GGUF IQ3_M 稳定配置 12 t/s）。
见仓库根目录 `run-autostart.bat` 的注释。

---

## 6. 待办与差距（诚实清单）

已知未完成项，按影响排序（详见
[world-memory-roadmap.md](world-memory-roadmap.md) 与
[design-rationale.md](design-rationale.md) 第 9 节）：

1. **公开基准未跑**：评测框架就绪，LoCoMo 等真实数据集数字待出
2. **文档解析深度**：无 OCR/版面分析，扫描件与跨页表格场景弱于 RAGFlow DeepDoc
3. **S3 上传/下载/删除为 TODO**（`backend/app/storage/blob.py`）
4. **四个前端视图的路由已接线**，管理页 UI 细节仍在打磨
5. **记忆检索 MCP 工具**：对话已入库可查（REST），网关 `memory.search` 工具待加
6. **多租户与 ACL**：租户隔离测试已覆盖，管理面 UI 未做

---

## 7. 复现说明

```bash
# 记忆入库端到端
docker compose up -d --build
docker exec esragpro-backend alembic upgrade head
python scripts/agent_memory_collector.py
curl -X POST http://127.0.0.1:8000/api/v1/memory-plane/query \
  -H "Content-Type: application/json" \
  -d '{"query":"<任意你的会话主题>","require_evidence":false}'

# Ollama 嵌入
ollama pull bge-m3
curl http://127.0.0.1:11434/api/embed \
  -d '{"model":"bge-m3","input":["维度与归一化测试"]}'
```

有任何复现偏差，欢迎提 Issue。
