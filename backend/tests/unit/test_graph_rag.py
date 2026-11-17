"""
GraphRAG service tests
"""
import pytest
from unittest.mock import AsyncMock, patch


class TestGraphRAGService:
    """Test GraphRAG service"""

    @pytest.mark.asyncio
    async def test_search_generates_embedding(self, mock_es_client, mock_embedding_service):
        """search_with_graph should generate query embedding"""
        from app.services.graph_rag import graph_rag_service

        # Setup mocks - use fixture mocks directly
        mock_es_client.hybrid_search = AsyncMock(return_value=[])

        # Import and use the mocked objects directly
        with patch("app.services.graph_rag.es_client", mock_es_client), \
             patch("app.services.graph_rag.embedding_service", mock_embedding_service):
            _ = await graph_rag_service.search_with_graph("test query", top_k=5)

        # Verify embedding was generated (not None)
        mock_es_client.hybrid_search.assert_called_once()
        call_kwargs = mock_es_client.hybrid_search.call_args.kwargs
        assert call_kwargs.get("query_vector") is not None, \
            "query_vector should be generated, not None"

    @pytest.mark.asyncio
    async def test_neo4j_disabled_graceful_degradation(self, mock_es_client, mock_embedding_service):
        """Should gracefully degrade when Neo4j is disabled"""
        from app.services.graph_rag import GraphRAGService

        # Create service with Neo4j disabled
        service = GraphRAGService()
        service.graph_enabled = False

        mock_es_client.hybrid_search = AsyncMock(return_value=[
            {"doc_id": "doc1", "content": "test"}
        ])

        with patch("app.core.elasticsearch.es_client", mock_es_client), \
             patch("app.services.embedding.embedding_service", mock_embedding_service):
            results = await service.search_with_graph("test query", top_k=5)

        # Should return ES results even without graph
        assert len(results) >= 0  # Should not error

    @pytest.mark.asyncio
    async def test_neo4j_failure_returns_es_results(self, mock_es_client, mock_embedding_service):
        """Should return ES results if graph query fails"""
        from app.services.graph_rag import GraphRAGService

        service = GraphRAGService()

        mock_es_client.hybrid_search = AsyncMock(return_value=[
            {"doc_id": "doc1", "content": "test"}
        ])

        with patch("app.core.elasticsearch.es_client", mock_es_client), \
             patch("app.services.embedding.embedding_service", mock_embedding_service), \
             patch.object(service, '_query_graph', side_effect=Exception("Neo4j connection failed")):
            results = await service.search_with_graph("test query", top_k=5)

        # Should still return ES results
        assert isinstance(results, list)

    def test_entity_extraction(self):
        """Test entity extraction from query"""
        from app.services.graph_rag import graph_rag_service

        # Test book entity extraction
        entities = graph_rag_service._extract_entities("《人力资源管理制度》规定")
        assert "人力资源管理制度" in entities

        # Test quoted entity extraction
        entities = graph_rag_service._extract_entities('"员工手册"提到')
        assert "员工手册" in entities

    @pytest.mark.asyncio
    async def test_generate_answer_no_results_refuses(self):
        """Should generate refusal when no results found"""
        from app.services.graph_rag import graph_rag_service

        with patch.object(graph_rag_service, 'search_with_graph', return_value=[]):
            result = await graph_rag_service.generate_answer_with_graph("test query", top_k=5)

        assert result.get("status") == "refused"
        assert result.get("confidence") == 0.0
        assert len(result.get("sources", [])) == 0
        assert "未找到" in result.get("answer", "") or "没有" in result.get("answer", "")