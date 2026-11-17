"""
Configuration Management Service (M7)

Environment-based configuration management with secret handling.
"""

import os
import logging
from typing import Dict
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class Environment(str, Enum):
    """Environment types"""
    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass
class DatabaseConfig:
    """Database configuration"""
    host: str = "localhost"
    port: int = 5432
    name: str = "esragpro"
    user: str = "postgres"
    password: str = ""  # Should be from env/secret manager
    pool_size: int = 10
    max_overflow: int = 20

    def get_connection_string(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


@dataclass
class RedisConfig:
    """Redis configuration"""
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str = ""
    key_prefix: str = "memory:"
    default_ttl: int = 3600


@dataclass
class ElasticsearchConfig:
    """Elasticsearch configuration"""
    host: str = "localhost"
    port: int = 9200
    index_name: str = "memory_items"
    username: str = ""
    password: str = ""


@dataclass
class Neo4jConfig:
    """Neo4j configuration"""
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""


@dataclass
class MinIOConfig:
    """MinIO configuration"""
    endpoint: str = "localhost:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket_name: str = "memory-objects"
    secure: bool = False


@dataclass
class MemoryConfig:
    """Memory system configuration"""
    environment: Environment = Environment.DEVELOPMENT
    default_retention_days: int = 365
    enable_audit: bool = True
    enable_dlp: bool = True
    default_token_budget: int = 4000
    default_max_items: int = 10
    min_confidence: float = 0.7


@dataclass
class AppConfig:
    """Application configuration"""
    # Environment
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    log_level: str = "INFO"

    # Database configs
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    elasticsearch: ElasticsearchConfig = field(default_factory=ElasticsearchConfig)
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    minio: MinIOConfig = field(default_factory=MinIOConfig)

    # Memory config
    memory: MemoryConfig = field(default_factory=MemoryConfig)


class ConfigManager:
    """
    Configuration manager with environment-based loading.

    Features:
    - Environment-based config
    - Secret management (env vars)
    - Validation
    - Defaults
    """

    def __init__(self):
        self.config = AppConfig()
        self._load_from_env()

    def _load_from_env(self) -> None:
        """Load configuration from environment variables"""
        # Environment
        env = os.getenv("APP_ENVIRONMENT", "development")
        self.config.environment = Environment(env)

        # Debug
        self.config.debug = os.getenv("APP_DEBUG", "false").lower() == "true"
        self.config.log_level = os.getenv("APP_LOG_LEVEL", "INFO")

        # Database
        self.config.database.host = os.getenv("DB_HOST", self.config.database.host)
        self.config.database.port = int(os.getenv("DB_PORT", str(self.config.database.port)))
        self.config.database.name = os.getenv("DB_NAME", self.config.database.name)
        self.config.database.user = os.getenv("DB_USER", self.config.database.user)
        self.config.database.password = os.getenv("DB_PASSWORD", "")

        # Redis
        self.config.redis.host = os.getenv("REDIS_HOST", self.config.redis.host)
        self.config.redis.port = int(os.getenv("REDIS_PORT", str(self.config.redis.port)))
        self.config.redis.password = os.getenv("REDIS_PASSWORD", "")

        # Elasticsearch
        self.config.elasticsearch.host = os.getenv("ES_HOST", self.config.elasticsearch.host)
        self.config.elasticsearch.port = int(os.getenv("ES_PORT", str(self.config.elasticsearch.port)))
        self.config.elasticsearch.username = os.getenv("ES_USER", "")
        self.config.elasticsearch.password = os.getenv("ES_PASSWORD", "")

        # Neo4j
        self.config.neo4j.uri = os.getenv("NEO4J_URI", self.config.neo4j.uri)
        self.config.neo4j.user = os.getenv("NEO4J_USER", self.config.neo4j.user)
        self.config.neo4j.password = os.getenv("NEO4J_PASSWORD", "")

        # MinIO
        self.config.minio.endpoint = os.getenv("MINIO_ENDPOINT", self.config.minio.endpoint)
        self.config.minio.access_key = os.getenv("MINIO_ACCESS_KEY", "")
        self.config.minio.secret_key = os.getenv("MINIO_SECRET_KEY", "")
        self.config.minio.bucket_name = os.getenv("MINIO_BUCKET", self.config.minio.bucket_name)

        # Memory
        self.config.memory.default_retention_days = int(
            os.getenv("MEMORY_RETENTION_DAYS", str(self.config.memory.default_retention_days))
        )
        self.config.memory.enable_audit = os.getenv("MEMORY_AUDIT", "true").lower() == "true"
        self.config.memory.enable_dlp = os.getenv("MEMORY_DLP", "true").lower() == "true"
        self.config.memory.default_token_budget = int(
            os.getenv("MEMORY_TOKEN_BUDGET", str(self.config.memory.default_token_budget))
        )

        logger.info(f"Configuration loaded for environment: {self.config.environment.value}")

    def get_config(self) -> AppConfig:
        """Get application configuration"""
        return self.config

    def is_production(self) -> bool:
        """Check if running in production"""
        return self.config.environment == Environment.PRODUCTION

    def is_development(self) -> bool:
        """Check if running in development"""
        return self.config.environment == Environment.DEVELOPMENT

    def validate(self) -> bool:
        """Validate configuration"""
        # Check required secrets in production
        if self.is_production():
            if not self.config.database.password:
                logger.error("DB_PASSWORD is required in production")
                return False
            if not self.config.elasticsearch.password:
                logger.error("ES_PASSWORD is required in production")
                return False
            if not self.config.neo4j.password:
                logger.error("NEO4J_PASSWORD is required in production")
                return False
            if not self.config.minio.access_key or not self.config.minio.secret_key:
                logger.error("MINIO credentials are required in production")
                return False

        return True

    def get_connection_strings(self) -> Dict[str, str]:
        """Get all connection strings"""
        return {
            "database": self.config.database.get_connection_string(),
            "redis": f"redis://{self.config.redis.host}:{self.config.redis.port}/{self.config.redis.db}",
            "elasticsearch": f"http://{self.config.elasticsearch.host}:{self.config.elasticsearch.port}",
            "neo4j": self.config.neo4j.uri,
            "minio": f"{'https' if self.config.minio.secure else 'http'}://{self.config.minio.endpoint}",
        }
