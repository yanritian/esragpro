"""
Agent Memory System - Memory Extraction Tests
记忆提取单元测试
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.memory_extraction import MemoryExtractor, ExtractedFact


class TestMemoryExtractor:
    """记忆提取器测试"""

    def test_build_dialogue_text(self):
        """测试构建对话文本"""
        extractor = MemoryExtractor(llm_service=MagicMock())

        # 创建模拟对话
        conversations = [
            MagicMock(role='user', content='Hello'),
            MagicMock(role='assistant', content='Hi there'),
            MagicMock(role='user', content='How are you?'),
        ]

        dialogue = extractor._build_dialogue_text(conversations)

        assert 'user: Hello' in dialogue
        assert 'assistant: Hi there' in dialogue
        assert 'user: How are you?' in dialogue

    @pytest.mark.asyncio
    async def test_extract_key_facts(self):
        """测试提取关键事实"""
        # Mock LLM 服务
        llm_service = MagicMock()
        llm_service.generate = AsyncMock(return_value="""
{
    "facts": [
        {
            "content": "用户喜欢简洁的回答",
            "type": "preference",
            "importance": 0.8,
            "metadata": {}
        },
        {
            "content": "项目截止日期是下周五",
            "type": "fact",
            "importance": 0.9,
            "metadata": {}
        }
    ]
}
""")

        extractor = MemoryExtractor(llm_service=llm_service)

        dialogue = "user: 我喜欢简洁的回答\nassistant: 好的\nuser: 项目截止日期是下周五"

        facts = await extractor._extract_key_facts(dialogue)

        assert len(facts) == 2
        assert facts[0].content == "用户喜欢简洁的回答"
        assert facts[0].fact_type == "preference"
        assert facts[0].importance == 0.8
        assert facts[1].content == "项目截止日期是下周五"
        assert facts[1].fact_type == "fact"

    @pytest.mark.asyncio
    async def test_extract_key_facts_empty(self):
        """测试提取空事实"""
        llm_service = MagicMock()
        llm_service.generate = AsyncMock(return_value='{"facts": []}')

        extractor = MemoryExtractor(llm_service=llm_service)

        facts = await extractor._extract_key_facts("some dialogue")

        assert len(facts) == 0

    @pytest.mark.asyncio
    async def test_extract_key_facts_error(self):
        """测试提取失败处理"""
        llm_service = MagicMock()
        llm_service.generate = AsyncMock(side_effect=Exception("LLM error"))

        extractor = MemoryExtractor(llm_service=llm_service)

        facts = await extractor._extract_key_facts("some dialogue")

        assert len(facts) == 0

    def test_is_similar(self):
        """测试相似度判断"""
        extractor = MemoryExtractor(llm_service=MagicMock())

        # 相似文本（英文，词集方法更有效）
        assert extractor._is_similar(
            "user likes simple answers",
            "user likes simple and clear answers",
            threshold=0.5
        ) is True

        # 不相似文本
        assert extractor._is_similar(
            "user likes simple answers",
            "the weather is nice today",
            threshold=0.5
        ) is False

    @pytest.mark.asyncio
    async def test_deduplicate_and_resolve(self):
        """测试去重和冲突解决"""
        llm_service = MagicMock()
        extractor = MemoryExtractor(llm_service=llm_service)

        facts = [
            ExtractedFact(
                content="用户喜欢简洁的回答",
                fact_type="preference",
                importance=0.8,
                metadata={},
            ),
            ExtractedFact(
                content="项目截止日期是下周五",
                fact_type="fact",
                importance=0.9,
                metadata={},
            ),
        ]

        # Mock 相似记忆查询
        with patch.object(extractor, '_find_similar_memories', return_value=[]):
            filtered = await extractor._deduplicate_and_resolve(facts)

        assert len(filtered) == 2

    @pytest.mark.asyncio
    async def test_write_to_long_term_memory(self):
        """测试写入长期记忆"""
        llm_service = MagicMock()
        extractor = MemoryExtractor(llm_service=llm_service)

        facts = [
            ExtractedFact(
                content="测试事实",
                fact_type="fact",
                importance=0.8,
                metadata={},
            ),
        ]

        # Mock Elasticsearch
        with patch('app.services.memory_extraction.es_client') as mock_es:
            mock_es.bulk_index = AsyncMock()

            with patch('app.services.memory_extraction.embedding_service') as mock_embed:
                mock_embed.embed_texts = MagicMock(return_value=[[0.1] * 1024])

                await extractor._write_to_long_term_memory(facts)

                # 验证调用
                mock_es.bulk_index.assert_called_once()
                call_args = mock_es.bulk_index.call_args
                assert call_args[0][0] == "long_term_memory"
                assert len(call_args[0][1]) == 1


class TestExtractedFact:
    """提取事实数据类测试"""

    def test_create_fact(self):
        """测试创建事实"""
        fact = ExtractedFact(
            content="用户喜欢简洁的回答",
            fact_type="preference",
            importance=0.8,
            metadata={"source": "conversation"},
        )

        assert fact.content == "用户喜欢简洁的回答"
        assert fact.fact_type == "preference"
        assert fact.importance == 0.8
        assert fact.metadata == {"source": "conversation"}

    def test_fact_types(self):
        """测试事实类型"""
        types = ["preference", "fact", "decision", "lesson"]

        for fact_type in types:
            fact = ExtractedFact(
                content="test",
                fact_type=fact_type,
                importance=0.5,
                metadata={},
            )
            assert fact.fact_type == fact_type
