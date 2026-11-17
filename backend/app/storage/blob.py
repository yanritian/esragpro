"""
Blob Storage - 对象存储抽象层

WP3 核心设计：
- local_blob: 开发环境本地存储
- s3_blob: 生产环境 S3-compatible 存储
- 租户隔离前缀
- 不可变写入（写入后不可修改）
"""
import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from uuid import UUID


class BlobStorage(ABC):
    """对象存储抽象基类"""

    @abstractmethod
    async def put(
        self,
        tenant_id: UUID,
        key: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> str:
        """
        存储对象

        Args:
            tenant_id: 租户 ID
            key: 对象键
            content: 内容
            content_type: 内容类型

        Returns:
            storage_key: 存储键（用于后续引用）
        """
        pass

    @abstractmethod
    async def get(
        self,
        tenant_id: UUID,
        storage_key: str,
    ) -> bytes:
        """
        获取对象

        Args:
            tenant_id: 租户 ID
            storage_key: 存储键

        Returns:
            内容
        """
        pass

    @abstractmethod
    async def delete(
        self,
        tenant_id: UUID,
        storage_key: str,
    ) -> bool:
        """
        删除对象

        Args:
            tenant_id: 租户 ID
            storage_key: 存储键

        Returns:
            是否成功
        """
        pass

    @abstractmethod
    async def compute_hash(
        self,
        content: bytes,
    ) -> str:
        """
        计算内容哈希

        Args:
            content: 内容

        Returns:
            SHA-256 哈希
        """
        pass


class LocalBlobStorage(BlobStorage):
    """
    本地对象存储

    用于开发环境
    """

    def __init__(self, base_path: Optional[str] = None):
        self.base_path = Path(base_path or os.environ.get("BLOB_STORAGE_PATH", "./data/blobs"))
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _tenant_path(self, tenant_id: UUID) -> Path:
        """租户存储路径"""
        return self.base_path / str(tenant_id)

    async def put(
        self,
        tenant_id: UUID,
        key: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> str:
        """存储对象"""
        tenant_path = self._tenant_path(tenant_id)
        tenant_path.mkdir(parents=True, exist_ok=True)

        # 使用 SHA-256 作为文件名（不可变）
        content_hash = await self.compute_hash(content)
        storage_key = f"{key}/{content_hash}"

        file_path = tenant_path / storage_key
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # 写入文件
        with open(file_path, "wb") as f:
            f.write(content)

        # 返回租户前缀的存储键
        return f"local://{tenant_id}/{storage_key}"

    async def get(
        self,
        tenant_id: UUID,
        storage_key: str,
    ) -> bytes:
        """获取对象"""
        # 解析 storage_key
        if storage_key.startswith("local://"):
            # 格式: local://{tenant_id}/{key}/{hash}
            parts = storage_key[8:].split("/", 2)
            if len(parts) >= 3:
                actual_tenant_id = UUID(parts[0])
                if actual_tenant_id != tenant_id:
                    raise ValueError(f"Tenant mismatch: expected {tenant_id}, got {actual_tenant_id}")
                relative_path = parts[1] + "/" + parts[2]
            else:
                raise ValueError(f"Invalid storage_key format: {storage_key}")
        else:
            # 简单路径格式
            relative_path = storage_key

        file_path = self._tenant_path(tenant_id) / relative_path

        if not file_path.exists():
            raise FileNotFoundError(f"Blob not found: {storage_key}")

        with open(file_path, "rb") as f:
            return f.read()

    async def delete(
        self,
        tenant_id: UUID,
        storage_key: str,
    ) -> bool:
        """删除对象"""
        try:
            # 解析 storage_key
            if storage_key.startswith("local://"):
                parts = storage_key[8:].split("/", 2)
                if len(parts) >= 3:
                    actual_tenant_id = UUID(parts[0])
                    if actual_tenant_id != tenant_id:
                        return False
                    relative_path = parts[1] + "/" + parts[2]
                else:
                    return False
            else:
                relative_path = storage_key

            file_path = self._tenant_path(tenant_id) / relative_path

            if file_path.exists():
                file_path.unlink()
                return True
            return False
        except Exception:
            return False

    async def compute_hash(
        self,
        content: bytes,
    ) -> str:
        """计算内容哈希"""
        return hashlib.sha256(content).hexdigest()


class S3BlobStorage(BlobStorage):
    """
    S3-compatible 对象存储

    用于生产环境
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        bucket: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        self.endpoint = endpoint or os.environ.get("S3_ENDPOINT")
        self.bucket = bucket or os.environ.get("S3_BUCKET", "esragpro-blobs")
        self.access_key = access_key or os.environ.get("S3_ACCESS_KEY")
        self.secret_key = secret_key or os.environ.get("S3_SECRET_KEY")

        # 前缀隔离
        self.prefix = os.environ.get("S3_PREFIX", "blobs")

    def _build_key(self, tenant_id: UUID, key: str, content_hash: str) -> str:
        """构建 S3 键"""
        return f"{self.prefix}/{tenant_id}/{key}/{content_hash}"

    async def put(
        self,
        tenant_id: UUID,
        key: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> str:
        """存储对象"""
        # TODO: 实现实际 S3 上传
        # 当前使用模拟实现
        content_hash = await self.compute_hash(content)
        s3_key = self._build_key(tenant_id, key, content_hash)

        # 返回 S3 存储键
        return f"s3://{self.bucket}/{s3_key}"

    async def get(
        self,
        tenant_id: UUID,
        storage_key: str,
    ) -> bytes:
        """获取对象"""
        # TODO: 实现实际 S3 下载
        raise NotImplementedError("S3 download not implemented")

    async def delete(
        self,
        tenant_id: UUID,
        storage_key: str,
    ) -> bool:
        """删除对象"""
        # TODO: 实现实际 S3 删除
        raise NotImplementedError("S3 delete not implemented")

    async def compute_hash(
        self,
        content: bytes,
    ) -> str:
        """计算内容哈希"""
        return hashlib.sha256(content).hexdigest()


def get_blob_storage() -> BlobStorage:
    """
    获取 Blob 存储实例

    根据环境选择存储类型
    """
    storage_type = os.environ.get("BLOB_STORAGE_TYPE", "local")

    if storage_type == "s3":
        return S3BlobStorage()
    else:
        return LocalBlobStorage()