"""
Agentic Router - 智能路由策略选择
根据问题类型自动选择最优检索策略
"""
import re
import logging
from typing import Dict, Any, List
from enum import Enum


logger = logging.getLogger(__name__)


class RetrievalStrategy(str, Enum):
    """检索策略"""
    BM25_ONLY = "bm25_only"           # 仅 BM25（简单关键词查询）
    SPARSE_ONLY = "sparse_only"       # 仅稀疏向量（语义查询）
    HYBRID = "hybrid"                 # BM25 + 稀疏混合
    GRAPH_ENTITY = "graph_entity"     # 知识图谱实体查询
    GRAPH_MULTIHOP = "graph_multihop" # 知识图谱多跳查询
    GRAPH_RAG = "graph_rag"           # GraphRAG 融合
    AGENTIC_RAG = "agentic_rag"       # Agentic RAG（复杂推理）
    CACHE_HIT = "cache_hit"           # 缓存命中（极速）


class QueryType(str, Enum):
    """查询类型"""
    ENTITY = "entity"                 # 实体查询："张三是谁？"
    RELATION = "relation"             # 关系查询："张三和李四什么关系？"
    FACT = "fact"                     # 事实查询："诉讼时效是多久？"
    REASONING = "reasoning"           # 推理性查询："如果合同违约怎么办？"
    MULTI_HOP = "multi_hop"           # 多跳查询："A的公司在哪？"
    SIMPLE = "simple"                 # 简单查询
    COMPLEX = "complex"               # 复杂查询
    OUT_OF_SCOPE = "out_of_scope"     # 超出范围


class AgenticRouter:
    """Agentic 路由器"""

    # 实体查询模式
    ENTITY_PATTERNS = [
        r".*是谁.*",
        r".*什么是.*",
        r".*定义.*",
        r".*介绍一下.*",
    ]

    # 关系查询模式
    RELATION_PATTERNS = [
        r".*什么关系.*",
        r".*关联.*",
        r".*联系.*",
    ]

    # 多跳查询模式
    MULTI_HOP_PATTERNS = [
        r".*的.*的.*",
        r".*的.*在哪.*",
        r".*的.*位于.*",
        r".*公司在哪.*",
        r".*位于哪里.*",
    ]

    # 推理查询模式
    REASONING_PATTERNS = [
        r".*如果.*怎么办.*",
        r".*怎么办.*",
        r".*如何处理.*",
        r".*应该.*",
        r".*如何.*",
        r".*为什么.*",
    ]

    def __init__(self):
        self.query_history: Dict[str, RetrievalStrategy] = {}

    def analyze_query(self, query: str) -> Dict[str, Any]:
        """分析问题，返回推荐策略

        Args:
            query: 用户查询

        Returns:
            {
                "query_type": "fact",
                "strategy": "hybrid",
                "confidence": 0.85,
                "fallback": ["bm25_only", "graph_entity"]
            }
        """
        query_type = self._classify_query(query)
        strategy = self._select_strategy(query_type)
        confidence = self._estimate_confidence(query, query_type)

        return {
            "query_type": query_type.value,
            "strategy": strategy.value,
            "confidence": confidence,
            "fallback": self._get_fallback_strategies(strategy),
        }

    def _classify_query(self, query: str) -> QueryType:
        """分类查询类型"""
        # 实体查询
        if any(re.match(p, query) for p in self.ENTITY_PATTERNS):
            return QueryType.ENTITY

        # 关系查询
        if any(re.match(p, query) for p in self.RELATION_PATTERNS):
            return QueryType.RELATION

        # 多跳查询
        if any(re.match(p, query) for p in self.MULTI_HOP_PATTERNS):
            return QueryType.MULTI_HOP

        # 推理查询
        if any(re.match(p, query) for p in self.REASONING_PATTERNS):
            return QueryType.REASONING

        # 简单事实
        if self._is_simple_fact(query):
            return QueryType.FACT

        # 默认复杂
        return QueryType.COMPLEX

    def _is_simple_fact(self, query: str) -> bool:
        """判断是否是简单事实查询"""
        # 短句、直接问（允许中英文问号）
        cleaned = query.replace("?", "").replace("？", "").strip()
        if len(cleaned) < 20:
            return True
        return False

    def _select_strategy(self, query_type: QueryType) -> RetrievalStrategy:
        """根据查询类型选择检索策略"""
        strategy_map = {
            QueryType.ENTITY: RetrievalStrategy.GRAPH_ENTITY,
            QueryType.RELATION: RetrievalStrategy.GRAPH_MULTIHOP,
            QueryType.FACT: RetrievalStrategy.BM25_ONLY,
            QueryType.REASONING: RetrievalStrategy.AGENTIC_RAG,
            QueryType.MULTI_HOP: RetrievalStrategy.GRAPH_RAG,
            QueryType.SIMPLE: RetrievalStrategy.BM25_ONLY,
            QueryType.COMPLEX: RetrievalStrategy.HYBRID,
        }
        return strategy_map.get(query_type, RetrievalStrategy.HYBRID)

    def _estimate_confidence(self, query: str, query_type: QueryType) -> float:
        """估计策略置信度"""
        # 基于查询长度、复杂度等
        confidence = 0.7

        if query_type == QueryType.FACT:
            confidence = 0.9
        elif query_type == QueryType.ENTITY:
            confidence = 0.85
        elif query_type == QueryType.RELATION:
            confidence = 0.8
        elif query_type == QueryType.REASONING:
            confidence = 0.75
        elif query_type == QueryType.MULTI_HOP:
            confidence = 0.7

        # 查询越长，置信度越低
        if len(query) > 50:
            confidence -= 0.1
        if len(query) > 100:
            confidence -= 0.1

        return max(0.3, min(1.0, confidence))

    def _get_fallback_strategies(self, primary: RetrievalStrategy) -> List[str]:
        """获取备用策略列表"""
        fallback_map = {
            RetrievalStrategy.GRAPH_ENTITY: ["bm25_only", "hybrid"],
            RetrievalStrategy.GRAPH_MULTIHOP: ["graph_rag", "hybrid"],
            RetrievalStrategy.BM25_ONLY: ["sparse_only", "hybrid"],
            RetrievalStrategy.HYBRID: ["bm25_only", "sparse_only"],
            RetrievalStrategy.GRAPH_RAG: ["hybrid", "bm25_only"],
            RetrievalStrategy.AGENTIC_RAG: ["graph_rag", "hybrid"],
        }
        return fallback_map.get(primary, ["hybrid", "bm25_only"])


# 全局实例
agentic_router = AgenticRouter()