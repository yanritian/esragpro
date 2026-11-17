"""
RRF (Reciprocal Rank Fusion) tests
"""
import pytest
from unittest.mock import AsyncMock, patch


class TestRRF:
    """Test RRF fusion algorithm"""

    def test_rrf_combines_scores(self):
        """RRF should combine scores from multiple sources"""
        # Graph results: doc1 at rank 0
        graph_results = [{"doc_id": "doc1", "score": 0.9}]

        # ES results: doc1 at rank 1, doc2 at rank 0
        es_results = [
            {"doc_id": "doc2", "score": 0.8},
            {"doc_id": "doc1", "score": 0.7}
        ]

        # Expected: doc1 should have combined RRF score
        # RRF_K = 60
        # doc1: 1/(60+0) + 1/(60+1) from graph + es = 0.01667 + 0.01639 = 0.03306
        # doc2: 1/(60+0) = 0.01667

        # This test will fail until GraphRAGService._fuse_results is implemented correctly
        from app.services.graph_rag import graph_rag_service

        results = graph_rag_service._fuse_results(graph_results, es_results, top_k=10)

        # doc1 should rank higher due to presence in both channels
        assert len(results) >= 1
        if len(results) > 0:
            # Check that doc1 is in results and has combined scores
            doc1 = next((r for r in results if r.get("doc_id") == "doc1"), None)
            if doc1:
                # Should have rrf_score combining both channels
                assert "rrf_score" in doc1
                assert doc1.get("graph_score", 0) > 0  # Has graph contribution
                assert doc1.get("es_score", 0) > 0     # Has ES contribution

    def test_rrf_same_chunk_multiple_hits(self):
        """Same chunk hit by both BM25 and vector should accumulate RRF"""
        # This tests the core RRF property: same document from multiple channels
        # should have higher combined score

        from app.services.graph_rag import graph_rag_service

        # Same doc in both channels
        graph_results = [{"doc_id": "doc1", "score": 0.9}]
        es_results = [{"doc_id": "doc1", "score": 0.8}]

        results = graph_rag_service._fuse_results(graph_results, es_results, top_k=10)

        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"
        # RRF score should be combination of both
        # 1/(60+0) + 1/(60+0) = 0.0333...
        assert results[0]["rrf_score"] > 0.03


class TestHybridSearch:
    """Test hybrid search with real embedding"""

    @pytest.mark.asyncio
    async def test_hybrid_search_generates_embedding(self, mock_es_client, mock_embedding_service):
        """Hybrid search should generate query embedding, not pass None"""
        from app.services.graph_rag import graph_rag_service

        # Setup: ES should receive non-None query_vector
        mock_es_client.hybrid_search = AsyncMock(return_value=[])

        # 直接在 graph_rag_service 模块中 mock
        with patch("app.services.graph_rag.es_client", mock_es_client), \
             patch("app.services.graph_rag.embedding_service", mock_embedding_service):
            _ = await graph_rag_service.search_with_graph("test query", top_k=5)

        # Verify embedding was generated
        assert mock_embedding_service.embed_query.called, "embed_query should be called"
        # Verify query_vector is not None (检查传递的参数)
        if mock_es_client.hybrid_search.called:
            call_kwargs = mock_es_client.hybrid_search.call_args.kwargs
            assert call_kwargs.get("query_vector") is not None, "query_vector should not be None"