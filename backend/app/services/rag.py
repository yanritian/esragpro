"""
RAG Service - Retrieval Augmented Generation
支持多种 LLM 提供商：OpenAI、智谱AI、通义千问、月之暗面等
包含幻觉防护：强制引用、事实核查、拒绝回答机制
"""
import logging
import httpx
from typing import Any, Dict, List

from app.core.config import settings
from app.services.embedding import embedding_service
from app.services.hallucination_guard import (
    hallucination_guard,
    AnswerStatus,
    VerifiedAnswer
)
from app.core.elasticsearch import es_client

logger = logging.getLogger(__name__)


class RAGService:
    """RAG service for retrieval and generation with hallucination protection"""

    def __init__(self):
        self.strict_mode = True  # 严格模式：移除无法验证的内容

    async def index_document(
        self,
        document: Dict[str, Any],
    ) -> bool:
        """
        Index a document with all its chunks

        Args:
            document: Document dictionary from document_processor

        Returns:
            Success status
        """
        chunks = document.get("chunks", [])
        if not chunks:
            return False

        # Embed all chunks
        texts = [chunk["content"] for chunk in chunks]
        embeddings = embedding_service.embed_texts(texts)

        # Prepare documents for indexing
        docs_to_index = []
        for chunk, embedding in zip(chunks, embeddings):
            doc = {
                "doc_id": f"{document['doc_id']}_{chunk['chunk_index']}",
                "parent_doc_id": document["doc_id"],
                "title": document["title"],
                "content": chunk["content"],
                "content_embedding": embedding,
                "doc_type": document["doc_type"],
                "source": document["source"],
                "file_path": document["file_path"],
                "created_at": document["created_at"],
                "tenant_id": document.get("tenant_id"),
                "knowledge_base_id": document.get("knowledge_base_id"),
                "access_partition_ids": document.get("access_partition_ids", []),
                "is_active": document.get("is_active", True),
                "metadata": {
                    **document.get("metadata", {}),
                    "chunk_index": chunk["chunk_index"],
                    "start_char": chunk["start_char"],
                    "end_char": chunk["end_char"],
                },
            }
            docs_to_index.append(doc)

        # Bulk index
        success, failed = await es_client.bulk_index(
            settings.ES_INDEX_NAME,
            docs_to_index,
        )

        return failed == 0

    async def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search with RRF fusion

        Args:
            query: Search query
            top_k: Number of results to return

        Returns:
            List of search results with traceability info
        """
        # Embed query
        query_vector = embedding_service.embed_query(query)

        # Hybrid search
        results = await es_client.hybrid_search(
            index_name=settings.ES_INDEX_NAME,
            query=query,
            query_vector=query_vector,
            size=top_k,
        )

        # Add confidence level
        for result in results:
            result["confidence_level"] = self._get_confidence_level(
                result["rrf_score"]
            )

        return results

    def _get_confidence_level(self, score: float) -> str:
        """Convert RRF score to confidence level"""
        if score >= 0.05:
            return "非常确定"
        elif score >= 0.03:
            return "基本确定"
        else:
            return "仅供参考"

    async def generate_answer(
        self,
        query: str,
        context_docs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Generate answer using LLM with hallucination protection

        包含四层防护：
        0. 提示词注入防护 - P0 #3 新增
        1. 安全提示词 - 强调必须基于文档
        2. 事实核查 - 验证每个声明
        3. 拒绝机制 - 无信息时拒绝回答
        """
        # === P0 #3: 第零层防护 - 提示词注入检查 ===
        from app.guardrails.injection import check_injection, InjectionRisk

        injection_result = check_injection(query)

        if injection_result.risk_level == InjectionRisk.CRITICAL:
            # 严重注入尝试，直接拒绝
            return {
                "query": query,
                "answer": "检测到异常输入模式，请重新表述您的问题。",
                "sources": [],
                "confidence": 0.0,
                "status": "refused",
                "warnings": ["检测到潜在的提示词注入攻击"],
            }

        # 高风险查询，使用清理后的内容
        safe_query = injection_result.sanitized_content
        if injection_result.was_modified:
            logger.warning(f"Query sanitized due to injection risk: {injection_result.detected_patterns}")

        # 构建上下文
        context_parts = []
        sources = []

        for i, doc in enumerate(context_docs[:5], 1):
            context_parts.append(f"[文档{i}：{doc['title']}]\n{doc['content']}")
            sources.append({
                "doc_id": doc["doc_id"],
                "title": doc["title"],
                "source": doc["source"],
                "content_snippet": doc["content"][:300] + "..." if len(doc["content"]) > 300 else doc["content"],
                "rrf_score": doc["rrf_score"],
                "confidence_level": doc["confidence_level"],
            })

        context = "\n\n".join(context_parts)

        # === 第一层防护：安全提示词 ===
        safe_prompt = hallucination_guard.build_safe_prompt(safe_query, context)

        # 调用 LLM
        try:
            raw_answer = await self._call_llm_with_prompt(query, safe_prompt)
        except Exception as e:
            # LLM 调用失败，生成拒绝回答
            return {
                "query": query,
                "answer": hallucination_guard.generate_refusal(query, "no_info"),
                "sources": [],
                "confidence": 0.0,
                "status": "error",
                "warnings": [f"LLM 服务错误: {str(e)}"],
            }

        # === 第二层防护：事实核查 ===
        verified: VerifiedAnswer = hallucination_guard.verify_answer(
            raw_answer, sources, context
        )

        # === 第三层防护：状态判断 ===
        # 如果置信度过低，替换为拒绝回答
        if verified.status in [AnswerStatus.OUT_OF_SCOPE, AnswerStatus.HALLUCINATED]:
            final_answer = hallucination_guard.generate_refusal(query)
            confidence = 0.0
            status = "refused"
        elif verified.status == AnswerStatus.UNCERTAIN:
            # 不确定时添加警告
            final_answer = verified.answer + "\n\n⚠️ 注意：此回答信息来源有限，建议查阅原文确认。"
            confidence = verified.confidence
            status = "uncertain"
        else:
            final_answer = verified.answer
            confidence = verified.confidence
            status = verified.status.value

        # 构建响应
        response = {
            "query": query,
            "answer": final_answer,
            "sources": sources[:3],  # 最多返回 3 个来源
            "confidence": round(confidence, 2),
            "status": status,
            "context_used": context[:500] + "...",
        }

        # 添加警告信息
        if verified.warnings:
            response["warnings"] = verified.warnings

        # 如果有移除的内容，记录下来
        if verified.removed_claims and self.strict_mode:
            response["removed_unverified_content"] = True

        return response

    async def _call_llm_with_prompt(self, query: str, prompt: str) -> str:
        """使用安全提示词调用 LLM"""
        provider = settings.LLM_PROVIDER.lower() if settings.LLM_PROVIDER else "openai"

        messages = [{"role": "user", "content": prompt}]

        if provider == "ollama":
            # Ollama 本地模型（OpenAI 兼容格式）
            return await self._call_openai_compatible(messages, provider)
        elif provider == "anthropic":
            # 阿里云百炼 Coding Plan (Anthropic 兼容格式)
            return await self._call_anthropic(messages)
        elif provider == "zhipu":
            return await self._call_zhipu(messages)
        elif provider == "deepseek":
            return await self._call_openai_compatible(messages, provider)
        elif provider in ["qwen", "tongyi"]:
            return await self._call_openai_compatible(messages, provider)
        elif provider == "moonshot":
            return await self._call_openai_compatible(messages, provider)
        elif provider == "openai":
            return await self._call_openai_compatible(messages, provider)
        else:
            return self._template_response(query, "")

    async def _call_llm(self, query: str, context: str) -> str:
        """调用 LLM（兼容旧接口）"""
        safe_prompt = hallucination_guard.build_safe_prompt(query, context)
        return await self._call_llm_with_prompt(query, safe_prompt)

    async def _call_anthropic(self, messages: List[Dict[str, Any]]) -> str:
        """阿里云百炼 Coding Plan (Anthropic 兼容格式)"""
        api_key = settings.LLM_API_KEY
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")

        base_url = settings.LLM_BASE_URL or "https://coding.dashscope.aliyuncs.com/apps/anthropic"
        model = settings.LLM_MODEL or "qwen3.6-plus"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model,
                    "max_tokens": 1500,
                    "messages": messages,
                }
            )

            if response.status_code == 200:
                data = response.json()
                # Anthropic 格式返回 content 数组
                content = data.get("content", [])
                if content and len(content) > 0:
                    return content[0].get("text", "")
                return ""
            else:
                raise Exception(f"Coding Plan API错误: {response.status_code} - {response.text}")

    async def _call_zhipu(self, messages: List[Dict[str, Any]]) -> str:
        """智谱 AI API (GLM-4)"""
        api_key = settings.LLM_API_KEY
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")

        model = settings.LLM_MODEL or "glm-4-flash"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,  # 降低温度，减少随机性
                    "max_tokens": 1500
                }
            )

            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"]
            else:
                raise Exception(f"智谱AI API错误: {response.status_code} - {response.text}")

    async def _call_openai_compatible(self, messages: List[Dict[str, Any]], provider: str) -> str:
        """OpenAI 兼容格式 API"""
        api_key = settings.LLM_API_KEY
        # Ollama 不需要 API Key
        if not api_key and provider != "ollama":
            raise ValueError("LLM_API_KEY 未配置")

        base_urls = {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "tongyi": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "moonshot": "https://api.moonshot.cn/v1",
            "ollama": "http://localhost:11434/v1",
        }

        base_url = settings.LLM_BASE_URL or base_urls.get(provider, "https://api.openai.com/v1")

        default_models = {
            "openai": "gpt-4o-mini",
            "deepseek": "deepseek-chat",
            "qwen": "qwen-turbo",
            "tongyi": "qwen-turbo",
            "moonshot": "moonshot-v1-8k",
            "ollama": "qwen2.5:7b",
        }
        model = settings.LLM_MODEL or default_models.get(provider, "gpt-3.5-turbo")

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 1500
                }
            )

            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"]
            else:
                raise Exception(f"LLM API错误: {response.status_code} - {response.text}")

    def _template_response(self, query: str, context: str) -> str:
        """模板响应（备用）"""
        return hallucination_guard.generate_refusal(query)


# Global RAG service instance
rag_service = RAGService()