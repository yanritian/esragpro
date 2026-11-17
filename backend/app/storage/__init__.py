"""
Storage - 存储抽象层

WP3 核心模块：
- blob: 对象存储（本地/S3）
"""
from app.storage.blob import (
    BlobStorage,
    LocalBlobStorage,
    S3BlobStorage,
    get_blob_storage,
)

__all__ = [
    "BlobStorage",
    "LocalBlobStorage",
    "S3BlobStorage",
    "get_blob_storage",
]