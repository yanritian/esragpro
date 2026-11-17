"""
Neo4j Projector - Neo4j 图投影器

WP4 核心功能：
- 将 active Claim 投影到 Neo4j 图
- 使用稳定 canonical ID，不按 name 全局合并
- 支持增量更新和删除
"""
import logging
from datetime import datetime
from typing import Any, Optional, LiteralString, cast
from uuid import UUID

from neo4j import AsyncDriver, AsyncSession

from app.db.models.claims import Claim, Entity

logger = logging.getLogger(__name__)


class Neo4jProjector:
    """Neo4j 图投影器"""

    def __init__(self, driver: AsyncDriver):
        self.driver = driver

    async def project_claim(self, claim: Claim, subject_entity: Entity, object_entity: Optional[Entity] = None) -> None:
        """
        投影单个声明到 Neo4j

        Args:
            claim: 声明
            subject_entity: 主体实体
            object_entity: 客体实体（如果 object_kind == entity）
        """
        async with self.driver.session() as session:
            # 1. 创建/更新主体实体节点（使用稳定 ID）
            await self._merge_entity(
                session=session,
                entity=subject_entity,
            )

            # 2. 如果有客体实体，创建/更新客体实体节点
            if object_entity:
                await self._merge_entity(
                    session=session,
                    entity=object_entity,
                )

            # 3. 创建/更新关系
            if claim.object_kind == "entity" and object_entity:
                await self._merge_relationship(
                    session=session,
                    claim=claim,
                    subject_entity_id=subject_entity.id,
                    object_entity_id=object_entity.id,
                )
            elif claim.object_kind == "value":
                await self._merge_value_relationship(
                    session=session,
                    claim=claim,
                    subject_entity_id=subject_entity.id,
                )

        logger.info(f"Projected claim {claim.id} to Neo4j")

    async def _merge_entity(self, session: AsyncSession, entity: Entity) -> None:
        """
        合并实体节点

        使用稳定 canonical ID 作为 MERGE 键，不按 name 全局合并
        """
        query = """
        MERGE (e:Entity {id: $id})
        SET e.canonical_name = $canonical_name,
            e.entity_type = $entity_type,
            e.tenant_id = $tenant_id,
            e.knowledge_base_id = $knowledge_base_id,
            e.revision = $revision,
            e.updated_at = $updated_at
        """
        await session.run(
            query,
            id=str(entity.id),
            canonical_name=entity.canonical_name,
            entity_type=entity.entity_type,
            tenant_id=str(entity.tenant_id),
            knowledge_base_id=str(entity.knowledge_base_id),
            revision=entity.revision,
            updated_at=entity.updated_at.isoformat() if entity.updated_at else None,
        )

    async def _merge_relationship(
        self,
        session: AsyncSession,
        claim: Claim,
        subject_entity_id: UUID,
        object_entity_id: UUID,
    ) -> None:
        """合并实体关系"""
        # 使用动态关系类型（predicate）
        # 注意：Neo4j 要求关系类型为大写字母开头
        rel_type = claim.predicate.upper()

        query = f"""
        MATCH (subject:Entity {{id: $subject_id}})
        MATCH (object:Entity {{id: $object_id}})
        MERGE (subject)-[r:{rel_type} {{claim_id: $claim_id}}]->(object)
        SET r.tenant_id = $tenant_id,
            r.knowledge_base_id = $knowledge_base_id,
            r.publication_state = $publication_state,
            r.valid_from = $valid_from,
            r.valid_to = $valid_to,
            r.created_at = $created_at,
            r.updated_at = $updated_at
        """
        await session.run(
            cast(LiteralString, query),
            subject_id=str(subject_entity_id),
            object_id=str(object_entity_id),
            claim_id=str(claim.id),
            tenant_id=str(claim.tenant_id),
            knowledge_base_id=str(claim.knowledge_base_id),
            publication_state=claim.publication_state,
            valid_from=claim.valid_from.isoformat() if claim.valid_from else None,
            valid_to=claim.valid_to.isoformat() if claim.valid_to else None,
            created_at=claim.created_at.isoformat() if claim.created_at else None,
            updated_at=claim.updated_at.isoformat() if claim.updated_at else None,
        )

    async def _merge_value_relationship(
        self,
        session: AsyncSession,
        claim: Claim,
        subject_entity_id: UUID,
    ) -> None:
        """合并值类型关系"""
        rel_type = claim.predicate.upper()

        # 对于值类型，创建一个值节点
        query = f"""
        MATCH (subject:Entity {{id: $subject_id}})
        MERGE (v:Value {{claim_id: $claim_id}})
        SET v.value = $value,
            v.tenant_id = $tenant_id,
            v.knowledge_base_id = $knowledge_base_id
        MERGE (subject)-[r:{rel_type}]->(v)
        SET r.publication_state = $publication_state,
            r.valid_from = $valid_from,
            r.valid_to = $valid_to
        """
        await session.run(
            cast(LiteralString, query),
            subject_id=str(subject_entity_id),
            claim_id=str(claim.id),
            value=str(claim.object_value_json) if claim.object_value_json else None,
            tenant_id=str(claim.tenant_id),
            knowledge_base_id=str(claim.knowledge_base_id),
            publication_state=claim.publication_state,
            valid_from=claim.valid_from.isoformat() if claim.valid_from else None,
            valid_to=claim.valid_to.isoformat() if claim.valid_to else None,
        )

    async def delete_claim(self, claim_id: UUID, tenant_id: UUID) -> None:
        """
        删除声明的图投影

        Args:
            claim_id: 声明 ID
            tenant_id: 租户 ID（用于权限验证）

        Raises:
            ValueError: 如果声明不存在或不属于该租户
        """
        async with self.driver.session() as session:
            # 先验证声明属于该租户
            verify_query = """
            MATCH ()-[r {claim_id: $claim_id}]-()
            RETURN r.tenant_id AS tenant_id
            LIMIT 1
            """
            result = await session.run(verify_query, claim_id=str(claim_id))
            record = await result.single()

            if not record:
                logger.warning(f"Claim {claim_id} not found in Neo4j")
                return

            actual_tenant_id = record["tenant_id"]
            if actual_tenant_id != str(tenant_id):
                raise ValueError(
                    f"Access denied: claim {claim_id} belongs to tenant {actual_tenant_id}, "
                    f"not {tenant_id}"
                )

            # 删除关系和值节点
            query = """
            MATCH ()-[r {claim_id: $claim_id}]-()
            DELETE r
            """
            await session.run(query, claim_id=str(claim_id))

            # 删除孤立的值节点
            query2 = """
            MATCH (v:Value {claim_id: $claim_id})
            DETACH DELETE v
            """
            await session.run(query2, claim_id=str(claim_id))

        logger.info(f"Deleted claim {claim_id} from Neo4j")

    async def update_claim_state(
        self,
        claim_id: UUID,
        tenant_id: UUID,
        new_state: str,
    ) -> None:
        """
        更新声明的发布状态

        Args:
            claim_id: 声明 ID
            tenant_id: 租户 ID（用于权限验证）
            new_state: 新状态 (active/superseded/revoked/expired)

        Raises:
            ValueError: 如果声明不存在或不属于该租户
        """
        async with self.driver.session() as session:
            # 先验证声明属于该租户
            verify_query = """
            MATCH ()-[r {claim_id: $claim_id}]-()
            RETURN r.tenant_id AS tenant_id
            LIMIT 1
            """
            result = await session.run(verify_query, claim_id=str(claim_id))
            record = await result.single()

            if not record:
                logger.warning(f"Claim {claim_id} not found in Neo4j")
                return

            actual_tenant_id = record["tenant_id"]
            if actual_tenant_id != str(tenant_id):
                raise ValueError(
                    f"Access denied: claim {claim_id} belongs to tenant {actual_tenant_id}, "
                    f"not {tenant_id}"
                )

            # 更新状态
            query = """
            MATCH ()-[r {claim_id: $claim_id}]-()
            SET r.publication_state = $new_state,
                r.updated_at = $updated_at
            """
            await session.run(
                query,
                claim_id=str(claim_id),
                new_state=new_state,
                updated_at=datetime.utcnow().isoformat(),
            )

        logger.info(f"Updated claim {claim_id} state to {new_state}")

    async def create_constraints(self) -> None:
        """
        创建 Neo4j 约束

        确保实体 ID 唯一
        """
        async with self.driver.session() as session:
            # 实体 ID 唯一约束
            await session.run(
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.id IS UNIQUE"
            )

            # Claim ID 在关系上唯一
            await session.run(
                "CREATE CONSTRAINT relationship_claim_id_unique IF NOT EXISTS "
                "FOR ()-[r]-() REQUIRE r.claim_id IS UNIQUE"
            )

        logger.info("Created Neo4j constraints")

    async def get_entity_by_canonical_id(
        self,
        entity_id: UUID,
    ) -> Optional[dict[str, Any]]:
        """
        通过稳定 ID 获取实体

        Args:
            entity_id: 实体 ID

        Returns:
            实体属性（如果存在）
        """
        async with self.driver.session() as session:
            query = "MATCH (e:Entity {id: $id}) RETURN e"
            result = await session.run(query, id=str(entity_id))
            record = await result.single()
            if record:
                return dict(record["e"])
            return None

    async def count_claims_for_entity(
        self,
        entity_id: UUID,
        tenant_id: UUID,
    ) -> int:
        """
        统计实体的声明数量

        Args:
            entity_id: 实体 ID
            tenant_id: 租户 ID

        Returns:
            声明数量
        """
        async with self.driver.session() as session:
            query = """
            MATCH (e:Entity {id: $entity_id, tenant_id: $tenant_id})
            MATCH (e)-[r]-()
            WHERE r.tenant_id = $tenant_id AND r.publication_state = 'active'
            RETURN count(r) as count
            """
            result = await session.run(
                query,
                entity_id=str(entity_id),
                tenant_id=str(tenant_id),
            )
            record = await result.single()
            return record["count"] if record else 0