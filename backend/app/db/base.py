"""
Database Base Configuration

SQLAlchemy 声明式基类配置
"""
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 命名约定：遵循 WP2 迁移的命名风格
convention = {
    "ix": "ix_%(table_name)s_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_label)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_label)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)


class Base(DeclarativeBase):
    """
    SQLAlchemy 声明式基类

    所有模型都继承自此类
    """
    metadata = metadata

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典（用于序列化）
        """
        result = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, UUID):
                value = str(value)
            result[column.name] = value
        return result