"""
ARQ Worker Settings - Worker 配置
"""
import os
from dataclasses import dataclass


@dataclass
class WorkerSettings:
    """Worker 配置"""
    # Redis 配置
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # Worker 配置
    max_concurrent_jobs: int = 4
    job_timeout_seconds: int = 300  # 5 分钟
    retry_delay_seconds: int = 60

    # 投影配置
    es_batch_size: int = 100
    neo4j_batch_size: int = 50

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


def get_worker_settings() -> WorkerSettings:
    """获取 Worker 配置"""
    return WorkerSettings(
        redis_host=os.environ.get("REDIS_HOST", "localhost"),
        redis_port=int(os.environ.get("REDIS_PORT", "6379")),
        redis_db=int(os.environ.get("REDIS_DB", "0")),
        max_concurrent_jobs=int(os.environ.get("WORKER_MAX_JOBS", "4")),
        job_timeout_seconds=int(os.environ.get("WORKER_JOB_TIMEOUT", "300")),
        retry_delay_seconds=int(os.environ.get("WORKER_RETRY_DELAY", "60")),
        es_batch_size=int(os.environ.get("ES_BATCH_SIZE", "100")),
        neo4j_batch_size=int(os.environ.get("NEO4J_BATCH_SIZE", "50")),
    )