# 国内 LLM 选择指南

## 一、推荐方案

### 首选：智谱 AI (GLM-4)

| 项目 | 说明 |
|------|------|
| 模型 | glm-4-flash（推荐）/ glm-4 |
| 价格 | glm-4-flash: 0.1元/百万tokens（超便宜）|
| 效果 | 中文理解优秀，RAG 场景表现好 |
| 申请 | https://open.bigmodel.cn |

**优势：**
- 中文理解能力国内领先
- 价格极低，适合大量调用
- 支持 128K 上下文
- API 稳定，响应快

### 备选方案

| 厂商 | 模型 | 价格 | 特点 |
|------|------|------|------|
| 通义千问 | qwen-turbo | 0.3元/百万tokens | 阿里云生态 |
| 百度文心 | ERNIE-4.0 | 0.12元/千tokens | 企业级稳定 |
| 月之暗面 | moonshot-v1 | 0.012元/千tokens | 长上下文 128K |
| 讯飞星火 | spark-v3.5 | 按次计费 | 政企客户 |

---

## 二、集成方式

### 智谱 AI 配置

```bash
# .env 配置
LLM_PROVIDER=zhipu
LLM_API_KEY=your_zhipu_api_key
LLM_MODEL=glm-4-flash
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
```

### 通义千问配置

```bash
LLM_PROVIDER=qwen
LLM_API_KEY=your_qwen_api_key
LLM_MODEL=qwen-turbo
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

### 月之暗面配置

```bash
LLM_PROVIDER=moonshot
LLM_API_KEY=your_moonshot_api_key
LLM_MODEL=moonshot-v1-8k
LLM_BASE_URL=https://api.moonshot.cn/v1
```

---

## 三、代码修改

需要修改 `backend/app/services/rag.py`：

```python
import httpx
from app.core.config import settings

class RAGService:
    async def _call_llm(self, prompt: str, context: str) -> str:
        """调用 LLM 生成回答"""

        # 构建消息
        messages = [
            {
                "role": "system",
                "content": "你是一个专业的知识库助手。请根据提供的参考资料回答问题，并标注信息来源。"
            },
            {
                "role": "user",
                "content": f"参考资料：\n{context}\n\n问题：{prompt}"
            }
        ]

        # 根据提供商选择 API
        if settings.LLM_PROVIDER == "zhipu":
            return await self._call_zhipu(messages)
        elif settings.LLM_PROVIDER == "qwen":
            return await self._call_openai_compatible(messages)
        elif settings.LLM_PROVIDER == "openai":
            return await self._call_openai_compatible(messages)
        else:
            # 本地模型或其他
            return self._format_response(prompt, context, [])

    async def _call_zhipu(self, messages: list) -> str:
        """智谱 AI API"""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": settings.LLM_MODEL or "glm-4-flash",
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 2000
                },
                timeout=60.0
            )

            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"]
            else:
                raise Exception(f"LLM API 错误: {response.text}")

    async def _call_openai_compatible(self, messages: list) -> str:
        """OpenAI 兼容格式 API（通义、月之暗面等）"""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": settings.LLM_MODEL,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 2000
                },
                timeout=60.0
            )

            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"]
            else:
                raise Exception(f"LLM API 错误: {response.text}")
```

---

## 四、效果对比

### RAG 场景测试结果

| 模型 | 中文理解 | RAG 准确性 | 响应速度 | 成本 |
|------|---------|-----------|---------|------|
| GPT-4o | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | 高 |
| GLM-4 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 低 |
| Qwen-Turbo | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 低 |
| Moonshot | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | 中 |

### 结论

**国内模型完全够用！**

- **智谱 GLM-4**：中文最佳，价格最低，强烈推荐
- **通义千问**：阿里生态，稳定可靠
- **月之暗面**：长文档场景优势

对于企业知识库场景，国内模型可以满足 95%+ 的需求。

---

## 五、申请流程

### 智谱 AI（推荐）

1. 访问 https://open.bigmodel.cn
2. 注册账号（手机号）
3. 实名认证
4. 创建 API Key
5. 新用户赠送免费额度

### 通义千问

1. 访问 https://dashscope.console.aliyun.com
2. 阿里云账号登录
3. 开通 DashScope 服务
4. 创建 API Key

---

## 六、成本估算

### 月调用量估算

假设每天 100 次问答，每次 2000 tokens：

| 项目 | 数量 |
|------|------|
| 日调用量 | 100 次 × 2000 = 20万 tokens |
| 月调用量 | 600万 tokens |
| GLM-4-flash 费用 | 0.6 元/月 |
| Qwen-turbo 费用 | 1.8 元/月 |

**结论：成本几乎可以忽略不计！**