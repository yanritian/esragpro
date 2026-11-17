# esRAGPro MCP Agent 记忆系统实现规划

> 版本：1.0  
> 日期：2026-07-13  
> 状态：规划阶段

---

## 目录

1. [项目概述](#项目概述)
2. [当前状态分析](#当前状态分析)
3. [AI Agent 记忆系统架构](#ai-agent-记忆系统架构)
4. [缺失组件详细设计](#缺失组件详细设计)
5. [技术实现方案](#技术实现方案)
6. [实施计划](#实施计划)
7. [测试策略](#测试策略)
8. [部署方案](#部署方案)

---

## 项目概述

### 目标

为 esRAGPro MCP Gateway 添加完整的 Agent 记忆系统，使其能够：
- 管理当前会话上下文（工作记忆）
- 追踪任务状态和进度（中期记忆）
- 从对话中提取并持久化关键信息（长期记忆写入）
- 基于上下文智能融合检索（工作记忆 + 长期记忆）
- 管理记忆生命周期（编码、巩固、遗忘）

### 核心价值

1. **多轮对话支持**：Agent 能够理解对话上下文
2. **个性化服务**：记住用户偏好和历史交互
3. **任务连续性**：跨会话追踪任务进度
4. **知识积累**：从对话中自动提取和沉淀知识

---

## 当前状态分析

### 已实现组件 ✅

| 组件 | 状态 | 说明 |
|------|------|------|
| **MCP Gateway** | ✅ 完成 | Rust 实现，支持 stdio/HTTP |
| **长期记忆存储** | ✅ 完成 | Elasticsearch 8.12，混合检索 |
| **检索机制** | ✅ 完成 | BM25 + 向量，Recall@3: 100% |
| **MCP 工具** | ✅ 完成 | 6 个只读工具 |
| **OAuth 认证** | ✅ 完成 | JWT + JWKS |
| **测试覆盖** | ✅ 完成 | 64 tests passed |

### 缺失组件 ❌

| 组件 | 优先级 | 说明 |
|------|--------|------|
| **工作记忆** | P0 | 当前会话上下文管理 |
| **中期记忆** | P1 | 任务状态和中间结果 |
| **记忆写入** | P0 | 从对话中提取关键信息 |
| **智能检索** | P1 | 融合工作记忆和长期记忆 |
| **记忆生命周期** | P2 | 权重衰减、冲突解决、遗忘 |

---

## AI Agent 记忆系统架构

### 标准记忆分类

```
┌─────────────────────────────────────────────────────────┐
│                    Agent 记忆系统                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 工作记忆（短期记忆）                              │   │
│  │ - 当前会话上下文                                  │   │
│  │ - 对话历史（最近 N 轮）                           │   │
│  │ - 当前任务状态                                    │   │
│  │ - 存储：PostgreSQL / Redis                        │   │
│  └─────────────────────────────────────────────────┘   │
│                         ↓                               │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 中期记忆（任务记忆）                              │   │
│  │ - 任务进度追踪                                    │   │
│  │ - 已完成步骤                                      │   │
│  │ - 中间结果缓存                                    │   │
│  │ - 存储：PostgreSQL                                │   │
│  └─────────────────────────────────────────────────┘   │
│                         ↓                               │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 长期记忆（知识库）                                │   │
│  │ - 用户偏好                                        │   │
│  │ - 历史决策                                        │   │
│  │ - 事实知识                                        │   │
│  │ - 经验教训                                        │   │
│  │ - 存储：Elasticsearch + 向量索引                  │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 记忆流转机制

```
用户输入
   ↓
[工作记忆] 当前对话上下文
   ↓
[中期记忆] 任务状态检查
   ↓
[检索融合] 工作记忆 + 长期记忆
   ↓
[知识检索] Elasticsearch
   ↓
[响应生成] LLM
   ↓
[记忆写入] 提取关键信息 → 长期记忆
```

---

## 缺失组件详细设计

### 1. 工作记忆（短期记忆）

#### 1.1 数据模型

```sql
-- 会话表
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB DEFAULT '{}',
    status VARCHAR(20) DEFAULT 'active' -- active, expired, closed
);

-- 对话历史表
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL, -- user, assistant, system
    content TEXT NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'
);

-- 索引
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_tenant ON sessions(tenant_id);
CREATE INDEX idx_conversations_session ON conversations(session_id);
CREATE INDEX idx_conversations_timestamp ON conversations(timestamp);
```

#### 1.2 API 设计

```rust
// MCP 工具扩展
#[tool(name = "memory.get_context")]
async fn get_context(
    session_id: String,
    last_n: Option<usize>, // 最近 N 轮对话
) -> Result<ConversationContext> {
    // 获取会话上下文
}

#[tool(name = "memory.save_message")]
async fn save_message(
    session_id: String,
    role: String,
    content: String,
) -> Result<()> {
    // 保存消息到工作记忆
}

#[tool(name = "memory.clear_context")]
async fn clear_context(
    session_id: String,
) -> Result<()> {
    // 清空会话上下文
}
```

#### 1.3 上下文窗口管理

```python
# 滑动窗口策略
class ContextWindowManager:
    def __init__(self, max_tokens=4000):
        self.max_tokens = max_tokens
    
    def get_context(self, conversations, last_n=10):
        # 策略 1: 固定轮数
        recent = conversations[-last_n:]
        
        # 策略 2: Token 限制
        tokens = sum(len(c.content) for c in recent)
        if tokens > self.max_tokens:
            # 压缩早期对话
            summary = await self.summarize(conversations[:-last_n])
            return [summary] + recent
        
        return recent
    
    async def summarize(self, conversations):
        # 使用轻量模型压缩对话
        return await llm.summarize(conversations)
```

---

### 2. 中期记忆（任务记忆）

#### 2.1 数据模型

```sql
-- 任务表
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id),
    user_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(20) DEFAULT 'pending', -- pending, in_progress, completed, failed
    progress INTEGER DEFAULT 0, -- 0-100
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB DEFAULT '{}'
);

-- 任务步骤表
CREATE TABLE task_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    result TEXT,
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB DEFAULT '{}'
);

-- 任务中间结果表
CREATE TABLE task_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    step_id UUID REFERENCES task_steps(id),
    result_type VARCHAR(50), -- text, json, file, etc.
    content TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'
);
```

#### 2.2 API 设计

```rust
#[tool(name = "task.create")]
async fn create_task(
    title: String,
    description: Option<String>,
) -> Result<Task> {
    // 创建新任务
}

#[tool(name = "task.update_status")]
async fn update_task_status(
    task_id: String,
    status: String,
    progress: Option<i32>,
) -> Result<Task> {
    // 更新任务状态
}

#[tool(name = "task.get_status")]
async fn get_task_status(
    task_id: String,
) -> Result<TaskStatus> {
    // 获取任务状态和进度
}

#[tool(name = "task.add_step")]
async fn add_task_step(
    task_id: String,
    description: String,
) -> Result<TaskStep> {
    // 添加任务步骤
}
```

---

### 3. 记忆写入机制

#### 3.1 信息提取流程

```python
class MemoryExtractor:
    """从对话中提取关键信息并写入长期记忆"""
    
    async def extract_from_conversation(self, conversation):
        """
        流程：
        1. 分析对话内容
        2. 提取关键事实、偏好、决策
        3. 向量化
        4. 写入 Elasticsearch
        """
        
        # Step 1: 提取关键信息
        key_facts = await self._extract_key_facts(conversation)
        
        # Step 2: 去重和冲突检测
        filtered_facts = await self._deduplicate_and_resolve(key_facts)
        
        # Step 3: 向量化
        embeddings = await self._embed_facts(filtered_facts)
        
        # Step 4: 写入长期记忆
        await self._write_to_long_term_memory(filtered_facts, embeddings)
        
        return len(filtered_facts)
    
    async def _extract_key_facts(self, conversation):
        """使用 LLM 提取关键信息"""
        prompt = f"""
        从以下对话中提取关键信息：
        - 用户偏好
        - 重要事实
        - 决策和结论
        - 经验教训
        
        对话内容：
        {conversation}
        
        返回 JSON 格式：
        {{
            "facts": [
                {{"content": "...", "type": "preference|fact|decision|lesson", "importance": 0.8}}
            ]
        }}
        """
        
        response = await llm.generate(prompt)
        return json.loads(response)
    
    async def _deduplicate_and_resolve(self, facts):
        """去重和冲突解决"""
        existing = await self._get_similar_memories(facts)
        
        resolved = []
        for fact in facts:
            # 检查是否已存在
            similar = self._find_similar(fact, existing)
            
            if similar:
                # 冲突解决：新事实优先
                if fact['importance'] > similar['importance']:
                    await self._mark_as_obsolete(similar)
                    resolved.append(fact)
            else:
                resolved.append(fact)
        
        return resolved
    
    async def _write_to_long_term_memory(self, facts, embeddings):
        """写入 Elasticsearch"""
        documents = []
        for fact, embedding in zip(facts, embeddings):
            documents.append({
                'content': fact['content'],
                'type': fact['type'],
                'importance': fact['importance'],
                'embedding': embedding,
                'timestamp': datetime.now(),
                'metadata': fact.get('metadata', {})
            })
        
        await es_client.bulk_index('long_term_memory', documents)
```

#### 3.2 触发机制

```python
# 自动触发：对话结束后
async def on_conversation_end(session_id):
    conversation = await get_full_conversation(session_id)
    extractor = MemoryExtractor()
    await extractor.extract_from_conversation(conversation)

# 手动触发：用户显式要求
@tool(name="memory.save_to_long_term")
async def save_to_long_term(session_id: str):
    """手动将当前会话保存到长期记忆"""
    conversation = await get_full_conversation(session_id)
    extractor = MemoryExtractor()
    count = await extractor.extract_from_conversation(conversation)
    return {"saved_facts": count}
```

---

### 4. 智能检索融合

#### 4.1 融合检索架构

```python
class SmartRetriever:
    """融合工作记忆和长期记忆的检索器"""
    
    async def retrieve(self, query, session_id=None, top_k=10):
        """
        融合检索流程：
        1. 获取工作记忆（当前对话上下文）
        2. 检索长期记忆（知识库）
        3. 融合和重排序
        4. 返回结果
        """
        
        # Step 1: 获取工作记忆
        working_memory = []
        if session_id:
            working_memory = await self._get_working_memory(session_id)
        
        # Step 2: 检索长期记忆
        long_term_results = await self._retrieve_long_term(query, top_k * 2)
        
        # Step 3: 融合
        fused_results = await self._fuse_results(
            query=query,
            working_memory=working_memory,
            long_term_results=long_term_results,
            top_k=top_k
        )
        
        return fused_results
    
    async def _fuse_results(self, query, working_memory, long_term_results, top_k):
        """融合工作记忆和长期记忆"""
        
        # 工作记忆权重（上下文相关性）
        working_weight = 0.3
        
        # 长期记忆权重（知识相关性）
        long_term_weight = 0.7
        
        # 计算融合分数
        all_results = []
        
        # 工作记忆结果
        for item in working_memory:
            relevance = await self._calculate_relevance(query, item.content)
            score = relevance * working_weight
            all_results.append({
                'content': item.content,
                'source': 'working_memory',
                'score': score,
                'timestamp': item.timestamp
            })
        
        # 长期记忆结果
        for item in long_term_results:
            score = item.score * long_term_weight
            all_results.append({
                'content': item.content,
                'source': 'long_term_memory',
                'score': score,
                'timestamp': item.timestamp
            })
        
        # 排序并返回 top_k
        all_results.sort(key=lambda x: x['score'], reverse=True)
        return all_results[:top_k]
```

#### 4.2 MCP 工具扩展

```rust
#[tool(name = "kb.smart_search")]
async fn smart_search(
    query: String,
    session_id: Option<String>,
    top_k: Option<usize>,
) -> Result<Vec<SearchResult>> {
    // 融合工作记忆和长期记忆的智能检索
    let retriever = SmartRetriever::new();
    retriever.retrieve(&query, session_id.as_deref(), top_k.unwrap_or(10)).await
}
```

---

### 5. 记忆生命周期管理

#### 5.1 权重衰减

```python
class MemoryDecay:
    """记忆权重随时间衰减"""
    
    def __init__(self, half_life_days=30):
        self.half_life_days = half_life_days
    
    def calculate_score(self, memory):
        """
        score = relevance × importance × decay(t)
        """
        relevance = memory.relevance
        importance = memory.importance
        age_days = (datetime.now() - memory.timestamp).days
        
        # 指数衰减
        decay = 0.5 ** (age_days / self.half_life_days)
        
        return relevance * importance * decay
```

#### 5.2 冲突解决

```python
class ConflictResolver:
    """处理记忆冲突"""
    
    async def resolve_conflict(self, new_fact, existing_fact):
        """
        冲突解决策略：
        1. 时间优先：新事实优先
        2. 重要性优先：高重要性优先
        3. 来源可信度：权威来源优先
        """
        
        # 策略 1: 新事实优先
        if new_fact.timestamp > existing_fact.timestamp:
            return new_fact
        
        # 策略 2: 重要性优先
        if new_fact.importance > existing_fact.importance * 1.2:
            return new_fact
        
        # 默认保留旧事实
        return existing_fact
```

#### 5.3 遗忘策略

```python
class MemoryForgettor:
    """定期清理过期/低价值记忆"""
    
    async def cleanup(self):
        """
        清理规则：
        1. 超过 1 年未访问的记忆
        2. 重要性 < 0.3 的记忆
        3. 权重衰减后分数 < 0.1 的记忆
        """
        
        # 查询需要清理的记忆
        old_memories = await es_client.search(
            index='long_term_memory',
            query={
                'bool': {
                    'should': [
                        {'range': {'last_accessed': {'lt': 'now-1y'}}},
                        {'range': {'importance': {'lt': 0.3}}},
                        {'range': {'decay_score': {'lt': 0.1}}}
                    ]
                }
            }
        )
        
        # 批量删除
        await es_client.bulk_delete('long_term_memory', old_memories)
```

---

## 技术实现方案

### 技术栈

| 组件 | 技术选型 | 说明 |
|------|---------|------|
| **工作记忆** | PostgreSQL + Redis | 持久化 + 缓存 |
| **中期记忆** | PostgreSQL | 任务状态和步骤 |
| **长期记忆** | Elasticsearch 8.12 | 已有，向量检索 |
| **MCP Gateway** | Rust + rmcp SDK | 已有，扩展工具 |
| **记忆提取** | LLM API | 从对话中提取信息 |
| **向量化** | BGE-M3 | 已有 |

### 架构图

```
┌──────────────────────────────────────────────────────────────┐
│                        AI Agent (Claude Code)                 │
└────────────────────┬─────────────────────────────────────────┘
                     │ MCP Protocol (stdio/HTTP)
                     ↓
┌──────────────────────────────────────────────────────────────┐
│                    esragpro-mcp (Rust)                        │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ MCP Tools (扩展)                                        │ │
│  │ - kb.search_evidence                                    │ │
│  │ - kb.smart_search (新)                                  │ │
│  │ - memory.get_context (新)                               │ │
│  │ - memory.save_message (新)                              │ │
│  │ - task.create (新)                                      │ │
│  │ - task.update_status (新)                               │ │
│  └────────────────────────────────────────────────────────┘ │
└────────────────────┬─────────────────────────────────────────┘
                     │ HTTP API
                     ↓
┌──────────────────────────────────────────────────────────────┐
│                    后端服务 (FastAPI)                          │
│  ┌─────────────────┐  ┌─────────────────┐                   │
│  │ 工作记忆服务     │  │ 中期记忆服务     │                   │
│  │ - Session 管理   │  │ - Task 管理      │                   │
│  │ - 对话历史       │  │ - 步骤追踪       │                   │
│  └────────┬────────┘  └────────┬────────┘                   │
│           │                     │                             │
│           ↓                     ↓                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 长期记忆服务 (已有)                                    │   │
│  │ - Elasticsearch 检索                                   │   │
│  │ - 记忆写入                                             │   │
│  │ - 记忆生命周期管理                                     │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────────────┬─────────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
   PostgreSQL    Redis      Elasticsearch
   (会话/任务)   (缓存)     (长期记忆)
```

---

## 实施计划

### Phase 1: 工作记忆（1-2 周）

#### Week 1: 数据模型和基础服务

**Day 1-2: 数据库设计**
- [ ] 创建 sessions 表
- [ ] 创建 conversations 表
- [ ] 添加索引和约束
- [ ] 编写迁移脚本

**Day 3-4: 后端服务**
- [ ] 实现 SessionService
  - create_session()
  - get_session()
  - close_session()
- [ ] 实现 ConversationService
  - save_message()
  - get_history()
  - get_recent_context()

**Day 5-7: API 端点**
- [ ] POST /api/v1/sessions - 创建会话
- [ ] GET /api/v1/sessions/{id} - 获取会话
- [ ] POST /api/v1/sessions/{id}/messages - 保存消息
- [ ] GET /api/v1/sessions/{id}/context - 获取上下文

#### Week 2: MCP 工具集成

**Day 8-10: MCP 工具**
- [ ] 实现 memory.get_context 工具
- [ ] 实现 memory.save_message 工具
- [ ] 实现 memory.clear_context 工具
- [ ] 编写工具测试

**Day 11-12: 上下文窗口管理**
- [ ] 实现滑动窗口策略
- [ ] 实现 Token 限制策略
- [ ] 实现对话压缩（使用 LLM）

**Day 13-14: 测试和文档**
- [ ] 单元测试覆盖
- [ ] 集成测试
- [ ] 编写使用文档

### Phase 2: 记忆写入（1 周）

**Day 15-17: 信息提取**
- [ ] 实现 MemoryExtractor 类
- [ ] 实现 _extract_key_facts() 方法
- [ ] 实现 _deduplicate_and_resolve() 方法

**Day 18-19: 写入机制**
- [ ] 实现自动触发（对话结束后）
- [ ] 实现手动触发（memory.save_to_long_term 工具）
- [ ] 编写写入测试

**Day 20-21: 测试和优化**
- [ ] 端到端测试
- [ ] 性能优化
- [ ] 编写文档

### Phase 3: 中期记忆（1 周）

**Day 22-24: 数据模型**
- [ ] 创建 tasks 表
- [ ] 创建 task_steps 表
- [ ] 创建 task_results 表

**Day 25-27: 服务实现**
- [ ] 实现 TaskService
- [ ] 实现 TaskStepService
- [ ] 实现 API 端点

**Day 28: MCP 工具**
- [ ] 实现 task.create 工具
- [ ] 实现 task.update_status 工具
- [ ] 实现 task.get_status 工具
- [ ] 实现 task.add_step 工具

### Phase 4: 智能检索（1 周）

**Day 29-31: 融合检索器**
- [ ] 实现 SmartRetriever 类
- [ ] 实现 _fuse_results() 方法
- [ ] 实现权重计算

**Day 32-33: MCP 工具**
- [ ] 实现 kb.smart_search 工具
- [ ] 编写测试

**Day 34-35: 优化**
- [ ] 性能测试
- [ ] 调优权重参数
- [ ] 编写文档

### Phase 5: 记忆生命周期（3-5 天）

**Day 36-37: 权重衰减**
- [ ] 实现 MemoryDecay 类
- [ ] 集成到检索流程

**Day 38-39: 冲突解决**
- [ ] 实现 ConflictResolver 类
- [ ] 集成到记忆写入

**Day 40: 遗忘策略**
- [ ] 实现 MemoryForgettor 类
- [ ] 设置定时任务（每周执行）

### 时间线总结

```
Week 1-2:  工作记忆（P0）
Week 3:    记忆写入（P0）
Week 4:    中期记忆（P1）
Week 5:    智能检索（P1）
Week 6:    记忆生命周期（P2）

总计：6 周
```

---

## 测试策略

### 单元测试

```python
# 工作记忆测试
class TestWorkingMemory:
    def test_create_session():
        session = await session_service.create_session(user_id)
        assert session.id is not None
        assert session.status == 'active'
    
    def test_save_message():
        await conversation_service.save_message(
            session_id, role='user', content='Hello'
        )
        history = await conversation_service.get_history(session_id)
        assert len(history) == 1
        assert history[0].content == 'Hello'
    
    def test_context_window():
        # 添加 20 条消息
        for i in range(20):
            await conversation_service.save_message(...)
        
        # 获取最近 10 条
        context = await context_manager.get_context(session_id, last_n=10)
        assert len(context) == 10

# 记忆写入测试
class TestMemoryExtraction:
    async def test_extract_facts():
        conversation = "用户偏好：喜欢简洁的回答"
        extractor = MemoryExtractor()
        facts = await extractor.extract_from_conversation(conversation)
        assert len(facts) > 0
        assert any('简洁' in f['content'] for f in facts)

# 智能检索测试
class TestSmartRetrieval:
    async def test_fusion():
        retriever = SmartRetriever()
        results = await retriever.retrieve(
            query='合同违约',
            session_id=session_id,
            top_k=5
        )
        assert len(results) == 5
        # 验证包含工作记忆和长期记忆
        sources = [r['source'] for r in results]
        assert 'working_memory' in sources or 'long_term_memory' in sources
```

### 集成测试

```python
class TestEndToEnd:
    async def test_full_workflow():
        # 1. 创建会话
        session = await session_service.create_session(user_id)
        
        # 2. 发送消息
        await conversation_service.save_message(
            session.id, role='user', content='什么是合同违约？'
        )
        
        # 3. 智能检索
        results = await retriever.retrieve('合同违约', session.id)
        
        # 4. 生成响应
        response = await llm.generate(results)
        
        # 5. 保存响应
        await conversation_service.save_message(
            session.id, role='assistant', content=response
        )
        
        # 6. 对话结束，提取记忆
        await memory_extractor.extract_from_conversation(session.id)
        
        # 7. 验证长期记忆已更新
        memories = await es_client.search('long_term_memory', '合同违约')
        assert len(memories) > 0
```

### 性能测试

```python
class TestPerformance:
    async def test_concurrent_sessions():
        # 创建 100 个并发会话
        tasks = [
            session_service.create_session(user_id)
            for _ in range(100)
        ]
        sessions = await asyncio.gather(*tasks)
        assert len(sessions) == 100
    
    async def test_retrieval_latency():
        # 检索延迟 < 100ms
        start = time.time()
        results = await retriever.retrieve('test query', session_id)
        latency = (time.time() - start) * 1000
        assert latency < 100
```

---

## 部署方案

### 数据库迁移

```bash
# 执行迁移
cd backend
alembic upgrade head

# 回滚
alembic downgrade -1
```

### 配置更新

```python
# .env
DATABASE_URL=postgresql://user:pass@localhost/esragpro
REDIS_URL=redis://localhost:6379/0
ELASTICSEARCH_URL=http://localhost:9200

# 记忆系统配置
MEMORY_WORKING_TTL=3600  # 1 小时
MEMORY_CONTEXT_WINDOW=10  # 最近 10 轮
MEMORY_EXTRACTION_ENABLED=true
MEMORY_DECAY_HALF_LIFE=30  # 30 天半衰期
```

### 定时任务

```python
# 每周执行记忆清理
@cron.schedule('0 2 * * 0')  # 每周日凌晨 2 点
async def cleanup_old_memories():
    forgettor = MemoryForgettor()
    await forgettor.cleanup()
```

### 监控指标

```python
# Prometheus 指标
MEMORY_SESSIONS_ACTIVE = Gauge('memory_sessions_active', 'Active sessions')
MEMORY_CONVERSATIONS_TOTAL = Counter('memory_conversations_total', 'Total conversations')
MEMORY_EXTRACTION_COUNT = Counter('memory_extraction_count', 'Memory extractions')
MEMORY_RETRIEVAL_LATENCY = Histogram('memory_retrieval_latency_ms', 'Retrieval latency')
```

---

## 风险和缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **LLM 提取不准确** | 高 | 使用多个模型交叉验证，人工审核 |
| **记忆冲突** | 中 | 实现冲突解决策略，保留多个版本 |
| **性能瓶颈** | 中 | Redis 缓存，批量操作，索引优化 |
| **隐私泄露** | 高 | 数据加密，访问控制，审计日志 |
| **存储膨胀** | 中 | 定期清理，权重衰减，遗忘策略 |

---

## 附录

### A. 相关文档

- [MCP-V1-CAPABILITY-MATRIX.md](./MCP-V1-CAPABILITY-MATRIX.md)
- [ARCHITECTURE.md](./ARCHITECTURE.md)
- [PLAN.md](./PLAN.md)

### B. 参考资料

- [AI Agent 记忆系统 - JavaGuide](https://javaguide.cn/ai/agent/agent-memory.html)
- [Agent Memory Best Practices - AWS](https://aws.amazon.com/cn/blogs/china/agentic-ai-infrastructure-deep-practice-experience-thinking-series-three-best-practices-for-agent-memory-module/)
- [AI Agent 通识教程 - 第5章](https://ai-agent-guide.xiaofuge.cn/)

### C. 术语表

| 术语 | 定义 |
|------|------|
| **工作记忆** | 当前会话的上下文，短期存储 |
| **长期记忆** | 跨会话的持久化知识存储 |
| **记忆提取** | 从对话中提取关键信息的过程 |
| **权重衰减** | 记忆重要性随时间降低的机制 |
| **冲突解决** | 处理新旧记忆矛盾的策略 |

---

**文档版本历史**

| 版本 | 日期 | 变更 | 作者 |
|------|------|------|------|
| 1.0 | 2026-07-13 | 初始版本 | Claude Code |
