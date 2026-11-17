"""
Entity Resolution Tests - 实体消歧测试

验证：
- 规范化名称
- 查找或创建实体
- 别名管理
- 实体合并
"""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from app.claims.entity_resolution import EntityResolutionService
from app.db.models.claims import Entity, EntityAlias


@pytest.fixture
def mock_session():
    """Mock database session"""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


class TestNormalizeName:
    """名称规范化测试"""

    def test_lowercase(self):
        """转小写"""
        result = EntityResolutionService.normalize_name("Zhang San")
        assert result == "zhang san"

    def test_strip_whitespace(self):
        """移除空格"""
        result = EntityResolutionService.normalize_name("  Zhang San  ")
        assert result == "zhang san"

    def test_remove_punctuation(self):
        """移除标点"""
        result = EntityResolutionService.normalize_name("Zhang, San!")
        assert result == "zhang san"

    def test_collapse_spaces(self):
        """合并空格"""
        result = EntityResolutionService.normalize_name("Zhang    San")
        assert result == "zhang san"


class TestFindOrCreateEntity:
    """查找或创建实体测试"""

    @pytest.mark.asyncio
    async def test_create_new_entity(self, mock_session):
        """创建新实体"""
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        service = EntityResolutionService(mock_session)

        tenant_id = uuid4()
        kb_id = uuid4()

        entity, is_new = await service.find_or_create_entity(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            name="张三",
            entity_type="PERSON",
            principal_id="user-1",
        )

        assert is_new is True
        assert entity.canonical_name == "张三"
        assert entity.entity_type == "PERSON"

    @pytest.mark.asyncio
    async def test_find_existing_entity_by_alias(self, mock_session):
        """通过别名查找现有实体"""
        tenant_id = uuid4()
        kb_id = uuid4()
        entity_id = uuid4()

        # Mock existing entity
        existing_entity = Entity(
            id=entity_id,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            canonical_name="Zhang San",
            entity_type="PERSON",
            revision=1,
        )

        # Mock existing alias
        existing_alias = EntityAlias(
            id=uuid4(),
            tenant_id=tenant_id,
            entity_id=entity_id,
            alias="Zhang San",
            normalized_alias="zhang san",
        )

        # Setup mock returns
        alias_result = MagicMock()
        alias_result.scalar_one_or_none = MagicMock(return_value=existing_alias)

        entity_result = MagicMock()
        entity_result.scalar_one_or_none = MagicMock(return_value=existing_entity)

        mock_session.execute = AsyncMock(side_effect=[alias_result, entity_result])

        service = EntityResolutionService(mock_session)

        entity, is_new = await service.find_or_create_entity(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            name="Zhang San",
            entity_type="PERSON",
            principal_id="user-1",
        )

        assert is_new is False
        assert entity.id == entity_id


class TestAddAlias:
    """添加别名测试"""

    @pytest.mark.asyncio
    async def test_add_alias_success(self, mock_session):
        """成功添加别名"""
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        service = EntityResolutionService(mock_session)

        alias = await service.add_alias(
            tenant_id=uuid4(),
            entity_id=uuid4(),
            alias_name="张三丰",
        )

        assert alias is not None
        mock_session.add.assert_called()
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_duplicate_alias_same_entity(self, mock_session):
        """添加重复别名（同一实体）"""
        tenant_id = uuid4()
        entity_id = uuid4()

        existing_alias = EntityAlias(
            id=uuid4(),
            tenant_id=tenant_id,
            entity_id=entity_id,
            alias="张三",
            normalized_alias="zhang san",
        )

        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_alias))
        )

        service = EntityResolutionService(mock_session)

        result = await service.add_alias(
            tenant_id=tenant_id,
            entity_id=entity_id,
            alias_name="张三",
        )

        # 返回现有别名
        assert result.id == existing_alias.id


class TestMergeEntities:
    """实体合并测试"""

    @pytest.mark.asyncio
    async def test_cannot_merge_with_self(self, mock_session):
        """不能与自己合并"""
        service = EntityResolutionService(mock_session)
        entity_id = uuid4()

        with pytest.raises(ValueError, match="Cannot merge entity with itself"):
            await service.merge_entities(
                tenant_id=uuid4(),
                source_entity_id=entity_id,
                target_entity_id=entity_id,
                principal_id="user-1",
            )

    @pytest.mark.asyncio
    async def test_merge_preserves_aliases(self, mock_session):
        """合并保留别名"""
        tenant_id = uuid4()
        source_id = uuid4()
        target_id = uuid4()

        # Mock source entity
        source_entity = Entity(
            id=source_id,
            tenant_id=tenant_id,
            knowledge_base_id=uuid4(),
            canonical_name="Source",
            entity_type="PERSON",
            revision=1,
        )

        # Mock target entity
        target_entity = Entity(
            id=target_id,
            tenant_id=tenant_id,
            knowledge_base_id=uuid4(),
            canonical_name="Target",
            entity_type="PERSON",
            revision=1,
        )

        # Mock source aliases
        source_alias = EntityAlias(
            id=uuid4(),
            tenant_id=tenant_id,
            entity_id=source_id,
            alias="Source Alias",
            normalized_alias="source alias",
        )

        # Setup mock returns
        source_result = MagicMock()
        source_result.scalar_one_or_none = MagicMock(return_value=source_entity)

        target_result = MagicMock()
        target_result.scalar_one_or_none = MagicMock(return_value=target_entity)

        aliases_result = MagicMock()
        aliases_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[source_alias])))

        mock_session.execute = AsyncMock(side_effect=[source_result, target_result, aliases_result])

        service = EntityResolutionService(mock_session)

        await service.merge_entities(
            tenant_id=tenant_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            principal_id="user-1",
        )

        # 验证别名被迁移
        assert source_alias.entity_id == target_id
        mock_session.flush.assert_called_once()


class TestResolveEntityByName:
    """通过名称解析实体测试"""

    @pytest.mark.asyncio
    async def test_resolve_found(self, mock_session):
        """成功解析"""
        tenant_id = uuid4()
        kb_id = uuid4()
        entity_id = uuid4()

        entity = Entity(
            id=entity_id,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            canonical_name="Zhang San",
            entity_type="PERSON",
            revision=1,
        )

        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=entity))
        )

        service = EntityResolutionService(mock_session)

        result = await service.resolve_entity_by_name(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            name="Zhang San",
        )

        assert result is not None
        assert result.id == entity_id

    @pytest.mark.asyncio
    async def test_resolve_not_found(self, mock_session):
        """未找到实体"""
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        service = EntityResolutionService(mock_session)

        result = await service.resolve_entity_by_name(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            name="NonExistent",
        )

        assert result is None