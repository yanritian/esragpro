"""
知识图谱管理 API（内部/管理用途）

注意：用户问答已集成到 /kb/search，此接口仅用于管理和调试
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from app.security.middleware import get_principal
from pydantic import BaseModel, Field

from app.services.knowledge_graph import knowledge_graph_service

router = APIRouter(prefix="/graph", tags=["Knowledge Graph (Admin)"], dependencies=[Depends(get_principal)])


# ==================== 请求/响应模型 ====================

class SubgraphQueryRequest(BaseModel):
    """子图查询请求"""
    entity_name: str
    depth: int = Field(default=2, ge=1, le=3)
    limit: int = Field(default=50, ge=1, le=200)


class MergeEntitiesRequest(BaseModel):
    """实体合并请求"""
    source_name: str = Field(..., description="要合并的源实体名称")
    target_name: str = Field(..., description="目标实体名称")
    keep_name: Optional[str] = Field(None, description="合并后的名称，默认使用目标名称")


class UpdateEntityRequest(BaseModel):
    """实体更新请求"""
    name: str = Field(..., description="实体名称")
    new_name: Optional[str] = Field(None, description="新名称")
    new_type: Optional[str] = Field(None, description="新类型")
    properties: Optional[Dict[str, Any]] = Field(None, description="更新属性")


class DeleteRelationRequest(BaseModel):
    """删除关系请求"""
    subject: str
    relation: str
    object: str


# ==================== 健康检查 ====================

@router.get("/health")
async def graph_health() -> Dict[str, Any]:
    """知识图谱健康检查"""
    is_healthy = await knowledge_graph_service.health_check()
    return {
        "status": "healthy" if is_healthy else "unavailable",
        "enabled": True
    }


# ==================== 统计信息 ====================

@router.get("/stats")
async def get_graph_stats() -> Dict[str, Any]:
    """获取知识图谱统计信息"""
    return await knowledge_graph_service.get_stats()


@router.get("/quality")
async def get_graph_quality() -> Dict[str, Any]:
    """获取图谱质量指标

    包括：
    - 实体覆盖率
    - 连通性
    - 类型均衡度
    - 高价值实体（Hub）
    """
    return await knowledge_graph_service.get_quality_metrics()


# ==================== 实体管理 ====================

@router.get("/entities")
async def search_entities(
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    entity_type: Optional[str] = Query(None, description="实体类型"),
    limit: int = Query(50, ge=1, le=200, description="返回数量限制")
) -> Dict[str, Any]:
    """搜索实体"""
    entities = await knowledge_graph_service.search_entities(
        entity_type=entity_type,
        keyword=keyword,
        limit=limit
    )
    return {
        "total": len(entities),
        "entities": entities
    }


@router.get("/entities/{entity_name}")
async def get_entity(entity_name: str) -> Dict[str, Any]:
    """获取实体详情"""
    entity = await knowledge_graph_service.get_entity(entity_name)
    if not entity:
        raise HTTPException(status_code=404, detail="实体不存在")
    return entity


@router.put("/entities")
async def update_entity(request: UpdateEntityRequest) -> Dict[str, Any]:
    """更新实体"""
    success = await knowledge_graph_service.update_entity(
        name=request.name,
        new_name=request.new_name,
        new_type=request.new_type,
        properties=request.properties
    )
    if not success:
        raise HTTPException(status_code=400, detail="更新失败")
    return {"success": True, "message": "实体更新成功"}


@router.delete("/entities/{entity_name}")
async def delete_entity(entity_name: str) -> Dict[str, Any]:
    """删除实体及其所有关系"""
    success = await knowledge_graph_service.delete_entity(entity_name)
    if not success:
        raise HTTPException(status_code=404, detail="实体不存在或删除失败")
    return {"success": True, "message": f"实体 '{entity_name}' 已删除"}


@router.post("/entities/merge")
async def merge_entities(request: MergeEntitiesRequest) -> Dict[str, Any]:
    """合并两个实体

    将源实体的所有关系转移到目标实体，然后删除源实体
    """
    success = await knowledge_graph_service.merge_entities(
        source_name=request.source_name,
        target_name=request.target_name,
        keep_name=request.keep_name or request.target_name
    )
    if not success:
        raise HTTPException(status_code=400, detail="合并失败")
    return {
        "success": True,
        "message": f"已将 '{request.source_name}' 合并到 '{request.target_name}'"
    }


@router.get("/entities/{entity_name}/relations")
async def get_entity_relations(
    entity_name: str,
    direction: str = Query("both", description="方向: outgoing/incoming/both")
) -> Dict[str, Any]:
    """获取实体的关系"""
    relations = await knowledge_graph_service.get_relations(entity_name, direction)
    return {
        "entity": entity_name,
        "relations": relations
    }


# ==================== 关系管理 ====================

@router.delete("/relations")
async def delete_relation(request: DeleteRelationRequest) -> Dict[str, Any]:
    """删除指定关系"""
    success = await knowledge_graph_service.delete_relation(
        subject=request.subject,
        relation=request.relation,
        obj=request.object
    )
    if not success:
        raise HTTPException(status_code=404, detail="关系不存在或删除失败")
    return {"success": True, "message": "关系已删除"}


# ==================== 子图可视化 ====================

@router.post("/subgraph")
async def query_subgraph(request: SubgraphQueryRequest) -> Dict[str, Any]:
    """查询子图（用于图谱可视化）"""
    return await knowledge_graph_service.query_subgraph(
        entity_name=request.entity_name,
        depth=request.depth,
        limit=request.limit
    )


@router.get("/subgraph/{entity_name}")
async def get_subgraph(
    entity_name: str,
    depth: int = Query(2, ge=1, le=3),
    limit: int = Query(50, ge=1, le=200)
) -> Dict[str, Any]:
    """查询子图（GET 方式）"""
    return await knowledge_graph_service.query_subgraph(
        entity_name=entity_name,
        depth=depth,
        limit=limit
    )


# ==================== 全局查询 ====================

@router.get("/overview")
async def get_overview(limit: int = Query(100, ge=10, le=500)) -> Dict[str, Any]:
    """获取图谱概览（用于首页可视化）

    返回节点和边的数据，适合前端图谱组件直接使用
    """
    try:
        # 获取所有节点（限制数量）
        nodes = await knowledge_graph_service.search_entities(limit=limit)

        # 获取这些节点之间的关系
        all_edges = []
        seen_edges = set()

        for node in nodes[:50]:  # 限制查询数量
            relations = await knowledge_graph_service.get_relations(node['name'])
            for rel in relations:
                edge_key = f"{rel.get('source')}-{rel['relation']}-{rel.get('target')}"
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    all_edges.append({
                        'source': rel.get('source'),
                        'target': rel.get('target'),
                        'relation': rel['relation']
                    })

        return {
            'nodes': nodes[:limit],
            'edges': all_edges[:limit * 2]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))