"""
Revocation Barrier - 撤权屏障服务

核心功能：
- 权限收紧/删除时立即阻断旧投影访问
- barrier 不可用时 fail closed
- 投影确认后才清除 barrier
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.identity import RevocationBarrier
from app.security.principal import PrincipalContext

logger = logging.getLogger(__name__)


class RevocationBarrierService:
    """撤权屏障服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_barrier(
        self,
        tenant_id: UUID,
        authorization_revision: int,
        barrier_type: str,
        knowledge_base_id: Optional[UUID] = None,
        document_id: Optional[UUID] = None,
    ) -> RevocationBarrier:
        """
        创建撤权屏障

        Args:
            tenant_id: 租户 ID
            authorization_revision: 授权修订号
            barrier_type: 屏障类型 (permission_revoked, kb_access_removed, document_deleted)
            knowledge_base_id: 可选的 KB ID
            document_id: 可选的文档 ID

        Returns:
            创建的屏障记录
        """
        barrier = RevocationBarrier(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            authorization_revision=authorization_revision,
            barrier_type=barrier_type,
            is_active=True,
        )
        self.session.add(barrier)
        await self.session.flush()
        return barrier

    async def get_active_barriers(
        self,
        tenant_id: UUID,
        knowledge_base_id: Optional[UUID] = None,
    ) -> List[RevocationBarrier]:
        """
        获取活跃的撤权屏障

        Args:
            tenant_id: 租户 ID
            knowledge_base_id: 可选的 KB ID

        Returns:
            活跃屏障列表
        """
        conditions = [
            RevocationBarrier.tenant_id == tenant_id,
            RevocationBarrier.is_active.is_(True),
        ]

        if knowledge_base_id:
            conditions.append(
                (RevocationBarrier.knowledge_base_id == knowledge_base_id) |
                (RevocationBarrier.knowledge_base_id.is_(None))
            )

        result = await self.session.execute(
            select(RevocationBarrier).where(and_(*conditions))
        )
        return list(result.scalars().all())

    async def clear_barrier(
        self,
        barrier_id: UUID,
        projection_watermark: int,
    ) -> bool:
        """
        清除屏障（投影已确认达到所需 revision）

        Args:
            barrier_id: 屏障 ID
            projection_watermark: 投影水位

        Returns:
            是否成功清除
        """
        result = await self.session.execute(
            select(RevocationBarrier).where(RevocationBarrier.id == barrier_id)
        )
        barrier = result.scalar_one_or_none()

        if not barrier:
            return False

        # 验证投影水位 >= barrier 的 authorization_revision
        if projection_watermark >= barrier.authorization_revision:
            barrier.is_active = False
            barrier.cleared_at = datetime.utcnow()
            await self.session.flush()
            return True

        return False

    async def build_barrier_filter(
        self,
        principal: PrincipalContext,
    ) -> Optional[Dict[str, Any]]:
        """
        构建 ES 过滤器中的 barrier 部分

        Returns:
            ES must_not 过滤器，或 None
        """
        barriers = await self.get_active_barriers(
            principal.tenant_id,
        )

        if not barriers:
            return None

        # 构建排除条件
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


async def create_revocation_barrier(
    session: AsyncSession,
    tenant_id: UUID,
    authorization_revision: int,
    barrier_type: str,
    knowledge_base_id: Optional[UUID] = None,
    document_id: Optional[UUID] = None,
) -> RevocationBarrier:
    """
    便捷函数：创建撤权屏障

    必须在 PG 事务中与授权修订一起提交
    """
    service = RevocationBarrierService(session)
    return await service.create_barrier(
        tenant_id=tenant_id,
        authorization_revision=authorization_revision,
        barrier_type=barrier_type,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
    )


async def get_active_barriers(
    session: AsyncSession,
    tenant_id: UUID,
    knowledge_base_id: Optional[UUID] = None,
) -> List[RevocationBarrier]:
    """便捷函数：获取活跃屏障"""
    service = RevocationBarrierService(session)
    return await service.get_active_barriers(tenant_id, knowledge_base_id)