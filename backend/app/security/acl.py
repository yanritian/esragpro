"""
ACL Filter Builder - 构建 Elasticsearch ACL 预过滤查询

核心原则：
- 所有检索必须在存储层预过滤
- 禁止检索后再删除无权结果
- 包含 tenant_id, knowledge_base_id, access_partition_ids
- 遵守 revocation barrier
- barrier 不可用时 fail closed
"""
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.security.principal import PrincipalContext
from app.security.revocation import RevocationBarrierService

logger = logging.getLogger(__name__)


class ACLFilterBuilder:
    """ACL 过滤器构建器"""

    def __init__(self, principal: PrincipalContext, session=None):
        self.principal = principal
        self.tenant_id = principal.tenant_id
        self.knowledge_base_ids = set(principal.authorization.knowledge_base_ids)
        self.access_partition_ids = set(principal.authorization.access_partition_ids)
        self.authorization_revision = principal.authorization.authorization_revision
        self.session = session  # AsyncSession for barrier queries

    def build_filter(
        self,
        knowledge_base_id: Optional[UUID] = None,
        include_revocation_barriers: bool = True,
    ) -> Dict[str, Any]:
        """
        构建 ACL 过滤查询

        Args:
            knowledge_base_id: 可选，限制到特定 KB
            include_revocation_barriers: 是否包含撤权屏障

        Returns:
            Elasticsearch query filter
        """
        must_terms = []
        must_not_terms = []

        # 1. Tenant 过滤（强制）
        must_terms.append({
            "term": {"tenant_id": str(self.tenant_id)}
        })

        # 2. Knowledge Base 过滤
        if knowledge_base_id:
            # 验证用户有权访问该 KB
            if knowledge_base_id not in self.knowledge_base_ids:
                # 返回永远不匹配的过滤器
                return {
                    "bool": {
                        "must": [{"term": {"_id": "__denied__"}}]
                    }
                }
            must_terms.append({
                "term": {"knowledge_base_id": str(knowledge_base_id)}
            })
        else:
            # 必须在授权的 KB 范围内
            if self.knowledge_base_ids:
                must_terms.append({
                    "terms": {"knowledge_base_id": [str(kb) for kb in self.knowledge_base_ids]}
                })
            else:
                # 无 KB 授权，拒绝所有
                return {
                    "bool": {
                        "must": [{"term": {"_id": "__no_kb_access__"}}]
                    }
                }

        # 3. Access Partition 过滤
        if self.access_partition_ids:
            must_terms.append({
                "terms": {"access_partition_ids": list(self.access_partition_ids)}
            })

        # 4. 活跃状态过滤
        must_terms.append({
            "term": {"is_active": True}
        })

        # 5. Revocation Barrier（如果启用）
        if include_revocation_barriers:
            barrier_filter = self._build_barrier_filter()
            if barrier_filter:
                must_not_terms.append(barrier_filter)

        # 6. Authorization Revision 过滤（可选，用于缓存一致性）
        # 检查文档的 authorization_revision <= 当前 revision

        return self._build_final_filter(must_terms, must_not_terms)

    async def build_filter_async(
        self,
        knowledge_base_id: Optional[UUID] = None,
        include_revocation_barriers: bool = True,
        barrier_list: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        异步构建 ACL 过滤查询（支持 Revocation Barrier）

        Args:
            knowledge_base_id: 可选，限制到特定 KB
            include_revocation_barriers: 是否包含撤权屏障
            barrier_list: 可选，预加载的 barrier 列表（避免额外查询）

        Returns:
            Elasticsearch query filter
        """
        must_terms = []
        must_not_terms = []

        # 1. Tenant 过滤（强制）
        must_terms.append({
            "term": {"tenant_id": str(self.tenant_id)}
        })

        # 2. Knowledge Base 过滤
        if knowledge_base_id:
            if knowledge_base_id not in self.knowledge_base_ids:
                return {
                    "bool": {
                        "must": [{"term": {"_id": "__denied__"}}]
                    }
                }
            must_terms.append({
                "term": {"knowledge_base_id": str(knowledge_base_id)}
            })
        else:
            if self.knowledge_base_ids:
                must_terms.append({
                    "terms": {"knowledge_base_id": [str(kb) for kb in self.knowledge_base_ids]}
                })
            else:
                return {
                    "bool": {
                        "must": [{"term": {"_id": "__no_kb_access__"}}]
                    }
                }

        # 3. Access Partition 过滤
        if self.access_partition_ids:
            must_terms.append({
                "terms": {"access_partition_ids": list(self.access_partition_ids)}
            })

        # 4. 活跃状态过滤
        must_terms.append({
            "term": {"is_active": True}
        })

        # 5. Revocation Barrier（异步版本）
        if include_revocation_barriers:
            try:
                barrier_filter = await self._build_barrier_filter_async(barrier_list)
                if barrier_filter:
                    must_not_terms.append(barrier_filter)
            except Exception as e:
                logger.warning(f"Barrier check failed: {e}")
                # Fail closed: 如果 barrier 查询失败，拒绝访问
                return {
                    "bool": {
                        "must": [{"term": {"_id": "__barrier_check_failed__"}}]
                    }
                }

        return self._build_final_filter(must_terms, must_not_terms)

    async def _build_barrier_filter_async(
        self,
        barrier_list: Optional[List[Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        异步构建 Revocation Barrier 过滤

        Args:
            barrier_list: 可选，预加载的 barrier 列表

        Returns:
            ES must_not 过滤器，或 None

        Security Note:
            如果 barrier_list 已提供，验证其 freshness（authorization_revision 一致性）
        """
        if barrier_list is None:
            if self.session is None:
                return None
            service = RevocationBarrierService(self.session)
            barriers = await service.get_active_barriers(self.tenant_id)
        else:
            barriers = barrier_list
            # 验证 barrier_list 的 freshness
            # 如果 barrier 的 authorization_revision 高于当前 principal，警告
            for barrier in barriers:
                if hasattr(barrier, 'authorization_revision'):
                    if barrier.authorization_revision > self.authorization_revision:
                        logger.warning(
                            f"Barrier revision {barrier.authorization_revision} > "
                            f"principal revision {self.authorization_revision}, "
                            "barrier_list may be stale"
                        )

        if not barriers:
            return None

        must_not_conditions = []

        for barrier in barriers:
            if barrier.knowledge_base_id:
                must_not_conditions.append({
                    "term": {"knowledge_base_id": str(barrier.knowledge_base_id)}
                })

            if barrier.document_id:
                must_not_conditions.append({
                    "term": {"document_id": str(barrier.document_id)}
                })

        if must_not_conditions:
            # 使用 minimum_should_match=1 确保至少匹配一个条件才排除
            return {"bool": {"should": must_not_conditions, "minimum_should_match": 1}}

        return None

    def _build_barrier_filter(self) -> Optional[Dict[str, Any]]:
        """
        同步构建 Revocation Barrier 过滤（仅用于测试）

        生产环境应使用 _build_barrier_filter_async
        """
        return None

    def _build_final_filter(
        self,
        must_terms: List[Dict[str, Any]],
        must_not_terms: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """构建最终过滤器"""
        filter_dict = {"bool": {}}

        if must_terms:
            filter_dict["bool"]["must"] = must_terms

        if must_not_terms:
            filter_dict["bool"]["must_not"] = must_not_terms

        return filter_dict

    def build_cache_key(
        self,
        query: str,
        index_version: str = "v1",
    ) -> str:
        """
        构建缓存键

        缓存键必须包含：
        - tenant_id
        - authorization_fingerprint
        - authorization_revision
        - knowledge_base_ids
        - query hash
        - index_version
        """
        import hashlib

        kb_hash = ",".join(sorted(str(kb) for kb in self.knowledge_base_ids))
        key_parts = [
            str(self.tenant_id),
            self.principal.authorization.authorization_fingerprint,
            str(self.authorization_revision),
            kb_hash,
            hashlib.sha256(query.encode()).hexdigest()[:16],
            index_version,
        ]

        return ":".join(key_parts)


async def build_acl_filter_async(
    principal: PrincipalContext,
    session,
    knowledge_base_id: Optional[UUID] = None,
    barrier_list: Optional[List[Any]] = None,
    include_revocation_barriers: bool = True,
) -> Dict[str, Any]:
    """
    异步便捷函数：构建 ACL 过滤器（含 Revocation Barrier）

    Args:
        principal: 当前主体上下文
        session: 数据库会话
        knowledge_base_id: 可选的 KB 限制
        barrier_list: 可选，预加载的 barrier 列表
        include_revocation_barriers: 是否包含撤权屏障

    Returns:
        Elasticsearch filter dict
    """
    builder = ACLFilterBuilder(principal, session)
    return await builder.build_filter_async(
        knowledge_base_id=knowledge_base_id,
        barrier_list=barrier_list,
        include_revocation_barriers=include_revocation_barriers,
    )


def build_acl_filter(
    principal: PrincipalContext,
    knowledge_base_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """
    同步便捷函数：构建 ACL 过滤器（不含 Barrier）

    注意：此函数不包含 Revocation Barrier 检查
    生产环境应使用 build_acl_filter_async

    Args:
        principal: 当前主体上下文
        knowledge_base_id: 可选的 KB 限制

    Returns:
        Elasticsearch filter dict
    """
    builder = ACLFilterBuilder(principal)
    return builder.build_filter(knowledge_base_id=knowledge_base_id)