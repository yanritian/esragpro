# esRAGPro MCP Gateway 使用指南

## 快速开始

### 1. 确保后端服务运行
```bash
# 后端 API 已在 http://localhost:8000 运行
curl http://localhost:8000/health
```

### 2. Claude Code 自动加载
MCP 服务器已配置在 `~/.claude.json`，Claude Code 启动时自动加载。

### 3. 在 Claude Code 中使用

启动 Claude Code 后，你可以直接提问：

```
> 搜索关于合同违约的文档
> 查询诉讼时效相关规定
> 员工工作时间是如何规定的
```

Claude Code 会自动调用 MCP Gateway 检索知识库。

## MCP 工具列表

| 工具 | 功能 | 用途 |
|------|------|------|
| `kb.search_evidence` | 检索证据 | 搜索相关文档 |
| `kb.answer_with_evidence` | 带引用回答 | 生成答案并标注来源 |
| `graph.lookup_entity` | 查询实体 | 查找知识图谱实体 |
| `graph.expand` | 扩展关系 | 探索实体关系 |
| `graph.find_paths` | 查找路径 | 多跳查询 |
| `graph.verify_claim` | 验证声明 | 验证事实声明 |

## 配置说明

```json
// ~/.claude.json
{
  "mcpServers": {
    "esragpro": {
      "command": "D:/dingsun/esRAGPro/esragpro-mcp/target/release/esragpro-mcp.exe",
      "args": ["stdio"],
      "env": {
        "ESRAGPRO_BACKEND_URL": "http://localhost:8000/api/v1"
      }
    }
  }
}
```

## 架构

```
AI Agent (Claude Code)
    ↓ MCP Protocol (stdio)
esragpro-mcp.exe
    ↓ HTTP API
后端 (FastAPI)
    ↓
Elasticsearch
```

## 测试数据

当前知识库包含 3 个测试文档：
- 民法典第577条（合同违约）
- 民法典第188条（诉讼时效）
- 公司员工手册（工作时间）

## 下一步

1. 添加更多文档到知识库
2. 在 Claude Code 中测试查询
3. 验证检索准确率
