"""
Blob Storage Tests - 对象存储测试

验证：
- 租户隔离
- 不可变写入
- 内容哈希
"""
import pytest
import tempfile
from uuid import uuid4

from app.storage.blob import LocalBlobStorage


@pytest.fixture
def temp_storage_path():
    """临时存储路径"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def local_blob(temp_storage_path):
    """本地 Blob 存储"""
    return LocalBlobStorage(base_path=temp_storage_path)


class TestLocalBlobStorage:
    """本地存储测试"""

    @pytest.mark.asyncio
    async def test_put_creates_file(self, local_blob, temp_storage_path):
        """存储对象创建文件"""
        tenant_id = uuid4()
        content = b"test content"

        storage_key = await local_blob.put(
            tenant_id=tenant_id,
            key="test.pdf",
            content=content,
        )

        assert storage_key is not None
        assert str(tenant_id) in storage_key

    @pytest.mark.asyncio
    async def test_get_returns_content(self, local_blob):
        """获取对象返回内容"""
        tenant_id = uuid4()
        content = b"test content"

        storage_key = await local_blob.put(
            tenant_id=tenant_id,
            key="test.pdf",
            content=content,
        )

        retrieved = await local_blob.get(
            tenant_id=tenant_id,
            storage_key=storage_key,
        )

        assert retrieved == content

    @pytest.mark.asyncio
    async def test_delete_removes_file(self, local_blob):
        """删除对象移除文件"""
        tenant_id = uuid4()
        content = b"test content"

        storage_key = await local_blob.put(
            tenant_id=tenant_id,
            key="test.pdf",
            content=content,
        )

        result = await local_blob.delete(
            tenant_id=tenant_id,
            storage_key=storage_key,
        )

        assert result is True

        # 再次获取应该失败
        with pytest.raises(FileNotFoundError):
            await local_blob.get(
                tenant_id=tenant_id,
                storage_key=storage_key,
            )

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, local_blob):
        """租户隔离"""
        tenant1 = uuid4()
        tenant2 = uuid4()
        content = b"tenant1 content"

        storage_key = await local_blob.put(
            tenant_id=tenant1,
            key="test.pdf",
            content=content,
        )

        # tenant2 无法访问 tenant1 的文件
        with pytest.raises(ValueError, match="Tenant mismatch"):
            await local_blob.get(
                tenant_id=tenant2,
                storage_key=storage_key,
            )

    @pytest.mark.asyncio
    async def test_same_content_same_hash(self, local_blob):
        """相同内容相同哈希"""
        content = b"test content"

        hash1 = await local_blob.compute_hash(content)
        hash2 = await local_blob.compute_hash(content)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256 hex digest length

    @pytest.mark.asyncio
    async def test_different_content_different_hash(self, local_blob):
        """不同内容不同哈希"""
        hash1 = await local_blob.compute_hash(b"content1")
        hash2 = await local_blob.compute_hash(b"content2")

        assert hash1 != hash2

    @pytest.mark.asyncio
    async def test_file_not_found(self, local_blob):
        """文件不存在"""
        tenant_id = uuid4()

        with pytest.raises(FileNotFoundError):
            await local_blob.get(
                tenant_id=tenant_id,
                storage_key="nonexistent.pdf/abc123",
            )

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, local_blob):
        """删除不存在文件返回 False"""
        tenant_id = uuid4()

        result = await local_blob.delete(
            tenant_id=tenant_id,
            storage_key="nonexistent.pdf/abc123",
        )

        assert result is False


class TestBlobStoragePath:
    """存储路径测试"""

    def test_tenant_path_isolation(self, temp_storage_path):
        """租户路径隔离"""
        blob = LocalBlobStorage(base_path=temp_storage_path)

        tenant1 = uuid4()
        tenant2 = uuid4()

        path1 = blob._tenant_path(tenant1)
        path2 = blob._tenant_path(tenant2)

        assert path1 != path2
        assert str(tenant1) in str(path1)
        assert str(tenant2) in str(path2)