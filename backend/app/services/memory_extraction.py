"""
Agent Memory System - Memory Extraction
从对话中提取关键信息并写入长期记忆
"""
import json
import logging
from typing import List, Dict, Any
from datetime import datetime
from uuid import UUID
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.memory import Conversation
from app.services.memory import ConversationService
from app.core.elasticsearch import es_client
from app.services.embedding import embedding_service

logger = logging.getLogger(__name__)


@dataclass
class ExtractedFact:
    """提取的事实"""
    content: str
    fact_type: str  # preference, fact, decision, lesson
    importance: float  # 0.0 - 1.0
    metadata: Dict[str, Any]


class MemoryExtractor:
    """从对话中提取关键信息"""

    def __init__(self, llm_service):
        self.llm_service = llm_service

    async def extract_from_conversation(
        self,
        session_id: UUID,
        db: AsyncSession,
    ) -> int:
        """
        从对话中提取关键信息并写入长期记忆

        Args:
            session_id: 会话 ID
            db: 数据库会话

        Returns:
            提取的事实数量
        """
        # 1. 获取对话历史
        conv_service = ConversationService(db)
        conversations = await conv_service.get_history(session_id)

        if not conversations:
            logger.info(f"No conversations found for session {session_id}")
            return 0

        # 2. 构建对话文本
        dialogue = self._build_dialogue_text(conversations)

        # 3. 提取关键事实
        facts = await self._extract_key_facts(dialogue)

        if not facts:
            logger.info(f"No facts extracted from session {session_id}")
            return 0

        # 4. 去重和冲突解决
        filtered_facts = await self._deduplicate_and_resolve(facts)

        # 5. 向量化并写入长期记忆
        await self._write_to_long_term_memory(filtered_facts)

        logger.info(f"Extracted {len(filtered_facts)} facts from session {session_id}")
        return len(filtered_facts)

    def _build_dialogue_text(self, conversations: List[Conversation]) -> str:
        """构建对话文本"""
        return "\n".join([
            f"{c.role}: {c.content}"
            for c in conversations
        ])

    async def _extract_key_facts(self, dialogue: str) -> List[ExtractedFact]:
        """使用 LLM 提取关键事实"""
        prompt = f"""从以下对话中提取关键信息：

{dialogue}

提取以下类型的信息：
1. 用户偏好 (preference) - 用户喜欢的做事方式
2. 重要事实 (fact) - 关键信息、数据、状态
3. 决策和结论 (decision) - 做出的决定
4. 经验教训 (lesson) - 学到的东西

返回 JSON 格式：
{{
    "facts": [
        {{
            "content": "提取的内容",
            "type": "preference|fact|decision|lesson",
            "importance": 0.8,
            "metadata": {{"key": "value"}}
        }}
    ]
}}

如果没有重要信息，返回 {{"facts": []}}
"""

        try:
            response = await self.llm_service.generate(
                prompt,
                max_tokens=1000,
                temperature=0.3,
            )

            # 解析 JSON
            facts_data = json.loads(response)
            facts = [
                ExtractedFact(
                    content=fact["content"],
                    fact_type=fact["type"],
                    importance=fact.get("importance", 0.5),
                    metadata=fact.get("metadata", {}),
                )
                for fact in facts_data.get("facts", [])
            ]

            return facts

        except Exception as e:
            logger.error(f"Failed to extract facts: {e}")
            return []

    async def _deduplicate_and_resolve(
        self,
        facts: List[ExtractedFact],
    ) -> List[ExtractedFact]:
        """去重和冲突解决"""
        if not facts:
            return []

        # 1. 向量化所有事实
        contents = [f.content for f in facts]
        embeddings = embedding_service.embed_texts(contents)

        # 2. 查询相似记忆
        similar_memories = []
        for fact, embedding in zip(facts, embeddings):
            similar = await self._find_similar_memories(embedding, threshold=0.85)
            similar_memories.extend(similar)

        # 3. 过滤重复
        filtered = []
        for fact in facts:
            is_duplicate = any(
                self._is_similar(fact.content, mem["content"])
                for mem in similar_memories
            )

            if not is_duplicate:
                filtered.append(fact)
            else:
                # 冲突解决：新事实优先
                logger.debug(f"Skipped duplicate fact: {fact.content[:50]}...")

        return filtered

    async def _find_similar_memories(
        self,
        embedding: List[float],
        threshold: float = 0.85,
    ) -> List[Dict[str, Any]]:
        """查找相似记忆"""
        try:
            results = await es_client.search(
                index="long_term_memory",
                query={
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query_vector, 'embedding') + 1.0",
                            "params": {"query_vector": embedding},
                        },
                    }
                },
                size=5,
            )

            return [
                {
                    "content": hit["_source"]["content"],
                    "score": hit["_score"],
                }
                for hit in results["hits"]["hits"]
                if hit["_score"] >= threshold
            ]

        except Exception as e:
            logger.warning(f"Failed to search similar memories: {e}")
            return []

    def _is_similar(self, text1: str, text2: str, threshold: float = 0.9) -> bool:
        """判断两个文本是否相似（简单版本）"""
        # 简单实现：使用词集交集
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 or not words2:
            return False

        intersection = words1 & words2
        union = words1 | words2

        similarity = len(intersection) / len(union)
        return similarity >= threshold

    async def _write_to_long_term_memory(
        self,
        facts: List[ExtractedFact],
    ) -> None:
        """写入长期记忆到 Elasticsearch"""
        if not facts:
            return

        # 1. 向量化
        contents = [f.content for f in facts]
        embeddings = embedding_service.embed_texts(contents)

        # 2. 构建文档
        documents = []
        for fact, embedding in zip(facts, embeddings):
            documents.append({
                "content": fact.content,
                "fact_type": fact.fact_type,
                "importance": fact.importance,
                "embedding": embedding,
                "timestamp": datetime.utcnow().isoformat(),
                "metadata": fact.metadata,
            })

        # 3. 批量写入
        try:
            await es_client.bulk_index("long_term_memory", documents)
            logger.info(f"Written {len(documents)} facts to long-term memory")
        except Exception as e:
            logger.error(f"Failed to write to long-term memory: {e}")
            raise


# 全局实例
memory_extractor = None


def get_memory_extractor(llm_service) -> MemoryExtractor:
    """获取记忆提取器实例"""
    global memory_extractor
    if memory_extractor is None:
        memory_extractor = MemoryExtractor(llm_service)
    return memory_extractor
