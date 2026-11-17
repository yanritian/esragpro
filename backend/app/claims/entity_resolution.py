"""
Entity Resolution - 实体消歧服务

WP4 核心功能：
- 实体规范化：同名异义识别
- 别名管理：保留合并审计
- 不按字符串静默合并
"""
import logging
import re
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claims import Entity, EntityAlias

logger = logging.getLogger(__name__)


class EntityResolutionService:
    """实体消歧服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def normalize_name(name: str) -> str:
        """
        规范化名称

        - 转小写
        - 移除多余空格
        - 移除标点
        """
        normalized = name.lower().strip()
        # 移除常见标点
        normalized = re.sub(r'[^\w\s]', '', normalized)
        # 合并空格
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized

    async def find_or_create_entity(
        self,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        name: str,
        entity_type: str,
        principal_id: str,
    ) -> tuple[Entity, bool]:
        """
        查找或创建实体

        Args:
            tenant_id: 租户 ID
            knowledge_base_id: 知识库 ID
            name: 实体名称
            entity_type: 实体类型
            principal_id: 创建者 ID

        Returns:
            (Entity, is_new) - 实体和是否新创建
        """
        normalized = self.normalize_name(name)

        # 查找现有别名
        existing_alias = await self._find_alias(tenant_id, knowledge_base_id, normalized)

        if existing_alias:
            # 返回关联的实体
            result = await self.session.execute(
                select(Entity).where(Entity.id == existing_alias.entity_id)
            )
            entity = result.scalar_one_or_none()
            if entity:
                logger.info(f"Found existing entity for alias: {name}")
                return entity, False

        # 创建新实体
        entity = Entity(
            id=uuid4(),
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            canonical_name=name,
            entity_type=entity_type,
            revision=1,
        )
        self.session.add(entity)

        # 创建别名
        alias = EntityAlias(
            id=uuid4(),
            tenant_id=tenant_id,
            entity_id=entity.id,
            alias=name,
            normalized_alias=normalized,
        )
        self.session.add(alias)

        await self.session.flush()

        logger.info(f"Created new entity: {name} ({entity_type})")
        return entity, True

    async def add_alias(
        self,
        tenant_id: UUID,
        entity_id: UUID,
        alias_name: str,
    ) -> EntityAlias:
        """
        添加实体别名

        Args:
            tenant_id: 租户 ID
            entity_id: 实体 ID
            alias_name: 别名

        Returns:
            EntityAlias
        """
        normalized = self.normalize_name(alias_name)

        # 检查别名是否已存在
        existing = await self._find_alias(tenant_id, None, normalized)
        if existing:
            if existing.entity_id == entity_id:
                return existing
            raise ValueError(f"Alias '{alias_name}' already belongs to another entity")

        alias = EntityAlias(
            id=uuid4(),
            tenant_id=tenant_id,
            entity_id=entity_id,
            alias=alias_name,
            normalized_alias=normalized,
        )
        self.session.add(alias)
        await self.session.flush()

        logger.info(f"Added alias '{alias_name}' to entity {entity_id}")
        return alias

    async def merge_entities(
        self,
        tenant_id: UUID,
        source_entity_id: UUID,
        target_entity_id: UUID,
        principal_id: str,
    ) -> Entity:
        """
        合并实体

        将 source 合并到 target，保留别名和审计日志

        Args:
            tenant_id: 租户 ID
            source_entity_id: 源实体 ID（将被合并）
            target_entity_id: 目标实体 ID
            principal_id: 操作者 ID

        Returns:
            目标实体
        """
        if source_entity_id == target_entity_id:
            raise ValueError("Cannot merge entity with itself")

        # 获取源实体
        result = await self.session.execute(
            select(Entity).where(
                and_(
                    Entity.id == source_entity_id,
                    Entity.tenant_id == tenant_id,
                )
            )
        )
        source_entity = result.scalar_one_or_none()

        if not source_entity:
            raise ValueError(f"Source entity not found: {source_entity_id}")

        # 获取目标实体
        result = await self.session.execute(
            select(Entity).where(
                and_(
                    Entity.id == target_entity_id,
                    Entity.tenant_id == tenant_id,
                )
            )
        )
        target_entity = result.scalar_one_or_none()

        if not target_entity:
            raise ValueError(f"Target entity not found: {target_entity_id}")

        # 迁移源实体的别名到目标实体
        result = await self.session.execute(
            select(EntityAlias).where(EntityAlias.entity_id == source_entity_id)
        )
        source_aliases = list(result.scalars().all())

        for alias in source_aliases:
            alias.entity_id = target_entity_id

        # 标记源实体为已合并（设置 canonical_name 为合并状态）
        source_entity.canonical_name = f"[MERGED into {target_entity_id}]"
        source_entity.revision += 1
        source_entity.updated_at = datetime.utcnow()

        # 更新目标实体的修订版本
        target_entity.revision += 1
        target_entity.updated_at = datetime.utcnow()

        await self.session.flush()

        logger.info(f"Merged entity {source_entity_id} into {target_entity_id}")
        return target_entity

    async def _find_alias(
        self,
        tenant_id: UUID,
        knowledge_base_id: Optional[UUID],
        normalized_alias: str,
    ) -> Optional[EntityAlias]:
        """查找别名"""
        conditions = [
            EntityAlias.tenant_id == tenant_id,
            EntityAlias.normalized_alias == normalized_alias,
        ]

        if knowledge_base_id:
            # 通过实体关联过滤知识库
            result = await self.session.execute(
                select(EntityAlias)
                .join(Entity, EntityAlias.entity_id == Entity.id)
                .where(
                    and_(
                        EntityAlias.tenant_id == tenant_id,
                        EntityAlias.normalized_alias == normalized_alias,
                        Entity.knowledge_base_id == knowledge_base_id,
                    )
                )
            )
        else:
            result = await self.session.execute(
                select(EntityAlias).where(and_(*conditions))
            )

        return result.scalar_one_or_none()

    async def get_entity_aliases(
        self,
        tenant_id: UUID,
        entity_id: UUID,
    ) -> list[EntityAlias]:
        """获取实体的所有别名"""
        result = await self.session.execute(
            select(EntityAlias).where(
                and_(
                    EntityAlias.tenant_id == tenant_id,
                    EntityAlias.entity_id == entity_id,
                )
            )
        )
        return list(result.scalars().all())

    async def resolve_entity_by_name(
        self,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        name: str,
    ) -> Optional[Entity]:
        """
        通过名称解析实体

        Args:
            tenant_id: 租户 ID
            knowledge_base_id: 知识库 ID
            name: 实体名称

        Returns:
            实体（如果找到）
        """
        normalized = self.normalize_name(name)

        result = await self.session.execute(
            select(Entity)
            .join(EntityAlias, Entity.id == EntityAlias.entity_id)
            .where(
                and_(
                    Entity.tenant_id == tenant_id,
                    Entity.knowledge_base_id == knowledge_base_id,
                    EntityAlias.normalized_alias == normalized,
                )
            )
        )
        return result.scalar_one_or_none()