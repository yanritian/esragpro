"""API module"""
from app.api.auth import router as auth_router
from app.api.kb import router as kb_router
from app.api.validate import router as validate_router
from app.api.graph import router as graph_router
from app.api.evaluation import router as evaluation_router
from app.api.agentic import router as agentic_router
from app.api.memory_plane import router as memory_plane_router
from app.api.world_memory_plane import router as world_memory_plane_router

# Legacy routers that depend on app.db.session (may not be available in all environments).
# These are imported conditionally so that modules without the db dependency
# (e.g. memory_plane) remain importable even when app.db.session is missing.
_LAZY_LEGACY_ROUTERS = {}
try:
    from app.api.memory import router as memory_router
    _LAZY_LEGACY_ROUTERS["memory_router"] = memory_router
except Exception:
    memory_router = None

try:
    from app.api.task import router as task_router
    _LAZY_LEGACY_ROUTERS["task_router"] = task_router
except Exception:
    task_router = None

try:
    from app.api.smart_search import router as smart_search_router
    _LAZY_LEGACY_ROUTERS["smart_search_router"] = smart_search_router
except Exception:
    smart_search_router = None

__all__ = [
    "auth_router",
    "kb_router",
    "validate_router",
    "graph_router",
    "evaluation_router",
    "agentic_router",
    "memory_router",
    "task_router",
    "smart_search_router",
    "memory_plane_router",
    "world_memory_plane_router",
]