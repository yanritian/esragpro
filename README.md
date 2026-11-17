# esRAGPro

企业可信知识库解决方案 - 基于 Elasticsearch 混合检索 + 三层幻觉防护

## 功能特性

- **混合检索**: BM25 + 向量检索 + RRF 融合，检索准确率提升
- **幻觉防护**: 三层防护机制，将幻觉率降至 3-5%
- **知识图谱**: Neo4j 图数据库支持多跳问答和关系推理
- **答案可追溯**: 每个回答标注来源文档，支持原文验证
- **置信度标注**: 实时返回回答置信度 (0-1)
- **智能拒绝**: 超出文档范围时主动拒绝回答
- **多格式支持**: PDF、DOCX、TXT、Markdown
- **私有部署**: 数据不出企业，安全可控

## 幻觉防护

esRAGPro 实现了完整的幻觉防护系统：

```
三层防护：
├── 安全提示词：强调必须基于文档、禁止编造
├── 事实核查：验证每个声明是否有来源支撑
└── 拒绝机制：无信息时拒绝回答、信息不足时警告
```

详见：[幻觉防护文档](docs/hallucination-protection.md)

| 配置 | 预期幻觉率 |
|------|-----------|
| 无防护 | 20-40% |
| 仅提示词 | 10-20% |
| 完整防护 | **3-5%** |

## 快速开始

### 📖 详细安装指南

请查看：**[完整安装文档](docs/INSTALL.md)**

### ⚡ 一键安装

```bash
# 下载安装脚本
wget https://raw.githubusercontent.com/你的用户名/esRAGPro/main/install.sh

# 执行安装
bash install.sh
```

### 🛡️ 安全部署

```bash
# 使用安全版配置
docker compose -f docker-compose.secure.yml up -d --build

# 运行安全加固
./setup-security.sh

# 配置 HTTPS
./setup-https.sh 你的域名 你的邮箱
```

### 环境要求

- Docker & Docker Compose
- Node.js 18+ (开发模式)
- Python 3.10+ (开发模式)
- Elasticsearch 8.12+ (或使用 Docker)

### 开发模式

```bash
# 克隆项目
git clone <repo-url>
cd esRAGPro

# 复制环境变量配置
cp .env.example .env

# Windows
start-dev.bat

# Linux/macOS
chmod +x start-dev.sh && ./start-dev.sh
```

访问:
- 前端: http://localhost:3000
- 后端 API: http://localhost:8000
- API 文档: http://localhost:8000/docs

## 技术架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              用户界面 (Vue 3)                             │
└─────────────────────────────────────┬───────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           API 网关 (Nginx)                               │
│    • HTTPS 加密传输  • 速率限制  • 安全头防护                            │
└─────────────────────────────────────┬───────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          后端服务 (FastAPI)                               │
│                                                                          │
│   用户查询 ──► 混合检索(BM25+向量+RRF)                                    │
│                         │                                                │
│                         ▼                                                │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │                   幻觉防护系统 (三层防护)                         │   │
│   │  第一层：安全提示词 ──► 第二层：事实核查 ──► 第三层：拒绝机制    │   │
│   └─────────────────────────────────────────────────────────────────┘   │
│                         │                                                │
│                         ▼                                                │
│              LLM 生成 (智谱/通义/OpenAI)                                 │
│                         │                                                │
│                         ▼                                                │
│              安全、可追溯的回答                                          │
│                                                                          │
└─────────────────────────────────────┬───────────────────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
                    ▼                 ▼                 ▼
            ┌───────────┐     ┌───────────┐     ┌───────────┐
            │    ES     │     │ PostgreSQL│     │   Redis   │
            │  (内部)   │     │  (内部)   │     │  (内部)   │
            └───────────┘     └───────────┘     └───────────┘
```

## 核心算法

### RRF (Reciprocal Rank Fusion)

将 BM25 和向量检索结果融合：

```
RRF_score(d) = Σ 1/(k + rank_i(d))

其中:
- d: 文档
- k: 平滑参数 (默认 60)
- rank_i(d): 文档 d 在第 i 个排序列表中的排名
```

### 幻觉检测

基于多维度验证：
- 声明来源核查
- 关键词匹配度
- 置信度计算

## 配置说明

主要环境变量:

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ES_HOST` | Elasticsearch 地址 | `http://localhost:9200` |
| `EMBEDDING_MODEL` | 嵌入模型 | `BAAI/bge-m3` |
| `RRF_K` | RRF 参数 | `60` |
| `LLM_PROVIDER` | LLM 提供商 | `zhipu` |
| `LLM_API_KEY` | LLM API 密钥 | - |
| `JWT_SECRET_KEY` | JWT 密钥 | - |

## API 文档

启动后端后访问:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

主要端点:

```
POST /api/v1/kb/upload        # 上传文档
GET  /api/v1/kb/documents     # 文档列表
POST /api/v1/kb/search        # 搜索 (带幻觉防护)
GET  /api/v1/validate/quick   # 效果验证
DELETE /api/v1/kb/documents/{id}  # 删除文档
```

## 目录结构

```
esRAGPro/
├── backend/           # FastAPI 后端
│   ├── app/
│   │   ├── api/      # API 路由
│   │   ├── core/     # 核心配置
│   │   ├── models/   # 数据模型
│   │   └── services/ # 业务逻辑
│   │       ├── rag.py              # RAG 服务
│   │       ├── hallucination_guard.py  # 幻觉防护
│   │       └── validator.py       # 效果验证
│   └── pyproject.toml
├── frontend/          # Vue 3 前端
│   ├── src/
│   │   ├── api/      # API 客户端
│   │   ├── router/   # 路由
│   │   └── views/    # 页面
│   └── package.json
├── docs/              # 项目文档
│   ├── INSTALL.md    # 安装指南
│   ├── hallucination-protection.md  # 幻觉防护
│   ├── security-guide.md  # 安全指南
│   └── llm-options.md     # LLM 选择
├── docker-compose.yml
├── docker-compose.secure.yml  # 安全版
└── .env.example
```

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/INSTALL.md](docs/INSTALL.md) | 完整安装指南 |
| [docs/hallucination-protection.md](docs/hallucination-protection.md) | 幻觉防护系统 |
| [docs/security-guide.md](docs/security-guide.md) | 安全配置指南 |
| [docs/llm-options.md](docs/llm-options.md) | LLM 选择指南 |

## 许可证

MIT License
