"""
知识图谱服务
与 Neo4j 图数据库交互的核心服务
"""
import logging
from typing import List, Dict, Any, Optional, LiteralString, cast
from dataclasses import dataclass
from contextlib import asynccontextmanager

from app.core.config import settings

logger = logging.getLogger(__name__)

# Neo4j 驱动（延迟导入）
_neo4j_driver = None


def _compile_query(query: str) -> LiteralString:
    """Adapt a dynamically-built Cypher query for the neo4j driver.

    ``AsyncSession.run`` requires a ``LiteralString`` or ``Query``; label and
    relationship types are interpolated at runtime (Cypher cannot parameterize
    them), so the string is cast explicitly to the exact type the driver's
    API declares.
    """
    return cast(LiteralString, query)


def get_neo4j_driver():
    """获取 Neo4j 驱动（单例模式）"""
    global _neo4j_driver

    if _neo4j_driver is None:
        try:
            from neo4j import AsyncGraphDatabase
            _neo4j_driver = AsyncGraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
                max_connection_lifetime=3600,
                max_connection_pool_size=50,
            )
            logger.info(f"Neo4j 驱动初始化成功: {settings.NEO4J_URI}")
        except ImportError:
            logger.warning("neo4j 库未安装，知识图谱功能不可用")
            return None
        except Exception as e:
            logger.error(f"Neo4j 驱动初始化失败: {e}")
            return None

    return _neo4j_driver


@dataclass
class GraphNode:
    """图谱节点"""
    id: str
    type: str
    properties: Dict[str, Any]


@dataclass
class GraphEdge:
    """图谱边"""
    source_id: str
    target_id: str
    relation: str
    properties: Dict[str, Any]


class KnowledgeGraphService:
    """知识图谱服务

    主要功能:
    1. 实体管理：添加、查询、删除实体
    2. 关系管理：添加、查询、删除关系
    3. 子图查询：查询实体的关联子图
    4. 多跳查询：支持多跳关系查询
    5. 事实验证：验证实体关系是否存在
    """

    def __init__(self):
        self._driver = None

    @property
    def driver(self):
        """延迟获取驱动"""
        if self._driver is None:
            self._driver = get_neo4j_driver()
        return self._driver

    @asynccontextmanager
    async def get_session(self):
        """获取数据库会话"""
        if not self.driver:
            raise RuntimeError("Neo4j 驱动未初始化")

        async with self.driver.session() as session:
            yield session

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            if not self.driver:
                return False

            async with self.get_session() as session:
                result = await session.run("RETURN 1 as test")
                record = await result.single()
                return record is not None

        except Exception as e:
            logger.warning(f"Neo4j 健康检查失败: {e}")
            return False

    # ==================== 实体管理 ====================

    async def add_entity(
        self,
        entity_type: str,
        name: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """添加实体

        Args:
            entity_type: 实体类型 (Person, Organization, Law, etc.)
            name: 实体名称
            properties: 实体属性

        Returns:
            实体 ID，失败返回 None
        """
        try:
            async with self.get_session() as session:
                # 使用 MERGE 避免重复
                query = _compile_query(f"""
                MERGE (e:{entity_type} {{name: $name}})
                SET e += $properties
                RETURN elementId(e) as id
                """)

                props = properties or {}
                props['name'] = name

                result = await session.run(
                    query,
                    name=name,
                    properties=props
                )
                record = await result.single()

                if record:
                    entity_id = record['id']
                    logger.debug(f"添加实体: {entity_type}({name}) -> {entity_id}")
                    return entity_id

        except Exception as e:
            logger.error(f"添加实体失败: {e}")

        return None

    async def get_entity(self, name: str) -> Optional[Dict[str, Any]]:
        """查询实体"""
        try:
            async with self.get_session() as session:
                query = """
                MATCH (e {name: $name})
                RETURN labels(e) as types, e as properties, elementId(e) as id
                """

                result = await session.run(query, name=name)
                record = await result.single()

                if record:
                    return {
                        'id': record['id'],
                        'name': name,
                        'types': record['types'],
                        'properties': dict(record['properties'])
                    }

        except Exception as e:
            logger.error(f"查询实体失败: {e}")

        return None

    async def delete_entity(self, name: str) -> bool:
        """删除实体及其所有关系"""
        try:
            async with self.get_session() as session:
                query = """
                MATCH (e {name: $name})
                DETACH DELETE e
                RETURN count(e) as deleted
                """

                result = await session.run(query, name=name)
                record = await result.single()

                return bool(record and record['deleted'] > 0)

        except Exception as e:
            logger.error(f"删除实体失败: {e}")
            return False

    async def merge_entities(
        self,
        source_name: str,
        target_name: str,
        keep_name: str
    ) -> bool:
        """合并两个实体

        将 source 的所有关系转移到 target，然后删除 source

        Args:
            source_name: 要合并的源实体
            target_name: 目标实体
            keep_name: 合并后保留的名称
        """
        try:
            async with self.get_session() as session:
                # 1. 将 source 的所有关系转移到 target
                query = """
                MATCH (source {name: $source_name})
                MATCH (target {name: $target_name})

                // 转移出边关系
                WITH source, target
                MATCH (source)-[r]->(other)
                WHERE other <> target
                WITH source, target, collect(r) as outRels, collect(other) as outTargets
                UNWIND range(0, size(outRels)-1) as i
                MATCH (source)-[r]->(ot)
                WHERE r = outRels[i] AND ot = outTargets[i]
                WITH source, target, type(r) as relType, ot
                MERGE (target)-[:RELATION {merged_from: source.name}]->(ot)

                // 转移入边关系
                WITH source, target
                MATCH (other)-[r]->(source)
                WHERE other <> target
                WITH source, target, collect(r) as inRels, collect(other) as inSources
                UNWIND range(0, size(inRels)-1) as i
                MATCH (is)-[r]->(source)
                WHERE r = inRels[i] AND is = inSources[i]
                WITH source, target, type(r) as relType, is
                MERGE (is)-[:RELATION {merged_from: source.name}]->(target)

                // 删除源实体
                WITH source
                DETACH DELETE source

                RETURN 1 as success
                """

                result = await session.run(
                    query,
                    source_name=source_name,
                    target_name=target_name
                )
                record = await result.single()
                return record is not None

        except Exception as e:
            logger.error(f"合并实体失败: {e}")
            return False

    async def update_entity(
        self,
        name: str,
        new_name: Optional[str] = None,
        new_type: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None
    ) -> bool:
        """更新实体"""
        try:
            async with self.get_session() as session:
                updates = []
                params: Dict[str, Any] = {"name": name}

                if new_name:
                    updates.append("SET e.name = $new_name")
                    params["new_name"] = new_name

                if properties:
                    updates.append("SET e += $properties")
                    params["properties"] = properties

                if not updates:
                    return True

                query = _compile_query(f"""
                MATCH (e {{name: $name}})
                {' '.join(updates)}
                RETURN e
                """)

                result = await session.run(query, **params)
                record = await result.single()
                return record is not None

        except Exception as e:
            logger.error(f"更新实体失败: {e}")
            return False

    async def delete_relation(
        self,
        subject: str,
        relation: str,
        obj: str
    ) -> bool:
        """删除指定关系"""
        try:
            async with self.get_session() as session:
                query = _compile_query(f"""
                MATCH (s {{name: $subject}})-[r:{relation}]->(o {{name: $object}})
                DELETE r
                RETURN count(r) as deleted
                """)

                result = await session.run(
                    query,
                    subject=subject,
                    object=obj
                )
                record = await result.single()
                return bool(record and record['deleted'] > 0)

        except Exception as e:
            logger.error(f"删除关系失败: {e}")
            return False

    async def delete_by_source_doc(self, doc_id: str) -> int:
        """删除指定文档来源的所有实体和关系

        文档删除时调用此方法清理图谱
        """
        try:
            async with self.get_session() as session:
                query = """
                MATCH (e)
                WHERE e.source_doc = $doc_id
                DETACH DELETE e
                RETURN count(e) as deleted
                """

                result = await session.run(query, doc_id=doc_id)
                record = await result.single()
                return record['deleted'] if record else 0

        except Exception as e:
            logger.error(f"清理文档图谱失败: {e}")
            return 0

    async def search_entities(
        self,
        entity_type: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """搜索实体"""
        try:
            async with self.get_session() as session:
                if entity_type and keyword:
                    query = _compile_query(f"""
                    MATCH (e:{entity_type})
                    WHERE e.name CONTAINS $keyword
                    RETURN labels(e) as types, e as properties, elementId(e) as id
                    LIMIT $limit
                    """)
                elif entity_type:
                    query = _compile_query(f"""
                    MATCH (e:{entity_type})
                    RETURN labels(e) as types, e as properties, elementId(e) as id
                    LIMIT $limit
                    """)
                elif keyword:
                    query = """
                    MATCH (e)
                    WHERE e.name CONTAINS $keyword
                    RETURN labels(e) as types, e as properties, elementId(e) as id
                    LIMIT $limit
                    """
                else:
                    query = """
                    MATCH (e)
                    RETURN labels(e) as types, e as properties, elementId(e) as id
                    LIMIT $limit
                    """

                result = await session.run(
                    query,
                    keyword=keyword or "",
                    limit=limit
                )

                entities = []
                async for record in result:
                    entities.append({
                        'id': record['id'],
                        'name': dict(record['properties']).get('name', ''),
                        'types': record['types'],
                        'properties': dict(record['properties'])
                    })

                return entities

        except Exception as e:
            logger.error(f"搜索实体失败: {e}")
            return []

    # ==================== 关系管理 ====================

    async def add_relation(
        self,
        subject_name: str,
        relation: str,
        object_name: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> bool:
        """添加关系

        Args:
            subject_name: 主体实体名称
            relation: 关系类型
            object_name: 客体实体名称
            properties: 关系属性

        Returns:
            是否成功
        """
        try:
            async with self.get_session() as session:
                # 创建或匹配实体，然后创建关系
                query = _compile_query(f"""
                MATCH (s {{name: $subject_name}})
                MATCH (o {{name: $object_name}})
                MERGE (s)-[r:{relation}]->(o)
                SET r += $properties
                RETURN r
                """)

                result = await session.run(
                    query,
                    subject_name=subject_name,
                    object_name=object_name,
                    properties=properties or {}
                )
                record = await result.single()

                if record:
                    logger.debug(f"添加关系: ({subject_name})-{relation}->({object_name})")
                    return True

        except Exception as e:
            logger.error(f"添加关系失败: {e}")

        return False

    async def get_relations(
        self,
        entity_name: str,
        direction: str = "both"
    ) -> List[Dict[str, Any]]:
        """查询实体的关系

        Args:
            entity_name: 实体名称
            direction: 方向 (outgoing/incoming/both)
        """
        try:
            async with self.get_session() as session:
                if direction == "outgoing":
                    query = """
                    MATCH (e {name: $name})-[r]->(target)
                    RETURN type(r) as relation, target.name as target, properties(r) as props
                    """
                elif direction == "incoming":
                    query = """
                    MATCH (source)-[r]->(e {name: $name})
                    RETURN type(r) as relation, source.name as source, properties(r) as props
                    """
                else:
                    query = """
                    MATCH (e {name: $name})-[r]-(other)
                    RETURN type(r) as relation,
                           CASE WHEN startNode(r) = e THEN other.name ELSE e.name END as source,
                           CASE WHEN endNode(r) = e THEN other.name ELSE e.name END as target,
                           properties(r) as props
                    """

                result = await session.run(query, name=entity_name)

                relations = []
                async for record in result:
                    relations.append({
                        'relation': record['relation'],
                        'source': record.get('source'),
                        'target': record.get('target'),
                        'properties': dict(record['props'] or {})
                    })

                return relations

        except Exception as e:
            logger.error(f"查询关系失败: {e}")
            return []

    # ==================== 子图查询 ====================

    async def query_subgraph(
        self,
        entity_name: str,
        depth: int = 2,
        limit: int = 50
    ) -> Dict[str, Any]:
        """查询实体的关联子图

        Args:
            entity_name: 起始实体名称
            depth: 查询深度（1-3）
            limit: 返回节点数量限制

        Returns:
            {'nodes': [...], 'edges': [...]}
        """
        try:
            async with self.get_session() as session:
                # 查询节点
                nodes_query = _compile_query(f"""
                MATCH path = (n {{name: $name}})-[*1..{depth}]-(related)
                RETURN DISTINCT
                    elementId(related) as id,
                    labels(related) as types,
                    related.name as name
                LIMIT $limit
                """)

                nodes_result = await session.run(
                    nodes_query,
                    name=entity_name,
                    limit=limit
                )

                nodes = []
                node_ids = set()
                async for record in nodes_result:
                    node_id = record['id']
                    if node_id not in node_ids:
                        node_ids.add(node_id)
                        nodes.append({
                            'id': node_id,
                            'name': record['name'],
                            'types': record['types']
                        })

                # 添加起始节点
                start_entity = await self.get_entity(entity_name)
                if start_entity and start_entity['id'] not in node_ids:
                    nodes.insert(0, {
                        'id': start_entity['id'],
                        'name': entity_name,
                        'types': start_entity['types']
                    })

                # 查询边
                edges_query = _compile_query(f"""
                MATCH (n {{name: $name}})-[r*1..{depth}]-(related)
                UNWIND r as rel
                RETURN DISTINCT
                    startNode(rel).name as source,
                    endNode(rel).name as target,
                    type(rel) as relation
                """)

                edges_result = await session.run(edges_query, name=entity_name)

                edges = []
                edge_set = set()
                async for record in edges_result:
                    edge_key = f"{record['source']}-{record['relation']}-{record['target']}"
                    if edge_key not in edge_set:
                        edge_set.add(edge_key)
                        edges.append({
                            'source': record['source'],
                            'target': record['target'],
                            'relation': record['relation']
                        })

                return {'nodes': nodes, 'edges': edges}

        except Exception as e:
            logger.error(f"子图查询失败: {e}")
            return {'nodes': [], 'edges': []}

    # ==================== 多跳查询 ====================

    async def multi_hop_query(
        self,
        start_entity: str,
        relation_path: List[str]
    ) -> List[Dict[str, Any]]:
        """多跳查询

        Args:
            start_entity: 起始实体
            relation_path: 关系路径，如 ['WORKS_FOR', 'LOCATED_IN']

        Returns:
            查询结果列表

        Example:
            # 查询：张三 --WORKS_FOR--> ? --LOCATED_IN--> ?
            multi_hop_query("张三", ["WORKS_FOR", "LOCATED_IN"])
        """
        try:
            async with self.get_session() as session:
                # 构建路径模式
                path_pattern = "-".join([f"[:{r}]-(:Entity)" for r in relation_path])
                # 简化版本
                path_pattern = ""
                for i, r in enumerate(relation_path):
                    path_pattern += f"-[:{r}]->(n{i})"

                query = _compile_query(f"""
                MATCH (start {{name: $name}}){path_pattern}
                RETURN start.name as start, {'n' + str(len(relation_path)-1) + '.name as end' if relation_path else ''}
                """)

                result = await session.run(query, name=start_entity)

                results = []
                async for record in result:
                    results.append(dict(record))

                return results

        except Exception as e:
            logger.error(f"多跳查询失败: {e}")
            return []

    # ==================== 事实验证 ====================

    async def verify_fact(
        self,
        subject: str,
        relation: str,
        obj: str
    ) -> Dict[str, Any]:
        """验证事实是否在图谱中存在

        Args:
            subject: 主体实体
            relation: 关系类型
            obj: 客体实体

        Returns:
            {'exists': bool, 'confidence': float, 'source': str}
        """
        try:
            async with self.get_session() as session:
                query = _compile_query(f"""
                MATCH (s {{name: $subject}})-[r:{relation}]->(o {{name: $object}})
                RETURN r, s, o
                """)

                result = await session.run(
                    query,
                    subject=subject,
                    object=obj
                )
                record = await result.single()

                if record:
                    return {
                        'exists': True,
                        'confidence': 1.0,
                        'source': 'knowledge_graph'
                    }

                return {
                    'exists': False,
                    'confidence': 0.0,
                    'source': 'knowledge_graph'
                }

        except Exception as e:
            logger.error(f"事实验证失败: {e}")
            return {
                'exists': False,
                'confidence': 0.0,
                'source': 'error',
                'error': str(e)
            }

    # ==================== 批量操作 ====================

    async def batch_add_entities(
        self,
        entities: List[Dict[str, Any]]
    ) -> int:
        """批量添加实体

        Args:
            entities: 实体列表，格式: [{'type': 'Person', 'name': '张三', 'properties': {...}}, ...]

        Returns:
            成功添加的数量
        """
        success_count = 0

        for entity in entities:
            result = await self.add_entity(
                entity_type=entity.get('type', 'Entity'),
                name=entity['name'],
                properties=entity.get('properties')
            )
            if result:
                success_count += 1

        return success_count

    async def batch_add_relations(
        self,
        relations: List[Dict[str, Any]]
    ) -> int:
        """批量添加关系

        Args:
            relations: 关系列表，格式: [{'subject': '张三', 'relation': 'WORKS_FOR', 'object': 'ABC公司'}, ...]

        Returns:
            成功添加的数量
        """
        success_count = 0

        for rel in relations:
            result = await self.add_relation(
                subject_name=rel['subject'],
                relation=rel['relation'],
                object_name=rel['object'],
                properties=rel.get('properties')
            )
            if result:
                success_count += 1

        return success_count

    # ==================== 统计信息 ====================

    async def get_stats(self) -> Dict[str, Any]:
        """获取图谱统计信息"""
        try:
            async with self.get_session() as session:
                # 节点统计
                nodes_result = await session.run("MATCH (n) RETURN count(n) as count")
                nodes_record = await nodes_result.single()

                # 关系统计
                edges_result = await session.run("MATCH ()-[r]->() RETURN count(r) as count")
                edges_record = await edges_result.single()

                # 按类型统计节点
                types_result = await session.run("""
                    MATCH (n)
                    WITH labels(n) as lbls
                    UNWIND lbls as label
                    RETURN label, count(*) as count
                    ORDER BY count DESC
                    LIMIT 10
                """)

                type_counts = {}
                async for record in types_result:
                    type_counts[record['label']] = record['count']

                # 按类型统计关系
                rel_result = await session.run("""
                    MATCH ()-[r]->()
                    RETURN type(r) as relation, count(*) as count
                    ORDER BY count DESC
                    LIMIT 10
                """)

                rel_counts = {}
                async for record in rel_result:
                    rel_counts[record['relation']] = record['count']

                # 孤立节点（没有关系的节点）
                orphan_result = await session.run("""
                    MATCH (n)
                    WHERE NOT (n)--()
                    RETURN count(n) as count
                """)
                orphan_record = await orphan_result.single()

                # 平均度
                avg_degree_result = await session.run("""
                    MATCH (n)
                    RETURN avg(size((n)--())) as avg_degree
                """)
                avg_degree_record = await avg_degree_result.single()

                return {
                    'total_nodes': nodes_record['count'] if nodes_record else 0,
                    'total_edges': edges_record['count'] if edges_record else 0,
                    'node_types': type_counts,
                    'relation_types': rel_counts,
                    'orphan_nodes': orphan_record['count'] if orphan_record else 0,
                    'avg_degree': round(avg_degree_record['avg_degree'] or 0, 2) if avg_degree_record else 0,
                }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {
                'total_nodes': 0,
                'total_edges': 0,
                'node_types': {},
                'relation_types': {},
                'orphan_nodes': 0,
                'avg_degree': 0,
                'error': str(e)
            }

    async def get_quality_metrics(self) -> Dict[str, Any]:
        """获取图谱质量指标"""
        try:
            async with self.get_session() as session:
                metrics = {}

                # 1. 实体覆盖率（有多少文档有实体）
                doc_coverage = await session.run("""
                    MATCH (n)
                    WHERE n.source_doc IS NOT NULL
                    RETURN count(DISTINCT n.source_doc) as count
                """)
                doc_record = await doc_coverage.single()
                metrics['docs_with_entities'] = doc_record['count'] if doc_record else 0

                # 2. 实体平均关系数
                avg_rels = await session.run("""
                    MATCH (n)
                    RETURN avg(size((n)--())) as avg
                """)
                avg_record = await avg_rels.single()
                metrics['avg_relations_per_entity'] = round(avg_record['avg'] or 0, 2) if avg_record else 0

                # 3. 连通性（最大连通分量占比）
                connected = await session.run("""
                    MATCH (n)
                    WITH n, size((n)--()) as degree
                    WHERE degree > 0
                    RETURN count(n) as connected_count
                """)
                conn_record = await connected.single()

                total_nodes_result = await session.run("MATCH (n) RETURN count(n) as total")
                total_record = await total_nodes_result.single()

                if total_record and total_record['total'] > 0:
                    metrics['connectivity'] = round(
                        ((conn_record['connected_count'] or 0) if conn_record else 0) / total_record['total'] * 100, 2
                    )
                else:
                    metrics['connectivity'] = 0

                # 4. 实体类型分布均衡度（熵）
                type_dist = await session.run("""
                    MATCH (n)
                    WITH labels(n)[0] as type, count(*) as cnt
                    RETURN type, cnt
                    ORDER BY cnt DESC
                """)

                type_counts = []
                async for record in type_dist:
                    type_counts.append(record['cnt'])

                if type_counts:
                    import math
                    total = sum(type_counts)
                    entropy = -sum((c/total) * math.log2(c/total) for c in type_counts if c > 0)
                    max_entropy = math.log2(len(type_counts)) if len(type_counts) > 1 else 1
                    metrics['type_balance'] = round(entropy / max_entropy * 100, 2) if max_entropy > 0 else 100
                else:
                    metrics['type_balance'] = 0

                # 5. 高价值实体（关系数 > 5）
                hub_entities = await session.run("""
                    MATCH (n)
                    WHERE size((n)--()) > 5
                    RETURN n.name as name, size((n)--()) as degree
                    ORDER BY degree DESC
                    LIMIT 10
                """)

                hubs = []
                async for record in hub_entities:
                    hubs.append({
                        'name': record['name'],
                        'degree': record['degree']
                    })
                metrics['hub_entities'] = hubs

                return metrics

        except Exception as e:
            logger.error(f"获取质量指标失败: {e}")
            return {'error': str(e)}

    async def close(self):
        """关闭连接"""
        if self.driver:
            await self.driver.close()


# 全局实例
knowledge_graph_service = KnowledgeGraphService()