from __future__ import annotations

from inspect import signature

from app.memory_core.world_memory_projection import (
    ElasticsearchCommand,
    Neo4jCommand,
    build_world_memory_handler_registry,
)


class _RecordingElasticsearchWriter:
    async def __call__(self, command: ElasticsearchCommand) -> None:
        return None


class _RecordingNeo4jWriter:
    async def __call__(self, command: Neo4jCommand) -> None:
        return None


def test_registry_composition_exposes_exact_world_memory_targets() -> None:
    registry = build_world_memory_handler_registry(
        elasticsearch_writer=_RecordingElasticsearchWriter(),
        neo4j_writer=_RecordingNeo4jWriter(),
    )

    assert registry.targets == ("elasticsearch", "neo4j")
    assert registry.get_handler("elasticsearch") is not None
    assert registry.get_handler("neo4j") is not None
    assert registry.get_handler("memory_snapshot") is None


def test_registry_composition_has_no_target_override_parameters() -> None:
    parameters = tuple(signature(build_world_memory_handler_registry).parameters)

    assert parameters == ("elasticsearch_writer", "neo4j_writer")
