"""Projection worker package (P1-04 + P6-C).

Production-shaped projection worker for memory_projection_outbox
(P1-04) and world-memory projection outbox (P6-C).
"""

from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    FailureDisposition,
    LeaseLostError,
    PermanentProjectionError,
    ProjectionEvent,
    ProjectionHandler,
    ProjectionHandlerRegistry,
    WorkerResult,
)
from app.memory_core.projection_worker.polling import run_polling_loop
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from app.memory_core.projection_worker.world_memory_store import (
    WorldMemoryProjectionStore,
)
from app.memory_core.projection_worker.world_memory_worker import (
    WorldMemoryProjectionWorker,
)

__all__ = [
    "ClaimedEvent",
    "ExponentialBackoffPolicy",
    "FailureDisposition",
    "LeaseLostError",
    "PermanentProjectionError",
    "ProjectionEvent",
    "ProjectionHandler",
    "ProjectionHandlerRegistry",
    "ProjectionOutboxStore",
    "ProjectionWorker",
    "WorkerResult",
    "WorldMemoryProjectionStore",
    "WorldMemoryProjectionWorker",
    "run_polling_loop",
]
