# 安装指南（INSTALL）

esRAGPro 支持三种安装路径：**Docker 一键部署**（推荐）、**开发模式**（前后端分离热更）、**离线私有化**（本地 embedding）。本文覆盖从零到 MCP 全链路接通的所有步骤。

---

## 0. 环境要求

| 组件 | 版本 | 用途 |
|------|------|------|
| Docker Desktop / Engine | 任意较新版本（WSL2 或 Hyper-V 后端均可） | 运行全栈 |
| Node.js | 18+ | 仅开发模式（前端热更） |
| Python | 3.12+ | 仅开发模式（后端热更） |
| Ollama | 任意版本（可选） | 本地向量嵌入 |
| 4 核 8G | 最低 | 4 核 16G 建议（ES 堆 1G + Neo4j 1G） |

> **内存提示**：如果用 WSL2 后端，建议在 `C:\Users\<你>\.wslconfig` 里加上
> `[wsl2]` `memory=6GB`，防止 Docker 虚拟机吃掉过多宿主内存。

---

## 1. 一键部署（Docker，推荐）

```bash
git clone https://github.com/yanritian/esragpro.git
cd esragpro

# 复制环境模板并编辑
cp .env.example .env
```

`.env` 必须确认的三组配置：

```bash
# (1) MinIO 凭据（compose 有硬性断言，缺失会拒绝启动）
MINIO_ACCESS_KEY=<自定义>
MINIO_SECRET_KEY=<自定义>

# (2) LLM（任选一家的 API Key）
LLM_PROVIDER=zhipu        # 或 qwen / openai
LLM_API_KEY=sk-...
JWT_SECRET_KEY=<随机长字符串>

# (3) 向量嵌入（见第 2 节选择模式）
EMBEDDING_PROVIDER=ollama
```

启动：

```bash
docker compose up -d --build
```

**首次启动时间线**（心里有数，不要慌）：

| 时间 | 现象 |
|------|------|
| 0-90s | Elasticsearch / Neo4j 单节点集群成形。healthcheck 窗口只有 50s，此时可能显示 unhealthy —— 容器带 `restart: unless-stopped` 会自愈 |
| ~90s | 再次执行 `docker compose up -d` 挂载依赖服务（backend / frontend / worker） |
| +30s | 后端执行数据库迁移（首次部署需手动执行一次，见下） |

首次部署手动执行数据库迁移：

```bash
docker exec esragpro-backend alembic upgrade head
```

**验证**：

```bash
curl http://127.0.0.1:8000/health        # {"status":"healthy"}
curl http://127.0.0.1:8000/api/v1/kb/health
```

| 入口 | 地址 |
|------|------|
| 前端 | http://localhost:80 |
| 后端 API | http://127.0.0.1:8000（仅回环绑定） |
| API 文档 | http://127.0.0.1:8000/docs |

---

## 2. 向量嵌入：四种模式怎么选

| 模式 | `EMBEDDING_PROVIDER` | 镜像体积 | 内存 | 数据出本机 | 适用 |
|------|---------------------|---------|------|-----------|------|
| **Ollama（推荐）** | `ollama` | ~600MB | ~150MB | ❌ 全本地 | 私有化部署 |
| 通义 API | `qwen_api` | ~600MB | ~150MB | ✅ | 有阿里云 Key |
| 智谱 API | `zhipu_api` | ~600MB | ~150MB | ✅ | 有智谱 Key |
| 本地 torch | `local` | ~1.5GB | ~600MB | ❌ | 完全离线私有化 |

**Ollama 模式（默认推荐）**：

```bash
ollama pull bge-m3        # 1.2GB，1024 维，与默认 EMBEDDING_DIMENSION 一致
```

`.env`：

```bash
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=bge-m3
EMBEDDING_OLLAMA_URL=http://host.docker.internal:11434   # 容器访问宿主 Ollama
```

> bge-m3 是 560M 参数的小模型：显存占用约 664MB，按需加载、空闲 5 分钟自动卸载，
> GTX 1060 6GB 实测持续压测峰值 77°C——普通办公/游戏本完全无压力。

**本地 torch 模式**（完全离线，镜像较大）：

```bash
docker build --build-arg LOCAL_EMBEDDING=1 -t esragpro-backend:local ./backend
# 然后 .env 设 EMBEDDING_PROVIDER=local
```

---

## 3. MCP 网关：把 esRAGPro 接入你的 AI Agent

esRAGPro 自带 Rust MCP 网关（`esragpro-mcp/`），支持 stdio 与 Streamable HTTP 双传输。

### 3.1 构建

```bash
cd esragpro-mcp
cargo build --release --features http
```

产物：`target/release/esragpro-mcp.exe`

### 3.2 启动 HTTP 单实例（多 Agent 共用）

```bash
export ESRAGPRO_HTTP_ENABLED=1
export ESRAGPRO_HTTP_ADDR=127.0.0.1:8421
export ESRAGPRO_BACKEND_URL=http://localhost:8000/api/v1
export ESRAGPRO_AUTH_TOKEN=<你的共享密钥>       # 本地静态令牌模式
./target/release/esragpro-mcp.exe http -a 127.0.0.1:8421
```

> **认证双模式**：配置 `ESRAGPRO_AUTH_TOKEN` 且未配 OIDC Issuer 时进入本地静态
> 令牌模式（单机部署）；生产多用户部署请配置 `ESRAGPRO_OAUTH_ISSUER` 等走完整 JWT。

### 3.3 各 Agent 接入

**Claude Code**（HTTP）：

```bash
claude mcp add --transport http esragpro http://127.0.0.1:8421/mcp \
  --header "Authorization: Bearer <你的共享密钥>"
```

**Codex**（stdio，`~/.codex/config.toml`）：

```toml
[mcp_servers.esragpro]
command = "D:\\path\\to\\esragpro-mcp.exe"
args = ["stdio"]
env = { ESRAGPRO_BACKEND_URL = "http://localhost:8000/api/v1" }
```

**PI**（`~/.pi/agent/mcp.json`）：

```json
{
  "mcpServers": {
    "esragpro": {
      "url": "http://127.0.0.1:8421/mcp",
      "headers": { "Authorization": "Bearer <你的共享密钥>" }
    }
  }
}
```

**WorkBuddy**（`~/.workbuddy-ai/mcp.json`）：同 Codex 的 stdio 格式。

接入后可用工具：`kb.search_evidence`、`kb.answer_with_evidence`、
`graph.lookup_entity`、`graph.expand`、`graph.find_paths`、`graph.verify_claim`。

---

## 4. Agent 对话自动入库（记忆采集器）

`scripts/agent_memory_collector.py` 会增量读取本机 Claude Code / Codex / PI 的
会话文件，把对话内容写入 memory-plane，供后续检索。

一次性配置（Windows 计划任务，每 5 分钟运行）：

```powershell
$action = New-ScheduledTaskAction -Execute "<venv>\Scripts\pythonw.exe" `
  -Argument "D:\path\to\esragpro\scripts\agent_memory_collector.py"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "esragpro-memory-collector" `
  -Action $action -Trigger $trigger -Force
```

特性：字节偏移增量同步（state 文件）、后端不可用时偏移不推进、单次限流 100 条。

---

## 5. 开发模式（不使用 Docker 运行前后端）

```bash
# 后端（首次先装依赖；本地 embedding 需要 .[local-embedding] 扩展）
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev
```

---

## 6. 常见问题（Troubleshooting）

| 现象 | 原因 | 处理 |
|------|------|------|
| ES 容器 unhealthy，backend 起不来 | 单节点集群成形需 60-90s，healthcheck 窗口只有 50s | 等 ES 自愈（restart 策略会保活），再 `docker compose up -d` |
| backend 崩溃循环，日志 `requires greenlet` | 已修复（`sqlalchemy[asyncio]`），旧镜像需重建 | `docker compose build backend && docker compose up -d backend` |
| `MINIO_ACCESS_KEY must be set` | `.env` 缺 MinIO 凭据 | 按第 1 节填写 |
| 推送/拉取 GitHub 连接重置 | 国内网络对 github.com 干扰 | git 走本地代理：`git config --global http.https://github.com.proxy http://127.0.0.1:7897` |
| ollama 报 model not found | 模型未拉取或标签不对 | `ollama pull bge-m3`；`BAAI/bge-m3` 会自动映射为 `bge-m3` |
| 切换 embedding 后检索质量下降 | 不同实现的向量语义有差异 | 重建索引：删除文档后重新上传（维度不变，mapping 无需改） |
