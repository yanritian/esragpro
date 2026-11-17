"""
Projectors - 投影器

WP3 核心模块：
- elasticsearch: ES 投影器
"""
from app.projectors.elasticsearch import ElasticsearchProjector

__all__ = [
    "ElasticsearchProjector",
]