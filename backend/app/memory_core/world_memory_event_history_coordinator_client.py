from __future__ import annotations

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.memory_core.world_memory_event_history_coordinator import (
    EVENT_HISTORY_COORDINATOR_UNAVAILABLE,
    EventHistoryCoordinatorRequest,
    EventHistoryCoordinatorResponse,
)

_coordinator_http_client: httpx.AsyncClient | None = None
_coordinator_http_url: str | None = None


def world_memory_event_history_coordinator_configured() -> bool:
    configured_url = settings.WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_URL
    return bool(configured_url and configured_url.strip())


def _configured_url() -> str | None:
    configured_url = settings.WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_URL
    if configured_url is None:
        return None
    normalized_url = configured_url.strip()
    return normalized_url or None


async def get_world_memory_event_history_coordinator_client() -> httpx.AsyncClient | None:
    global _coordinator_http_client, _coordinator_http_url
    configured_url = _configured_url()
    if configured_url is None:
        return None
    if _coordinator_http_client is not None and _coordinator_http_url == configured_url:
        return _coordinator_http_client
    if _coordinator_http_client is not None:
        await _coordinator_http_client.aclose()
    _coordinator_http_client = httpx.AsyncClient(
        timeout=settings.WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_TIMEOUT_S,
    )
    _coordinator_http_url = configured_url
    return _coordinator_http_client


async def close_world_memory_event_history_coordinator_client() -> None:
    global _coordinator_http_client, _coordinator_http_url
    client = _coordinator_http_client
    _coordinator_http_client = None
    _coordinator_http_url = None
    if client is not None:
        await client.aclose()


async def query_world_memory_event_history_coordinator(
    request: EventHistoryCoordinatorRequest,
) -> EventHistoryCoordinatorResponse:
    client = await get_world_memory_event_history_coordinator_client()
    configured_url = _configured_url()
    if client is None or configured_url is None:
        return EventHistoryCoordinatorResponse(
            refusal_reason=EVENT_HISTORY_COORDINATOR_UNAVAILABLE,
        )

    try:
        response = await client.post(
            configured_url,
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return EventHistoryCoordinatorResponse.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError, TypeError, ValueError):
        return EventHistoryCoordinatorResponse(
            refusal_reason=EVENT_HISTORY_COORDINATOR_UNAVAILABLE,
        )


__all__ = [
    "close_world_memory_event_history_coordinator_client",
    "get_world_memory_event_history_coordinator_client",
    "query_world_memory_event_history_coordinator",
    "world_memory_event_history_coordinator_configured",
]
