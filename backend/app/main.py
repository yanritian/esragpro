"""
esRAGPro - Enterprise Trusted Knowledge Base Solution
Main application entry point
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Final

from anyio import to_thread
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    agentic_router,
    auth_router,
    evaluation_router,
    graph_router,
    kb_router,
    memory_plane_router,
    memory_router,
    smart_search_router,
    task_router,
    validate_router,
    world_memory_plane_router,
)
from app.api.auth import close_db_pool, get_db_pool
from app.core.config import settings
from app.core.elasticsearch import es_client
from app.core.readiness import (
    ReadinessDependencies,
    ReadinessPayload,
    ReadinessProbe,
    assess_application_readiness,
    create_redis_probe,
    get_migration_heads,
)
from app.memory_core.memory_plane_session import (
    close_session_factory,
    init_session_factory,
)
from app.memory_core.world_memory_candidate_runtime import (
    close_world_memory_candidate_sources,
    get_world_memory_candidate_sources,
)
from app.memory_core.world_memory_event_history_coordinator_client import (
    close_world_memory_event_history_coordinator_client,
)
from app.memory_core.world_memory_event_history_runtime import (
    close_world_memory_event_history_client,
    get_world_memory_event_history_client,
)
from app.services.embedding import EmbeddingModelError, embedding_service

logger = logging.getLogger(__name__)

CORS_ALLOWED_ORIGINS: Final[tuple[str, ...]] = tuple(
    origin
    for raw_origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if (origin := raw_origin.strip()) and origin != "*"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    app.state.embedding_model_ready = False
    await es_client.connect()
    await es_client.create_index(settings.ES_INDEX_NAME)
    await get_db_pool()
    await init_session_factory()
    await get_world_memory_candidate_sources()
    await get_world_memory_event_history_client()
    try:
        await to_thread.run_sync(embedding_service.preflight, abandon_on_cancel=True)
    except EmbeddingModelError:
        logger.exception("Embedding model preflight failed; readiness remains unavailable")
    else:
        app.state.embedding_model_ready = True
    try:
        yield
    finally:
        await close_world_memory_event_history_coordinator_client()
        await close_world_memory_event_history_client()
        await close_world_memory_candidate_sources()
        await es_client.close()
        await close_db_pool()
        await close_session_factory()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Enterprise Trusted Knowledge Base with ES Hybrid Search",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=bool(CORS_ALLOWED_ORIGINS),
    allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
)

# Include routers
app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(kb_router, prefix=settings.API_PREFIX)
app.include_router(agentic_router, prefix=settings.API_PREFIX)
app.include_router(evaluation_router, prefix=settings.API_PREFIX)
app.include_router(graph_router, prefix=settings.API_PREFIX)
app.include_router(validate_router, prefix=settings.API_PREFIX)
app.include_router(memory_plane_router, prefix=settings.API_PREFIX)
app.include_router(world_memory_plane_router, prefix=settings.API_PREFIX)

# Legacy routers (may be unavailable when app.db.session is not present)
if memory_router is not None:
    app.include_router(memory_router, prefix=settings.API_PREFIX)
if task_router is not None:
    app.include_router(task_router, prefix=settings.API_PREFIX)
if smart_search_router is not None:
    app.include_router(smart_search_router, prefix=settings.API_PREFIX)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
    }


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy"}


def _readiness_probe(name: str) -> ReadinessProbe | None:
    probe = getattr(app.state, name, None)
    if isinstance(probe, ReadinessProbe):
        return probe
    return None


def _readiness_dependencies() -> ReadinessDependencies:
    async def elasticsearch_probe() -> None:
        await es_client.client.info()

    async def embedding_model_probe() -> None:
        if getattr(app.state, "embedding_model_ready", False) is not True:
            raise EmbeddingModelError()

    neo4j_probe = _readiness_probe("neo4j_readiness_probe")
    minio_probe = _readiness_probe("minio_readiness_probe")
    return ReadinessDependencies(
        database_pool_factory=get_db_pool,
        elasticsearch_probe=elasticsearch_probe,
        redis_probe=create_redis_probe(settings.REDIS_URL),
        migration_heads_provider=get_migration_heads,
        neo4j_enabled=settings.NEO4J_ENABLED,
        neo4j_probe=neo4j_probe,
        minio_enabled=getattr(app.state, "minio_enabled", False) is True,
        minio_probe=minio_probe,
        embedding_model_probe=embedding_model_probe,
    )


@app.get("/ready")
async def ready() -> ReadinessPayload:
    report = await assess_application_readiness(_readiness_dependencies())
    payload = report.to_payload()
    if not report.ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
    )
