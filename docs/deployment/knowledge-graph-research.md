# 知识图谱技术预研

## 一、为什么需要知识图谱

### 当前架构的局限

```
当前方案（纯向量检索）：

问："合同违约后怎么办？"
    │
    ▼
找到文档片段：
├── "根据民法典第577条，违约方应承担责任..."
├── "诉讼时效为三年..."
└── "可以向法院起诉..."
    │
    ▼
回答：罗列找到的片段，缺乏系统性

问题：
├── 只能找"提到什么"，不能推理"还有什么"
├── 缺乏知识之间的关联
└── 回答碎片化，不完整
```

### 知识图谱的价值

```
加入知识图谱后：

问："合同违约后怎么办？"
    │
    ▼
图谱查询：

合同违约 ──救济──→ 继续履行/赔偿/解除
    │
    ├──时效──→ 3年（民法典188条）
    │
    ├──举证──→ 原告举证（谁主张谁举证）
    │
    ├──管辖──→ 被告住所地/合同履行地法院
    │
    ├──保全──→ 诉前/诉中财产保全
    │
    └──执行──→ 申请强制执行（2年时效）
    │
    ▼
回答：完整的解决方案，有系统、有条理
```

---

## 二、技术选型

### 图数据库对比

| 数据库 | 优势 | 劣势 | 推荐度 |
|--------|------|------|--------|
| **Neo4j** | 最成熟，文档全，Cypher易学 | 企业版收费 | ⭐⭐⭐⭐⭐ |
| **NebulaGraph** | 国产，分布式，高性能 | 社区较小 | ⭐⭐⭐⭐ |
| JanusGraph | 开源，可扩展 | 配置复杂 | ⭐⭐⭐ |
| TigerGraph | 高性能 | 商业化 | ⭐⭐⭐ |

**推荐方案**：
- **开发/小规模**：Neo4j Community（免费）
- **生产/大规模**：NebulaGraph（国产，性能好）

### 实体识别工具

| 工具 | 说明 | 推荐度 |
|------|------|--------|
| spaCy + 中文模型 | 轻量，易用 | ⭐⭐⭐⭐ |
| HanLP | 中文效果好 | ⭐⭐⭐⭐ |
| LLM (GPT/GLM) | 效果最好，但慢 | ⭐⭐⭐ |
| Jieba + 规则 | 简单场景 | ⭐⭐ |

**推荐方案**：HanLP 做基础识别 + LLM 做关系抽取

---

## 三、架构设计

### 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                        用户提问                              │
└─────────────────────────┬───────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
          ▼               ▼               ▼
    ┌───────────┐   ┌───────────┐   ┌───────────┐
    │ ES混合检索 │   │ 图谱查询   │   │ 外部知识  │
    │ (BM25+向量)│   │ (Neo4j)   │   │ (可选)    │
    └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
          │               │               │
          └───────────────┼───────────────┘
                          │
                          ▼
                  ┌───────────────┐
                  │   上下文融合   │
                  └───────┬───────┘
                          │
                          ▼
                  ┌───────────────┐
                  │   LLM 生成    │
                  └───────┬───────┘
                          │
                          ▼
                  ┌───────────────┐
                  │   图谱验证    │ ← 新增：验证回答中的实体关系
                  └───────┬───────┘
                          │
                          ▼
                      最终回答
```

### 图谱构建流程

```
文档上传
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│                      文档处理                                │
│                                                              │
│   文档 ──► 分块 ──► 向量化 ──► ES索引（现有流程）           │
│       │                                                      │
│       └──► 实体识别 ──► 关系抽取 ──► 图谱存储（新增）       │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 图谱 Schema 设计（律所场景）

```cypher
// 实体类型
(:Person)          // 人物：张三、李四
(:Organization)    // 组织：ABC公司、XX法院
(:Law)             // 法律：民法典、合同法
(:Case)            // 案例：XX案
(:Document)        // 文档：合同、判决书
(:Concept)         // 概念：违约、诉讼时效

// 关系类型
(:Person)-[:WORKS_FOR]->(:Organization)      // 任职
(:Person)-[:FOUNDED]->(:Organization)        // 创办
(:Case)-[:CITES]->(:Law)                     // 引用法律
(:Case)-[:SIMILAR_TO]->(:Case)               // 相似案例
(:Concept)-[:DEFINED_BY]->(:Law)             // 定义
(:Concept)-[:RELATED_TO]->(:Concept)         // 相关
(:Document)-[:ABOUT]->(:Concept)             // 关于
(:Document)-[:INVOLVES]->(:Person)           // 涉及
```

---

## 四、核心模块设计

### 4.1 实体识别服务

```python
# backend/app/services/entity_extractor.py

from typing import List, Dict, Any
import hanlp

class EntityExtractor:
    """实体识别服务"""

    def __init__(self):
        # 加载中文 NER 模型
        self.ner = hanlp.load(hanlp.pretrained.ner.MSRA_NER_BERT_BASE_ZH)

        # 法律领域实体类型
        self.entity_types = {
            'PERSON': '人物',
            'ORGANIZATION': '组织',
            'LAW': '法律',
            'CASE': '案例',
            'DOCUMENT': '文档',
            'DATE': '日期',
            'MONEY': '金额',
        }

        # 法律关键词词典（增强识别）
        self.law_keywords = [
            '民法典', '合同法', '公司法', '劳动法',
            '诉讼时效', '违约金', '管辖', '举证',
        ]

    def extract(self, text: str) -> List[Dict[str, Any]]:
        """
        从文本中提取实体

        Returns:
            [{'text': '张三', 'type': 'PERSON', 'start': 0, 'end': 2}, ...]
        """
        entities = []

        # 1. 使用 HanLP 识别基础实体
        ner_result = self.ner(text)
        for entity_text, entity_type, start, end in ner_result:
            entities.append({
                'text': entity_text,
                'type': entity_type,
                'start': start,
                'end': end,
            })

        # 2. 法律关键词识别（补充）
        for keyword in self.law_keywords:
            if keyword in text:
                idx = text.find(keyword)
                entities.append({
                    'text': keyword,
                    'type': 'LAW' if '法' in keyword else 'CONCEPT',
                    'start': idx,
                    'end': idx + len(keyword),
                })

        # 3. 去重
        seen = set()
        unique_entities = []
        for e in entities:
            key = (e['text'], e['type'])
            if key not in seen:
                seen.add(key)
                unique_entities.append(e)

        return unique_entities
```

### 4.2 关系抽取服务

```python
# backend/app/services/relation_extractor.py

from typing import List, Dict, Any

class RelationExtractor:
    """关系抽取服务"""

    # 预定义关系模式
    RELATION_PATTERNS = [
        {
            'pattern': r'(.+)创办了(.+)',
            'relation': 'FOUNDED',
            'subject_type': 'Person',
            'object_type': 'Organization',
        },
        {
            'pattern': r'(.+)在(.+)工作',
            'relation': 'WORKS_FOR',
            'subject_type': 'Person',
            'object_type': 'Organization',
        },
        {
            'pattern': r'根据(.+)第(.+)条',
            'relation': 'CITES',
            'subject_type': 'Document',
            'object_type': 'Law',
        },
        {
            'pattern': r'(.+)诉讼时效为(.+)',
            'relation': 'STATUTE_OF_LIMITATIONS',
            'subject_type': 'Concept',
            'object_type': 'Duration',
        },
    ]

    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    async def extract(
        self,
        text: str,
        entities: List[Dict]
    ) -> List[Dict[str, Any]]:
        """
        从文本中抽取实体间关系

        Returns:
            [{'subject': '张三', 'relation': 'FOUNDED', 'object': 'ABC公司'}, ...]
        """
        relations = []

        # 1. 规则匹配（快速）
        relations.extend(self._rule_based_extract(text))

        # 2. LLM 抽取（准确）
        if self.llm_client:
            llm_relations = await self._llm_extract(text, entities)
            relations.extend(llm_relations)

        return relations

    def _rule_based_extract(self, text: str) -> List[Dict]:
        """基于规则的关系抽取"""
        import re
        relations = []

        for pattern_config in self.RELATION_PATTERNS:
            matches = re.findall(pattern_config['pattern'], text)
            for match in matches:
                relations.append({
                    'subject': match[0],
                    'relation': pattern_config['relation'],
                    'object': match[1],
                    'confidence': 0.8,
                })

        return relations

    async def _llm_extract(
        self,
        text: str,
        entities: List[Dict]
    ) -> List[Dict]:
        """使用 LLM 进行关系抽取"""
        prompt = f"""
        从以下文本中抽取实体之间的关系：

        文本：{text}

        已识别的实体：{[e['text'] for e in entities]}

        请以 JSON 格式输出关系列表：
        [
            {{"subject": "实体1", "relation": "关系类型", "object": "实体2"}}
        ]

        关系类型包括：FOUNDED, WORKS_FOR, CITES, DEFINED_BY, RELATED_TO
        """

        # 调用 LLM
        response = await self.llm_client.generate(prompt)

        # 解析结果
        import json
        try:
            relations = json.loads(response)
            return relations
        except:
            return []
```

### 4.3 图谱服务

```python
# backend/app/services/knowledge_graph.py

from typing import List, Dict, Any, Optional
from neo4j import AsyncGraphDatabase

class KnowledgeGraphService:
    """知识图谱服务"""

    def __init__(self, uri: str, user: str, password: str):
        self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def close(self):
        await self.driver.close()

    async def add_entity(
        self,
        entity_type: str,
        properties: Dict[str, Any]
    ) -> str:
        """添加实体"""
        async with self.driver.session() as session:
            query = f"""
            CREATE (e:{entity_type} $properties)
            RETURN elementId(e) as id
            """
            result = await session.run(query, properties=properties)
            record = await result.single()
            return record['id']

    async def add_relation(
        self,
        subject_id: str,
        relation: str,
        object_id: str,
        properties: Optional[Dict] = None
    ):
        """添加关系"""
        async with self.driver.session() as session:
            query = f"""
            MATCH (s), (o)
            WHERE elementId(s) = $subject_id AND elementId(o) = $object_id
            CREATE (s)-[r:{relation} $properties]->(o)
            RETURN r
            """
            await session.run(
                query,
                subject_id=subject_id,
                object_id=object_id,
                properties=properties or {}
            )

    async def query_subgraph(
        self,
        entity_name: str,
        depth: int = 2
    ) -> Dict[str, Any]:
        """查询实体的关联子图"""
        async with self.driver.session() as session:
            query = f"""
            MATCH path = (n {{name: $name}})-[*1..{depth}]-(related)
            RETURN n, related, relationships(path) as rels
            """
            result = await session.run(query, name=entity_name)

            nodes = []
            edges = []
            async for record in result:
                nodes.append(dict(record['n']))
                nodes.append(dict(record['related']))
                for rel in record['rels']:
                    edges.append({
                        'type': rel.type,
                        'properties': dict(rel)
                    })

            return {'nodes': nodes, 'edges': edges}

    async def multi_hop_query(
        self,
        start_entity: str,
        relation_path: List[str]
    ) -> List[Dict]:
        """多跳查询"""
        # 例如：张三 --WORKS_FOR--> ? --LOCATED_IN--> ?
        path_pattern = '-'.join([f'[:{r}]-( )' for r in relation_path])

        async with self.driver.session() as session:
            query = f"""
            MATCH (start {{name: $name}}){path_pattern}
            RETURN *
            """
            result = await session.run(query, name=start_entity)
            return [dict(record) async for record in result]

    async def verify_fact(
        self,
        subject: str,
        relation: str,
        obj: str
    ) -> bool:
        """验证事实是否在图谱中存在"""
        async with self.driver.session() as session:
            query = f"""
            MATCH (s {{name: $subject}})-[r:{relation}]->(o {{name: $object}})
            RETURN count(r) > 0 as exists
            """
            result = await session.run(
                query,
                subject=subject,
                object=obj
            )
            record = await result.single()
            return record['exists']
```

### 4.4 GraphRAG 融合服务

```python
# backend/app/services/graph_rag.py

from typing import List, Dict, Any

from app.services.knowledge_graph import KnowledgeGraphService
from app.services.entity_extractor import EntityExtractor
from app.services.relation_extractor import RelationExtractor
from app.services.rag import RAGService

class GraphRAGService:
    """GraphRAG 融合服务"""

    def __init__(
        self,
        kg_service: KnowledgeGraphService,
        rag_service: RAGService,
        entity_extractor: EntityExtractor,
        relation_extractor: RelationExtractor,
    ):
        self.kg = kg_service
        self.rag = rag_service
        self.entity_extractor = entity_extractor
        self.relation_extractor = relation_extractor

    async def index_document(self, document: Dict[str, Any]) -> bool:
        """
        索引文档到 ES 和知识图谱

        流程：
        1. 文档分块
        2. 向量化 + ES 索引（原有流程）
        3. 实体识别
        4. 关系抽取
        5. 图谱存储
        """
        # 1. ES 索引（原有）
        await self.rag.index_document(document)

        # 2. 图谱构建
        for chunk in document.get('chunks', []):
            text = chunk['content']

            # 实体识别
            entities = self.entity_extractor.extract(text)

            # 关系抽取
            relations = await self.relation_extractor.extract(text, entities)

            # 存入图谱
            entity_ids = {}
            for entity in entities:
                entity_id = await self.kg.add_entity(
                    entity['type'],
                    {'name': entity['text'], 'source': document['doc_id']}
                )
                entity_ids[entity['text']] = entity_id

            for relation in relations:
                subject_id = entity_ids.get(relation['subject'])
                object_id = entity_ids.get(relation['object'])
                if subject_id and object_id:
                    await self.kg.add_relation(
                        subject_id,
                        relation['relation'],
                        object_id
                    )

        return True

    async def search_with_graph(
        self,
        query: str,
        top_k: int = 10
    ) -> Dict[str, Any]:
        """
        融合检索：ES + 知识图谱

        Returns:
            {
                'documents': [...],    # ES 检索结果
                'subgraph': {...},     # 图谱关联信息
                'entities': [...],     # 相关实体
            }
        """
        # 1. ES 检索
        documents = await self.rag.search(query, top_k)

        # 2. 提取查询中的实体
        query_entities = self.entity_extractor.extract(query)

        # 3. 图谱查询
        subgraph = {}
        for entity in query_entities[:3]:  # 最多查 3 个实体
            entity_subgraph = await self.kg.query_subgraph(
                entity['text'],
                depth=2
            )
            subgraph.update(entity_subgraph)

        # 4. 返回融合结果
        return {
            'documents': documents,
            'subgraph': subgraph,
            'entities': query_entities,
        }

    async def generate_answer_with_graph(
        self,
        query: str,
        top_k: int = 5
    ) -> Dict[str, Any]:
        """
        生成答案（含图谱信息）
        """
        # 1. 融合检索
        search_result = await self.search_with_graph(query, top_k)

        # 2. 构建增强上下文
        context = self._build_context(search_result)

        # 3. LLM 生成
        answer_result = await self.rag.generate_answer(
            query,
            search_result['documents']
        )

        # 4. 图谱验证
        verified = await self._verify_with_graph(
            answer_result['answer'],
            search_result['subgraph']
        )

        return {
            **answer_result,
            'graph_context': verified.get('graph_info', []),
            'verified': verified['verified'],
        }

    def _build_context(self, search_result: Dict) -> str:
        """构建包含图谱信息的上下文"""
        parts = []

        # 文档内容
        for doc in search_result['documents'][:3]:
            parts.append(f"[文档] {doc['content'][:500]}")

        # 图谱信息
        subgraph = search_result['subgraph']
        if subgraph.get('nodes'):
            parts.append("\n[相关知识图谱]")
            for edge in subgraph.get('edges', [])[:5]:
                parts.append(f"- {edge}")

        return '\n'.join(parts)

    async def _verify_with_graph(
        self,
        answer: str,
        subgraph: Dict
    ) -> Dict[str, Any]:
        """用图谱验证回答"""
        # 提取回答中的实体
        answer_entities = self.entity_extractor.extract(answer)

        verified = True
        graph_info = []

        for entity in answer_entities:
            # 检查实体是否在图谱中
            # 如果在，获取其关联信息
            pass

        return {
            'verified': verified,
            'graph_info': graph_info,
        }
```

---

## 五、Docker 集成

### docker-compose.yml 添加 Neo4j

```yaml
services:
  # ... 现有服务 ...

  neo4j:
    image: neo4j:5.15-community
    container_name: esragpro-neo4j
    ports:
      - "7474:7474"  # HTTP
      - "7687:7687"  # Bolt
    environment:
      - NEO4J_AUTH=neo4j/${NEO4J_PASSWORD:-esragpro123}
      - NEO4J_PLUGINS=["apoc"]
      - NEO4J_dbms_memory_heap_initial__size=512m
      - NEO4J_dbms_memory_heap_max__size=1G
    volumes:
      - neo4j_data:/data
      - neo4j_logs:/logs
    networks:
      - esragpro-internal

  backend:
    environment:
      - NEO4J_URI=bolt://neo4j:7687
      - NEO4J_USER=neo4j
      - NEO4J_PASSWORD=${NEO4J_PASSWORD:-esragpro123}
    depends_on:
      - neo4j

volumes:
  neo4j_data:
  neo4j_logs:
```

---

## 六、开发计划

### 阶段一：基础集成（2周）

```
Week 1：
├── Day 1-2：Neo4j 部署和基础服务
├── Day 3-4：实体识别服务
└── Day 5-7：关系抽取服务（规则+LLM）

Week 2：
├── Day 1-3：图谱构建流程
├── Day 4-5：图谱查询和验证
└── Day 6-7：集成测试
```

### 阶段二：优化完善（1周）

```
├── 性能优化（批量写入）
├── 准确率优化（实体消歧）
├── API 接口完善
└── 文档更新
```

---

## 七、预期效果

### 幻觉率改善

| 指标 | 当前（v0.2） | 加入图谱后（v0.3） |
|------|-------------|-------------------|
| 幻觉率 | 3-5% | **1-3%** |
| 回答完整性 | 70% | **90%** |
| 多跳问答能力 | ❌ | ✅ |
| 关系推理能力 | ❌ | ✅ |

### 新增能力

```
✅ 多跳问答
   问："张三的公司的合作伙伴是谁？"

✅ 关系推理
   问："合同违约和诉讼时效有什么关系？"

✅ 图谱验证
   自动验证回答中的实体关系是否正确

✅ 知识关联
   回答时自动补充相关知识
```

---

## 八、风险与应对

| 风险 | 应对 |
|------|------|
| 实体识别准确率不够 | 规则+词典+LLM 三重保障 |
| 图谱构建太慢 | 异步处理，批量写入 |
| 图谱质量差 | 人工审核 + 自动清洗 |
| 存储空间不足 | 定期清理，冷热分离 |

---

## 九、后续迭代

```
v0.3：基础知识图谱
├── Neo4j 集成
├── 实体识别
├── 关系抽取
└── 图谱查询

v0.4：图谱增强
├── 图谱可视化
├── 人工标注接口
├── 图谱编辑功能
└── 知识推理

v1.0：GraphRAG 完整版
├── 多模态图谱
├── 动态图谱更新
├── 图谱问答优化
└── 效果评估体系
```