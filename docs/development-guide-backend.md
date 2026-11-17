# Backend 开发指南

## 前置条件

- Windows 10/11、Python 3.10+。
- Elasticsearch 8.12+。
- PostgreSQL 15（传统模型/迁移需要）。
- Redis 7、Neo4j 5.15（对应功能启用时）。
- 本地 `.env`，参考 `.env.example`。

## Windows 本地运行

```powershell
cd D:\dingsun\esRAGPro\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
uvicorn app.main:app --reload --port 8000
```

## 测试

```powershell
cd D:\dingsun\esRAGPro\backend
pytest
pytest -q tests/unit/test_unpermissioned_memory.py tests/unit/test_memory_plane_import.py
```

当前新增 Memory Plane 定向测试已验证 57 passed；完整后端套件应在真实数据库依赖和环境变量准备后重新执行。

## 变更规则

1. 修改 Memory 合同时同步更新 Python schema、Rust schema、API 测试、MCP 测试和文档。
2. 修改存储投影时先明确 PostgreSQL 权威记录、ES/Neo4j/Redis 的投影语义。
3. 不把相似度、模型输出或图路径直接发布为事实；必须经过证据和审核。
4. 新接口先写失败/拒答测试，再实现 happy path。
5. 任何涉及租户/ACL 的传统接口不得被无权限 Memory Plane 的数据维度逻辑替代。
