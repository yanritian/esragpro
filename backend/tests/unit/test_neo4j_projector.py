"""
Neo4j Projector Tests - Neo4j 投影器测试

验证：
- 实体节点 MERGE 使用稳定 ID
- 关系创建
- 状态更新
- 删除操作
"""
import pytest
from datetime import datetime
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from app.projectors.neo4j import Neo4jProjector
from app.db.models.claims import Claim, Entity


@pytest.fixture
def mock_driver():
    """Mock Neo4j driver"""
    driver = AsyncMock()
    return driver


@pytest.fixture
def mock_session():
    """Mock Neo4j session"""
    session = AsyncMock()
    session.run = AsyncMock()
    return session


@pytest.fixture
def sample_entity():
    """Sample entity"""
    return Entity(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        canonical_name="张三",
        entity_type="PERSON",
        revision=1,
    )


@pytest.fixture
def sample_organization():
    """Sample organization"""
    return Entity(
        id=uuid4(),
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        canonical_name="A公司",
        entity_type="ORGANIZATION",
        revision=1,
    )


@pytest.fixture
def sample_claim(sample_entity, sample_organization):
    """Sample claim"""
    return Claim(
        id=uuid4(),
        tenant_id=sample_entity.tenant_id,
        knowledge_base_id=sample_entity.knowledge_base_id,
        approved_from_candidate_id=uuid4(),
        subject_entity_id=sample_entity.id,
        predicate="WORKS_FOR",
        object_kind="entity",
        object_entity_id=sample_organization.id,
        publication_state="active",
        created_by="reviewer-1",
        revision=1,
    )


class TestProjectClaim:
    """投影声明测试"""

    @pytest.mark.asyncio
    async def test_project_entity_claim(self, mock_driver, mock_session, sample_claim, sample_entity, sample_organization):
        """投影实体关系声明"""
        # Mock session context
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        await projector.project_claim(
            claim=sample_claim,
            subject_entity=sample_entity,
            object_entity=sample_organization,
        )

        # 验证 run 被调用（至少 3 次：主体、客体、关系）
        assert mock_session.run.call_count >= 3

    @pytest.mark.asyncio
    async def test_project_value_claim(self, mock_driver, mock_session, sample_entity):
        """投影值类型声明"""
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        claim = Claim(
            id=uuid4(),
            tenant_id=sample_entity.tenant_id,
            knowledge_base_id=sample_entity.knowledge_base_id,
            approved_from_candidate_id=uuid4(),
            subject_entity_id=sample_entity.id,
            predicate="AGE",
            object_kind="value",
            object_value_json={"value": 25},
            publication_state="active",
            created_by="reviewer-1",
            revision=1,
        )

        projector = Neo4jProjector(mock_driver)

        await projector.project_claim(
            claim=claim,
            subject_entity=sample_entity,
            object_entity=None,
        )

        # 验证 run 被调用
        assert mock_session.run.call_count >= 2


class TestMergeEntity:
    """合并实体测试"""

    @pytest.mark.asyncio
    async def test_merge_entity_uses_stable_id(self, mock_driver, mock_session, sample_entity):
        """合并实体使用稳定 ID"""
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        await projector._merge_entity(mock_session, sample_entity)

        # 获取第一个调用的参数
        call_args = mock_session.run.call_args
        query = call_args[0][0]

        # 验证使用 MERGE 和 id 作为键
        assert "MERGE" in query
        assert "id: $id" in query

    @pytest.mark.asyncio
    async def test_merge_entity_sets_properties(self, mock_driver, mock_session, sample_entity):
        """合并实体设置属性"""
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        await projector._merge_entity(mock_session, sample_entity)

        call_args = mock_session.run.call_args
        params = call_args[1]

        # 验证参数传递
        assert params["id"] == str(sample_entity.id)
        assert params["canonical_name"] == sample_entity.canonical_name
        assert params["entity_type"] == sample_entity.entity_type


class TestDeleteClaim:
    """删除声明测试"""

    @pytest.mark.asyncio
    async def test_delete_claim_removes_relationship(self, mock_driver, mock_session):
        """删除声明移除关系"""
        claim_id = uuid4()
        tenant_id = uuid4()

        # Mock 验证查询返回正确租户
        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value={"tenant_id": str(tenant_id)})
        mock_session.run.return_value = mock_result

        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        await projector.delete_claim(claim_id, tenant_id)

        # 验证删除查询被调用
        assert mock_session.run.call_count >= 2

    @pytest.mark.asyncio
    async def test_delete_claim_rejects_cross_tenant(self, mock_driver, mock_session):
        """删除声明拒绝跨租户访问"""
        claim_id = uuid4()
        tenant_id = uuid4()
        different_tenant_id = uuid4()

        # Mock 验证查询返回不同租户
        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value={"tenant_id": str(different_tenant_id)})
        mock_session.run.return_value = mock_result

        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        with pytest.raises(ValueError, match="Access denied"):
            await projector.delete_claim(claim_id, tenant_id)


class TestUpdateClaimState:
    """更新状态测试"""

    @pytest.mark.asyncio
    async def test_update_state_to_superseded(self, mock_driver, mock_session):
        """更新状态为已取代"""
        claim_id = uuid4()
        tenant_id = uuid4()

        # Mock 验证查询返回正确租户
        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value={"tenant_id": str(tenant_id)})
        mock_session.run.return_value = mock_result

        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        await projector.update_claim_state(claim_id, tenant_id, "superseded")

        call_args = mock_session.run.call_args
        params = call_args[1]

        assert params["new_state"] == "superseded"

    @pytest.mark.asyncio
    async def test_update_state_to_revoked(self, mock_driver, mock_session):
        """更新状态为已撤销"""
        claim_id = uuid4()
        tenant_id = uuid4()

        # Mock 验证查询返回正确租户
        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value={"tenant_id": str(tenant_id)})
        mock_session.run.return_value = mock_result

        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        await projector.update_claim_state(claim_id, tenant_id, "revoked")

        call_args = mock_session.run.call_args
        params = call_args[1]

        assert params["new_state"] == "revoked"

    @pytest.mark.asyncio
    async def test_update_state_rejects_cross_tenant(self, mock_driver, mock_session):
        """更新状态拒绝跨租户访问"""
        claim_id = uuid4()
        tenant_id = uuid4()
        different_tenant_id = uuid4()

        # Mock 验证查询返回不同租户
        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value={"tenant_id": str(different_tenant_id)})
        mock_session.run.return_value = mock_result

        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        with pytest.raises(ValueError, match="Access denied"):
            await projector.update_claim_state(claim_id, tenant_id, "revoked")


class TestCreateConstraints:
    """创建约束测试"""

    @pytest.mark.asyncio
    async def test_create_constraints(self, mock_driver, mock_session):
        """创建唯一约束"""
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        await projector.create_constraints()

        # 验证创建了两个约束
        assert mock_session.run.call_count == 2


class TestGetEntityByCanonicalId:
    """获取实体测试"""

    @pytest.mark.asyncio
    async def test_get_existing_entity(self, mock_driver, mock_session):
        """获取存在的实体"""
        entity_data = {"id": "test-id", "canonical_name": "张三"}

        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value={"e": entity_data})
        mock_session.run.return_value = mock_result
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        result = await projector.get_entity_by_canonical_id(uuid4())

        assert result == entity_data

    @pytest.mark.asyncio
    async def test_get_nonexistent_entity(self, mock_driver, mock_session):
        """获取不存在的实体"""
        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value=None)
        mock_session.run.return_value = mock_result
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        result = await projector.get_entity_by_canonical_id(uuid4())

        assert result is None


class TestCountClaims:
    """统计声明测试"""

    @pytest.mark.asyncio
    async def test_count_active_claims(self, mock_driver, mock_session):
        """统计活跃声明"""
        mock_result = MagicMock()
        mock_result.single = AsyncMock(return_value={"count": 5})
        mock_session.run.return_value = mock_result
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        projector = Neo4jProjector(mock_driver)

        count = await projector.count_claims_for_entity(
            entity_id=uuid4(),
            tenant_id=uuid4(),
        )

        assert count == 5


class TestTimeValidity:
    """时间有效性测试"""

    @pytest.mark.asyncio
    async def test_claim_with_time_constraints(self, mock_driver, mock_session, sample_entity, sample_organization):
        """带时间约束的声明"""
        mock_driver.session = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock(return_value=None)))

        now = datetime.utcnow()
        claim = Claim(
            id=uuid4(),
            tenant_id=sample_entity.tenant_id,
            knowledge_base_id=sample_entity.knowledge_base_id,
            approved_from_candidate_id=uuid4(),
            subject_entity_id=sample_entity.id,
            predicate="WORKS_FOR",
            object_kind="entity",
            object_entity_id=sample_organization.id,
            valid_from=now,
            valid_to=now.replace(year=now.year + 1),
            publication_state="active",
            created_by="reviewer-1",
            revision=1,
        )

        projector = Neo4jProjector(mock_driver)

        await projector.project_claim(
            claim=claim,
            subject_entity=sample_entity,
            object_entity=sample_organization,
        )

        # 验证关系创建包含时间字段
        call_args_list = mock_session.run.call_args_list
        found_time_fields = False
        for call_args in call_args_list:
            params = call_args[1]
            if "valid_from" in params:
                found_time_fields = True
                assert params["valid_from"] is not None

        assert found_time_fields