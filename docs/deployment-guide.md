# 部署与运行指南

## Compose 服务

`docker-compose.yml` 定义：

| 服务 | 镜像/角色 | 端口或网络 |
|---|---|---|
| `elasticsearch` | Elasticsearch 8.12.0 | `9200` |
| `postgres` | PostgreSQL 15 | `5432` |
| `redis` | Redis 7 | `6379` |
| `neo4j` | Neo4j 5.15 Community | `7474`、`7687` |
| `backend` | FastAPI Dockerfile | 内部 `8000` |
| `frontend` | Vue 构建 + Nginx | 默认 `80` |

## 开发启动

```powershell
Copy-Item .env.example .env
docker compose up -d elasticsearch postgres redis neo4j
docker compose ps
```

Backend 和 Frontend 的 Compose 启动依赖 Neo4j 健康检查；如果只需要 Memory Plane 单元测试，不需要启动所有容器。

## 生产注意事项

- 使用 `docker-compose.secure.yml` 并设置真实 JWT、数据库和 LLM 密钥。
- 不把无权限 Memory Plane 直接暴露到跨租户网络；通过宿主应用或 Gateway 做边界隔离。
- 持久化卷必须备份，PostgreSQL 是权威数据和审计恢复的优先对象。
- ES、Neo4j、Redis 的投影状态要有 health、lag、replay 和 deletion metrics。
- 当前 Memory 适配器仍有 mock/stub，不能仅凭 Compose 服务启动就宣称记忆系统已生产化。
- 当前文档未证明所有六个 Compose 服务均已在本机同时运行；部署验收必须使用 `docker compose ps`、健康接口和端到端写读测试。

## 环境变量

关键变量包括：`ES_HOST`、`ES_INDEX_NAME`、`DATABASE_URL`、`REDIS_URL`、`NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD`、`LLM_PROVIDER`、`LLM_API_KEY`、`JWT_SECRET_KEY`。
