from __future__ import annotations

from typing import Self

import pytest
from elasticsearch import AsyncElasticsearch
from neo4j import AsyncDriver

from app.core.config import Settings
from app.memory_core import world_memory_candidate_runtime as runtime


class _FakeElasticsearch(AsyncElasticsearch):
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _FakeDriver(AsyncDriver):
    def __init__(self) -> None:
        self.close_calls = 0

    def session(self, **config: str) -> Self:
        raise AssertionError(f"unexpected session: {config}")

    async def close(self) -> None:
        self.close_calls += 1


def _settings(enabled: bool) -> Settings:
    return Settings(
        WORLD_MEMORY_CANDIDATE_SOURCES_ENABLED=enabled,
        WORLD_MEMORY_ELASTICSEARCH_INDEX_NAME="world-memory-history-v1",
        WORLD_MEMORY_NEO4J_DATABASE="neo4j",
    )


@pytest.mark.asyncio
async def test_disabled_runtime_creates_no_clients() -> None:
    await runtime.close_world_memory_candidate_sources()
    settings = _settings(False)
    created_es: list[_FakeElasticsearch] = []
    created_drivers: list[_FakeDriver] = []

    def make_es(url: str) -> _FakeElasticsearch:
        del url
        client = _FakeElasticsearch()
        created_es.append(client)
        return client

    def make_driver(uri: str, *, auth: tuple[str, str]) -> _FakeDriver:
        del uri, auth
        driver = _FakeDriver()
        created_drivers.append(driver)
        return driver

    original = runtime.settings
    runtime.settings = settings
    try:
        sources = await runtime.get_world_memory_candidate_sources(
            elasticsearch_factory=make_es,
            neo4j_factory=make_driver,
        )
    finally:
        runtime.settings = original

    assert sources == ()
    assert created_es == []
    assert created_drivers == []


@pytest.mark.asyncio
async def test_enabled_runtime_caches_clients_and_closes_once() -> None:
    await runtime.close_world_memory_candidate_sources()
    settings = _settings(True)
    created_es: list[_FakeElasticsearch] = []
    created_drivers: list[_FakeDriver] = []

    def make_es(url: str) -> _FakeElasticsearch:
        del url
        client = _FakeElasticsearch()
        created_es.append(client)
        return client

    def make_driver(uri: str, *, auth: tuple[str, str]) -> _FakeDriver:
        del uri, auth
        driver = _FakeDriver()
        created_drivers.append(driver)
        return driver

    original = runtime.settings
    runtime.settings = settings
    try:
        first = await runtime.get_world_memory_candidate_sources(
            elasticsearch_factory=make_es,
            neo4j_factory=make_driver,
        )
        second = await runtime.get_world_memory_candidate_sources(
            elasticsearch_factory=make_es,
            neo4j_factory=make_driver,
        )
        await runtime.close_world_memory_candidate_sources()
        await runtime.close_world_memory_candidate_sources()
    finally:
        runtime.settings = original

    assert first is second
    assert len(first) == 2
    assert len(created_es) == 1
    assert len(created_drivers) == 1
    assert created_es[0].close_calls == 1
    assert created_drivers[0].close_calls == 1
