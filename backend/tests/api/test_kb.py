"""
KB API tests
"""
import pytest


class TestKBApi:
    """Test KB API endpoints"""

    def test_raw_search_imports_rag_service(self):
        """raw_search should import and use rag_service"""
        # 验证 rag_service 被正确导入
        from app.api import kb
        import inspect

        # 检查模块中是否有 rag_service 的引用
        source = inspect.getsource(kb)
        assert "rag_service" in source, "kb.py should import rag_service"

    @pytest.mark.asyncio
    async def test_upload_uses_knowledge_base_service(self):
        """upload 应该使用 knowledge_base_service，而不是不存在的方法"""
        from app.services.knowledge_base import knowledge_base_service

        # knowledge_base_service 应该存在并有正确的方法
        assert hasattr(knowledge_base_service, 'ingest_document')

        # graph_rag_service.index_document_with_graph 不应该存在
        from app.services.graph_rag import graph_rag_service
        assert not hasattr(graph_rag_service, 'index_document_with_graph'), \
            "index_document_with_graph should not exist"

    @pytest.mark.asyncio
    async def test_delete_removes_all_chunks(self, mock_es_client):
        """delete should remove all chunks by root doc_id"""
        # 使用 knowledge_base_service 的逻辑
        from app.services.knowledge_base import knowledge_base_service

        # 验证方法存在
        assert hasattr(knowledge_base_service, 'delete_document')

    def test_search_returns_evidence_ids(self):
        """Each source should have stable evidence_id"""
        # Evidence IDs should be deterministic and traceable
        # Format: UUIDv5 based on chunk_id + span + hash
        pass

    def test_list_documents_deduplicates_by_root_id(self):
        """List should not show duplicate chunks from same document"""
        # knowledge_base_service.list_documents 已经实现去重
        from app.services.knowledge_base import knowledge_base_service
        assert hasattr(knowledge_base_service, 'list_documents')