import asyncio
import concurrent.futures
import logging
import math
from pathlib import Path
from typing import Any

import anyio
import httpx

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)


def _run_blocking_coroutine(coro_fn, *args):
    """Run an async embedding call from sync code.

    Works both with and without a running event loop in the current thread:
    - No loop: anyio.run executes it directly.
    - Loop already running (async callers that use the sync facade): offload
      to a worker thread with its own loop, otherwise anyio.run raises
      "Already running asyncio in this thread".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(coro_fn, *args)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(anyio.run, coro_fn, *args).result()

logger = logging.getLogger(__name__)


class EmbeddingModelError(RuntimeError):
    reason = "embedding_model_unavailable"

    LOCAL_EXTRA_HINT = (
        "local embedding requires the optional 'local-embedding' extra: "
        "pip install -e '.[local-embedding]' (or build the image with LOCAL_EMBEDDING=1)"
    )

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.reason)


class EmbeddingService:
    """Embedding service supporting multiple providers

    torch / sentence-transformers are optional: they are imported lazily and
    only when EMBEDDING_PROVIDER=local. Remote providers (qwen_api, zhipu_api)
    work without them, which keeps the default image small and the process lean.
    """

    def __init__(self, config: Settings | None = None):
        self._model: Any | None = None
        self._settings: Settings = settings if config is None else config
        self._provider: str = self._settings.EMBEDDING_PROVIDER.lower()
        self._device: str = self._detect_device() if self._provider == "local" else "cpu"
        self._preflight_complete: bool = False

    def _detect_device(self) -> str:
        """自动检测可用设备（仅本地嵌入模式会调用；惰性导入 torch）"""
        try:
            import torch
        except ImportError as error:
            raise EmbeddingModelError() from error
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name()
            mem_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
            logger.info(f"GPU 可用: {device_name} ({mem_gb:.1f} GB)")
            return "cuda"
        logger.info("使用 CPU 进行嵌入")
        return "cpu"

    def _model_source(self) -> tuple[str, bool]:
        configured_path = (self._settings.EMBEDDING_MODEL_PATH or "").strip()
        if configured_path:
            model_path = Path(configured_path)
            if not model_path.is_dir():
                raise EmbeddingModelError()
            return str(model_path), True
        if self._settings.EMBEDDING_OFFLINE or self._settings.EMBEDDING_LOCAL_FILES_ONLY:
            raise EmbeddingModelError()
        return self._settings.EMBEDDING_MODEL, False

    def load_model(self) -> Any:
        """Load the local embedding model"""
        if self._model is None:
            model_source, local_files_only = self._model_source()
            logger.info("加载模型 %s 到 %s", model_source, self._device)
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(
                    model_source,
                    device=self._device,
                    trust_remote_code=False,
                    revision=self._settings.EMBEDDING_MODEL_REVISION,
                    local_files_only=local_files_only,
                )
            except ImportError as error:
                raise EmbeddingModelError(
                    f"{EmbeddingModelError.reason}: {EmbeddingModelError.LOCAL_EXTRA_HINT}"
                ) from error
            except (
                AttributeError,
                IndexError,
                KeyError,
                MemoryError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise EmbeddingModelError() from error
        return self._model

    def preflight(self) -> None:
        if self._preflight_complete:
            return
        if self._provider in {"qwen_api", "zhipu_api"}:
            self._preflight_complete = True
            return
        if self._provider == "ollama":
            sample = self._embed_ollama(["embedding model readiness"])
            self._validate_vector(sample[0])
            self._preflight_complete = True
            return
        model = self.load_model()
        try:
            embeddings = model.encode(
                ["embedding model readiness"],
                normalize_embeddings=True,
                device=self._device,
            )
            self._validate_preflight_vector(embeddings)
        except EmbeddingModelError:
            raise
        except (
            AttributeError,
            ImportError,
            IndexError,
            KeyError,
            MemoryError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise EmbeddingModelError() from error
        self._preflight_complete = True

    def _validate_preflight_vector(self, embeddings: Any) -> None:
        try:
            import torch
        except ImportError as error:
            raise EmbeddingModelError(
                f"{EmbeddingModelError.reason}: {EmbeddingModelError.LOCAL_EXTRA_HINT}"
            ) from error
        try:
            matrix = torch.as_tensor(embeddings, dtype=torch.float64)
        except (TypeError, ValueError, RuntimeError) as error:
            raise EmbeddingModelError("embedding_model_unavailable: invalid_embedding") from error
        if matrix.ndim != 2 or matrix.shape[0] != 1:
            raise EmbeddingModelError("embedding_model_unavailable: shape")
        if int(matrix.shape[1]) != self._settings.EMBEDDING_DIMENSION:
            raise EmbeddingModelError("embedding_model_unavailable: dimension")
        if not bool(torch.isfinite(matrix).all()):
            raise EmbeddingModelError("embedding_model_unavailable: finite")
        norms = torch.linalg.vector_norm(matrix, dim=1)
        if not bool(torch.all((norms >= 0.95) & (norms <= 1.05))):
            raise EmbeddingModelError("embedding_model_unavailable: normalization")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a list of texts

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors
        """
        if self._provider == "local":
            return self._embed_local(texts)
        elif self._provider == "qwen_api":
            return self._embed_qwen_api(texts)
        elif self._provider == "zhipu_api":
            return self._embed_zhipu_api(texts)
        elif self._provider == "ollama":
            return self._embed_ollama(texts)
        else:
            # 默认使用本地
            return self._embed_local(texts)

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """本地模型嵌入（GPU/CPU 自动）"""
        model = self.load_model()
        embeddings = model.encode(
            texts,
            normalize_embeddings=True,
            device=self._device,
        )
        return embeddings.tolist()

    def _validate_vector(self, vector: list[float]) -> None:
        """纯 Python 向量校验（供无 torch 的远程/ollama provider 使用）"""
        if len(vector) != self._settings.EMBEDDING_DIMENSION:
            raise EmbeddingModelError("embedding_model_unavailable: dimension")
        if not all(math.isfinite(x) for x in vector):
            raise EmbeddingModelError("embedding_model_unavailable: finite")
        norm = math.sqrt(sum(x * x for x in vector))
        if not (0.95 <= norm <= 1.05):
            raise EmbeddingModelError("embedding_model_unavailable: normalization")

    def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        """Ollama /api/embed 批量嵌入（本地网关，返回前做归一化）"""
        return _run_blocking_coroutine(self._call_ollama_embedding, texts)

    async def _call_ollama_embedding(self, texts: list[str]) -> list[list[float]]:
        """调用 Ollama Embedding API（EMBEDDING_MODEL 即 Ollama 模型标签，如 bge-m3）"""
        base = self._settings.EMBEDDING_OLLAMA_URL.rstrip("/")
        # Ollama 是本机回环网关：绝不经由环境代理转发（trust_env=False），
        # 否则沙箱/企业代理会把 127.0.0.1 请求解析成"未知主机"
        async with httpx.AsyncClient(
            timeout=self._settings.EMBEDDING_OLLAMA_TIMEOUT_S, trust_env=False
        ) as client:
            payload = {"model": self._settings.EMBEDDING_MODEL, "input": texts}
            response = await client.post(f"{base}/api/embed", json=payload)
            if response.status_code != 200:
                detail = response.text[:200]
                # 容错：EMBEDDING_MODEL 默认是 HF 风格 "BAAI/bge-m3"，Ollama 标签是 "bge-m3"
                if "not found" in detail and "/" in self._settings.EMBEDDING_MODEL:
                    fallback = self._settings.EMBEDDING_MODEL.rsplit("/", 1)[-1]
                    retry = await client.post(
                        f"{base}/api/embed",
                        json={"model": fallback, "input": texts},
                    )
                    if retry.status_code == 200:
                        response = retry
                    else:
                        raise EmbeddingModelError(
                            f"embedding_model_unavailable: ollama {response.status_code} {detail}"
                        )
                else:
                    raise EmbeddingModelError(
                        f"embedding_model_unavailable: ollama {response.status_code} {detail}"
                    )
            embeddings = response.json().get("embeddings", [])
        if len(embeddings) != len(texts):
            raise EmbeddingModelError("embedding_model_unavailable: ollama count mismatch")
        normalized: list[list[float]] = []
        for vector in embeddings:
            norm = math.sqrt(sum(x * x for x in vector))
            if norm == 0:
                raise EmbeddingModelError("embedding_model_unavailable: zero vector")
            normalized.append([x / norm for x in vector])
        return normalized

    def _embed_qwen_api(self, texts: list[str]) -> list[list[float]]:
        """阿里通义 Embedding API"""
        api_key = settings.EMBEDDING_API_KEY or settings.LLM_API_KEY
        if not api_key:
            raise ValueError("请配置 EMBEDDING_API_KEY 或 LLM_API_KEY")

        # 通义 embedding API
        return _run_blocking_coroutine(self._call_qwen_embedding, texts, api_key)

    async def _call_qwen_embedding(self, texts: list[str], api_key: str) -> list[list[float]]:
        """调用通义 Embedding API"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "text-embedding-v3",
                    "input": {
                        "texts": texts
                    },
                    "parameters": {
                        "text_type": "document"
                    }
                }
            )

            if response.status_code == 200:
                data = response.json()
                return [item["embedding"] for item in data["output"]["embeddings"]]
            else:
                raise Exception(f"通义 Embedding API 错误: {response.status_code} - {response.text}")

    def _embed_zhipu_api(self, texts: list[str]) -> list[list[float]]:
        """智谱 Embedding API"""
        api_key = settings.EMBEDDING_API_KEY or settings.LLM_API_KEY
        if not api_key:
            raise ValueError("请配置 EMBEDDING_API_KEY 或 LLM_API_KEY")

        return _run_blocking_coroutine(self._call_zhipu_embedding, texts, api_key)

    async def _call_zhipu_embedding(self, texts: list[str], api_key: str) -> list[list[float]]:
        """调用智谱 Embedding API"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://open.bigmodel.cn/api/paas/v4/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "embedding-3",
                    "input": texts
                }
            )

            if response.status_code == 200:
                data = response.json()
                return [item["embedding"] for item in data["data"]]
            else:
                raise Exception(f"智谱 Embedding API 错误: {response.status_code} - {response.text}")

    def embed_query(self, query: str) -> list[float]:
        """
        Embed a single query

        Args:
            query: Query string

        Returns:
            Embedding vector
        """
        return self.embed_texts([query])[0]


# Global embedding service instance
embedding_service = EmbeddingService()
