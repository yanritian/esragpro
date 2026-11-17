from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, final

from elastic_transport import ObjectApiResponse
from elasticsearch.exceptions import ApiError, TransportError

from .world_memory_projection._write_codec import JsonValue


class _AuthorityCounter(Protocol):
    async def count(self) -> int: ...


class _EventHistoryCounter(Protocol):
    async def count(
        self,
        *,
        index: str,
        query: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]]: ...


class EventHistoryReadinessStatus(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    LAGGING = "lagging"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class EventHistoryReadiness:
    status: EventHistoryReadinessStatus
    authority_count: int | None
    history_count: int | None

    @property
    def is_ready(self) -> bool:
        return self.status is EventHistoryReadinessStatus.READY


def _response_body(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> Mapping[str, JsonValue]:
    if isinstance(response, ObjectApiResponse):
        return response.body
    return response


@final
class EventHistoryReadinessGate:
    def __init__(
        self,
        *,
        enabled: bool,
        authority_repository: _AuthorityCounter,
        client: _EventHistoryCounter,
        index_name: str,
    ) -> None:
        self._enabled = enabled
        self._authority_repository = authority_repository
        self._client = client
        self._index_name = index_name

    async def assess(self) -> EventHistoryReadiness:
        if not self._enabled:
            return EventHistoryReadiness(EventHistoryReadinessStatus.DISABLED, None, None)
        try:
            authority_count, response = await asyncio.gather(
                self._authority_repository.count(),
                self._client.count(
                    index=self._index_name,
                    query={
                        "bool": {
                            "filter": [
                                {"exists": {"field": "authority_envelope_id"}},
                                {"exists": {"field": "has_evidence"}},
                            ]
                        }
                    },
                ),
            )
        except (ApiError, OSError, TransportError):
            return EventHistoryReadiness(EventHistoryReadinessStatus.UNAVAILABLE, None, None)
        history_count = _response_body(response).get("count")
        if (
            isinstance(authority_count, bool)
            or authority_count < 0
            or isinstance(history_count, bool)
            or not isinstance(history_count, int)
            or history_count < 0
        ):
            return EventHistoryReadiness(EventHistoryReadinessStatus.UNAVAILABLE, None, None)
        if authority_count == history_count:
            return EventHistoryReadiness(
                EventHistoryReadinessStatus.READY,
                authority_count,
                history_count,
            )
        return EventHistoryReadiness(
            EventHistoryReadinessStatus.LAGGING,
            authority_count,
            history_count,
        )


__all__ = [
    "EventHistoryReadiness",
    "EventHistoryReadinessGate",
    "EventHistoryReadinessStatus",
]
