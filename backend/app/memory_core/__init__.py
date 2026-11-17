"""
Agent Memory Core (M1-M8)

Memory bounded context for Agent Memory System.
Independent from MCP V1 Principal Scope.

Key Components:
- Identity mapping: (tenant_id, issuer, subject_id) → principal_id
- AgentAuthorizationContext: internal authorization (legacy, not for MCP)
- MemoryAccessContext: memory access control (legacy, not for MCP)
- MemoryObservation: immutable observation events
- ExtractedCandidateDraft: LLM-generated candidates
- MemoryItem: active memory items
- EvidencePacket: evidence references
- MemoryContextPack: memory context for LLM
- AgentContextBundle: full agent context

Phase 1-U: Unpermissioned contracts for Agent Memory Plane
- UnpermissionedMemoryQuery: query without auth requirement
- EvidenceRef: stable evidence reference with traceability
- ProvenanceInfo: source tracking for evidence
- InsufficientEvidenceError: fail-closed exception
"""

from .candidate_adapters import (
    ElasticsearchCandidateSource,
    GraphCandidateSource,
    RedisHotStateCandidateSource,
)

# P2: Query coordinator + candidate source protocol.
from .candidate_source import (
    CandidateProposal,
    CandidateSource,
    CandidateSourceKind,
    CandidateSourceResult,
)
from .contracts import (
    AgentAuthorizationContext,
    ExtractedCandidateDraft,
    MemoryAccessContext,
    MemoryObservation,
)
from .contracts import (
    AgentContextBundle as LegacyAgentContextBundle,
)
from .contracts import (
    EvidencePacket as LegacyEvidencePacket,
)
from .contracts import (
    MemoryContextPack as LegacyMemoryContextPack,
)
from .contracts import (
    MemoryItem as LegacyMemoryItem,
)
from .corpus import CorpusItem, load_corpus, write_corpus
from .eval_gate import (
    ProjectionHealth,
    ReleaseDecision,
    ReleaseThresholds,
    evaluate_release_gate,
)
from .eval_metrics import EvaluationMeasurement
from .evaluator import (
    EvaluationRunner,
    FakeRetrievalAdapter,
    RetrievalAdapter,
)

# P5: Evaluation and release-gate.
from .holdout import (
    HoldoutCountError,
    HoldoutIntegrityError,
    HoldoutManifest,
    HoldoutQueryItem,
    HoldoutValidationError,
    SealedHoldoutDataset,
    load_holdout,
    write_holdout,
)
from .query_coordinator import MemoryQueryCoordinator

# Phase 1-U: Unpermissioned contracts for Agent Memory Plane
from .unpermissioned_contracts import (
    AgentContextBundle,
    EvidencePacket,
    # Core contracts (no auth required)
    EvidenceRef,
    EvidenceVerificationError,
    # Exceptions (fail-closed)
    InsufficientEvidenceError,
    MemoryContextPack,
    MemoryItem,
    MemoryItemStatus,
    MemoryNotFoundError,
    MemoryQueryResult,
    Observation,
    ProvenanceInfo,
    QueryStatus,
    UnpermissionedMemoryQuery,
)
from .unpermissioned_repository import (
    InMemoryUnpermissionedMemoryRepository,
    UnpermissionedMemoryRepositoryPort,
    create_reference_repository,
    get_reference_repository,
)
from .unpermissioned_retrieval import (
    UnpermissionedSmartRetrievalService,
    create_unpermissioned_retrieval_service,
)

# P7: Dynamic World Memory Contract Kernel
from .world_memory import (
    Hypothesis,
    HypothesisStatus,
    InvalidTransitionError,
    InvalidValidityIntervalError,
    MissingEvidenceError,
    RevocationRecord,
    TransitionRecord,
    ValidityInterval,
    VerifiedFact,
    WorldContextItem,
    WorldContextItemKind,
    WorldContextPack,
    WorldObservation,
    WorldState,
    fuse_world_state,
    refute_hypothesis,
    revoke_fact,
    verify_hypothesis,
)

# P8-A: World-memory replay and projection mapping
from .world_memory_replay import (
    EnvelopeValidationError,
    IdempotencyConflictError,
    ReplayConflict,
    ReplayResult,
    ValidatedEnvelope,
    map_to_projection_event,
    replay_envelopes,
    validate_world_memory_envelope,
)

# P7-B: World-memory persistence bridge
from .world_memory_serializer import (
    AGGREGATE_TYPE,
    EVENT_CONTEXT_PACKED,
    EVENT_FACT_REVOKED,
    EVENT_FACT_VERIFIED,
    EVENT_HYPOTHESIS_REFUTED,
    EVENT_HYPOTHESIS_VERIFIED,
    EVENT_OBSERVATION_INGESTED,
    EVENT_STATE_FUSED,
    KIND_CONTEXT_PACK,
    KIND_FACT,
    KIND_HYPOTHESIS,
    KIND_OBSERVATION,
    KIND_REVOCATION,
    KIND_STATE,
    KIND_TRANSITION,
    P7_SCHEMA_VERSION,
    build_world_memory_envelope,
    build_world_memory_idempotency_key,
    deserialize_hypothesis,
    deserialize_revocation_record,
    deserialize_transition_record,
    deserialize_verified_fact,
    deserialize_world_context_item,
    deserialize_world_context_pack,
    deserialize_world_memory,
    deserialize_world_observation,
    deserialize_world_state,
    serialize_hypothesis,
    serialize_revocation_record,
    serialize_transition_record,
    serialize_verified_fact,
    serialize_world_context_item,
    serialize_world_context_pack,
    serialize_world_observation,
    serialize_world_state,
)

__all__ = [  # noqa: RUF022 — grouped by phase, not alphabetical
    "AgentAuthorizationContext",
    "AgentContextBundle",
    "CandidateProposal",
    # P2: Query coordinator
    "CandidateSource",
    "CandidateSourceKind",
    "CandidateSourceResult",
    "CorpusItem",
    # P2: Concrete candidate-source adapters
    "ElasticsearchCandidateSource",
    "EvaluationMeasurement",
    "EvaluationRunner",
    "EvidencePacket",
    # Phase 1-U: Unpermissioned contracts
    "EvidenceRef",
    "EvidenceVerificationError",
    "ExtractedCandidateDraft",
    "FakeRetrievalAdapter",
    "GraphCandidateSource",
    "HoldoutCountError",
    "HoldoutIntegrityError",
    "HoldoutManifest",
    # P5: Evaluation and release-gate
    "HoldoutQueryItem",
    "HoldoutValidationError",
    "InMemoryUnpermissionedMemoryRepository",
    # Exceptions
    "InsufficientEvidenceError",
    "LegacyAgentContextBundle",
    "LegacyEvidencePacket",
    "LegacyMemoryContextPack",
    # Legacy aliases (re-exports for downstream consumers)
    "LegacyMemoryItem",
    "MemoryAccessContext",
    "MemoryContextPack",
    "MemoryItem",
    "MemoryItemStatus",
    "MemoryNotFoundError",
    # Legacy contracts (with auth context)
    "MemoryObservation",
    "MemoryQueryCoordinator",
    "MemoryQueryResult",
    "Observation",
    "ProjectionHealth",
    "ProvenanceInfo",
    "QueryStatus",
    "RedisHotStateCandidateSource",
    "ReleaseDecision",
    "ReleaseThresholds",
    "RetrievalAdapter",
    "SealedHoldoutDataset",
    "UnpermissionedMemoryQuery",
    # Repository
    "UnpermissionedMemoryRepositoryPort",
    # Services
    "UnpermissionedSmartRetrievalService",
    "create_reference_repository",
    "create_unpermissioned_retrieval_service",
    "evaluate_release_gate",
    "get_reference_repository",
    "load_corpus",
    "load_holdout",
    "write_corpus",
    "write_holdout",
    # P7: Dynamic World Memory Contract Kernel
    "Hypothesis",
    "HypothesisStatus",
    "InvalidTransitionError",
    "InvalidValidityIntervalError",
    "MissingEvidenceError",
    "RevocationRecord",
    "TransitionRecord",
    "ValidityInterval",
    "VerifiedFact",
    "WorldContextItem",
    "WorldContextItemKind",
    "WorldContextPack",
    "WorldObservation",
    "WorldState",
    "fuse_world_state",
    "refute_hypothesis",
    "revoke_fact",
    "verify_hypothesis",
    # P7-B: World-memory persistence bridge
    "AGGREGATE_TYPE",
    "EVENT_CONTEXT_PACKED",
    "EVENT_FACT_REVOKED",
    "EVENT_FACT_VERIFIED",
    "EVENT_HYPOTHESIS_REFUTED",
    "EVENT_HYPOTHESIS_VERIFIED",
    "EVENT_OBSERVATION_INGESTED",
    "EVENT_STATE_FUSED",
    "KIND_CONTEXT_PACK",
    "KIND_FACT",
    "KIND_HYPOTHESIS",
    "KIND_OBSERVATION",
    "KIND_REVOCATION",
    "KIND_STATE",
    "KIND_TRANSITION",
    "P7_SCHEMA_VERSION",
    "build_world_memory_envelope",
    "build_world_memory_idempotency_key",
    "deserialize_hypothesis",
    "deserialize_revocation_record",
    "deserialize_transition_record",
    "deserialize_verified_fact",
    "deserialize_world_context_item",
    "deserialize_world_context_pack",
    "deserialize_world_memory",
    "deserialize_world_observation",
    "deserialize_world_state",
    "serialize_hypothesis",
    "serialize_revocation_record",
    "serialize_transition_record",
    "serialize_verified_fact",
    "serialize_world_context_item",
    "serialize_world_context_pack",
    "serialize_world_observation",
    "serialize_world_state",
    # P8-A: World-memory replay and projection mapping
    "EnvelopeValidationError",
    "IdempotencyConflictError",
    "ReplayConflict",
    "ReplayResult",
    "ValidatedEnvelope",
    "map_to_projection_event",
    "replay_envelopes",
    "validate_world_memory_envelope",
]
