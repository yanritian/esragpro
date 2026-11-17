"""
Application configuration using Pydantic Settings
"""

import os
import pathlib
from functools import lru_cache
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings


def _load_local_config() -> dict[str, str]:
    """从本地配置文件加载配置"""
    config: dict[str, str] = {}
    try:
        config_path = pathlib.Path(__file__).parent.parent.parent / "config.local.py"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                exec(f.read(), config)  # noqa: S102
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
        return config
    return config


_local_config = _load_local_config()


class Settings(BaseSettings):
    """Application settings"""

    # App
    APP_NAME: str = "esRAGPro"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True

    # API
    API_PREFIX: str = "/api/v1"
    SECRET_KEY: str = "your-secret-key-change-in-production"

    # Elasticsearch
    ES_HOST: str = "http://localhost:9200"
    ES_USER: str | None = None
    ES_PASSWORD: str | None = None
    ES_INDEX_NAME: str = "knowledge_base"

    # PostgreSQL
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/esragpro"
    DATABASE_POOL_SIZE: int = Field(default=20, ge=1, le=200)
    DATABASE_MAX_OVERFLOW: int = Field(default=20, ge=0, le=200)

    ES_CONNECTIONS_PER_NODE: int = Field(default=32, ge=1, le=200)

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Neo4j 知识图谱
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = _local_config.get("NEO4J_PASSWORD", "esragpro123")
    NEO4J_ENABLED: bool = True

    # Embedding
    EMBEDDING_PROVIDER: str = os.environ.get("EMBEDDING_PROVIDER") or _local_config.get(
        "EMBEDDING_PROVIDER", "local"
    )
    EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL") or _local_config.get(
        "EMBEDDING_MODEL", "BAAI/bge-m3"
    )
    EMBEDDING_MODEL_REVISION: str = "5617a9f61b028005a4858fdac845db406aefb181"
    EMBEDDING_MODEL_PATH: str | None = None
    EMBEDDING_LOCAL_FILES_ONLY: bool = False
    EMBEDDING_OFFLINE: bool = False
    EMBEDDING_DIMENSION: int = 1024
    EMBEDDING_API_KEY: str | None = None
    # Ollama embedding provider (EMBEDDING_PROVIDER=ollama). EMBEDDING_MODEL is
    # the Ollama model tag, e.g. "bge-m3" (1024 dims, matches EMBEDDING_DIMENSION).
    # NOTE: containers must use http://host.docker.internal:11434 (set in compose);
    # host-side processes (tests, dev server) use http://127.0.0.1:11434 —
    # override here or via config.local.py if the compose default leaks into
    # a host process through .env.
    EMBEDDING_OLLAMA_URL: str = os.environ.get("EMBEDDING_OLLAMA_URL") or _local_config.get(
        "EMBEDDING_OLLAMA_URL", "http://127.0.0.1:11434"
    )
    EMBEDDING_OLLAMA_TIMEOUT_S: float = 60.0

    # LLM Configuration - 从 config.local.py 或环境变量读取
    LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER") or _local_config.get("LLM_PROVIDER", "qwen")
    LLM_API_KEY: str | None = os.environ.get("LLM_API_KEY") or _local_config.get("LLM_API_KEY", "")
    LLM_MODEL: str | None = os.environ.get("LLM_MODEL") or _local_config.get(
        "LLM_MODEL", "qwen3.6-plus"
    )
    LLM_BASE_URL: str | None = os.environ.get("LLM_BASE_URL") or _local_config.get(
        "LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    # Memory candidate sources (P2) — opt-in
    MEMORY_CANDIDATE_SOURCES_ENABLED: bool = False
    MEMORY_CANDIDATE_SOURCE_TIMEOUT_S: float = 2.0

    WORLD_MEMORY_CANDIDATE_SOURCES_ENABLED: bool = False
    WORLD_MEMORY_ELASTICSEARCH_INDEX_NAME: str = "world-memory-history-v1"
    WORLD_MEMORY_NEO4J_DATABASE: str = "neo4j"
    WORLD_MEMORY_CANDIDATE_SOURCE_TIMEOUT_S: float = Field(
        default=2.0,
        gt=0.0,
        le=30.0,
    )
    WORLD_MEMORY_EVENT_HISTORY_ENABLED: bool = False
    WORLD_MEMORY_EVENT_HISTORY_ASGI_DIAGNOSTICS_ENABLED: bool = False
    WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME: str = "world-memory-events-v2"
    WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_URL: str | None = None
    WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_TIMEOUT_S: float = Field(
        default=2.0,
        gt=0.0,
        le=30.0,
    )

    # RRF Parameters
    RRF_K: int = 60
    RERANK_ENABLED: bool = False

    # Upload
    UPLOAD_DIR: str = "./uploads"
    MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024

    # JWT
    JWT_SECRET_KEY: str = _local_config.get("JWT_SECRET_KEY", "jwt-secret-key-change-in-production")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24
    JWT_ISSUER: str = "https://auth.esragpro.local"
    JWT_AUDIENCE: str = "https://api.esragpro.local"
    BOOTSTRAP_TOKEN: str | None = None
    BOOTSTRAP_TENANT_ID: UUID = UUID("00000000-0000-4000-8000-000000000001")
    BOOTSTRAP_TENANT_NAME: str = "Default Tenant"
    BOOTSTRAP_TENANT_SLUG: str = "default"
    BOOTSTRAP_KNOWLEDGE_BASE_ID: UUID = UUID("00000000-0000-4000-8000-000000000002")
    BOOTSTRAP_KNOWLEDGE_BASE_NAME: str = "Default Knowledge Base"
    BOOTSTRAP_KNOWLEDGE_BASE_SLUG: str = "default"

    class Config:
        env_file = str(pathlib.Path(__file__).parent.parent.parent.parent / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()


settings = get_settings()
