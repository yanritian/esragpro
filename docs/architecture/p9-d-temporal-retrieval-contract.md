# P9-D Temporal Retrieval Contract

## Status

Accepted design contract for P9-D implementation slices. This document changes no FastAPI, MCP, database, Elasticsearch, or device-control behavior.

## Why P9-C Stops at Blank Queries

The current P6-G text path is intentionally a **current-object** retrieval path. Candidate stores return snapshot object IDs and the PostgreSQL authority rehydrates them through `resolve_latest`. Therefore a candidate that matched an older event can be returned as that object's newer authority row.

P9-C correctly rejects a nonblank `query` with `event_time_lte`. Allowing that combination before the contracts below exist would label a latest-object result as historical evidence.

The current `world-memory-history-v1` projection is also not a temporal event index: its document ID is the snapshot object ID and its v1 mapping has no indexed top-level authority-envelope ID or `occurred_at`. The opaque `source_envelope` is not a query contract. Neo4j is currently a structural, current-state candidate source and has no verified temporal-graph guarantee.

## Time Axes

| Axis | Authority | Meaning | P9-D use |
| --- | --- | --- | --- |
| Event time | `envelope.occurred_at` | When the represented event happened | Historical event-search cutoff |
| Observation receipt | observation `observed_at` / `ingested_at` | When a source observed and the system received evidence | Provenance only unless a future operation explicitly names it |
| Valid time | state `validity.start` / `validity.end` | Interval in which a state claims to hold | State-snapshot selection |
| Authority record time | durable PostgreSQL row creation/commit time | When authority accepted the record | Audit/provenance only in P9-D |

No P9-D operation may silently substitute one axis for another. Returned envelopes retain their existing source-provided temporal provenance; this memory plane does not derive a freshness, safety, or control decision from it.

## 1. Historical Event Search

A future nonblank text query with an inclusive `event_time_lte` means:

> Search immutable authority-envelope events whose `occurred_at` is less than or equal to the supplied UTC instant.

It does **not** mean a reconstructed state, a valid-time containment query, or the latest version of a matched object.

The historical path has these invariants:

1. Elasticsearch may supply ranked candidate **envelope IDs only**.
2. PostgreSQL rehydrates the exact authoritative envelope IDs selected by the candidate source, preserving source rank without calling `resolve_latest`.
3. The authority rehydration repeats the cutoff, kind, aggregate, and evidence filters before a response is returned.
4. Until a source has an independently verified immutable temporal contract, only the Elasticsearch event index participates. Neo4j must be omitted from the source list rather than treated as a current-state fallback.
5. An unavailable, stale, or incomplete temporal index fails closed. The API exposes the degraded source trace and returns no fabricated/latest-object match.

## 2. Immutable Elasticsearch Event Projection v2

P9-D2 creates a new versioned event index. It must not reinterpret or mutate the v1 current-object projection in place.

Each indexed document represents exactly one accepted authority envelope and contains these top-level indexed fields:

- `envelope_id` — immutable PostgreSQL authority-envelope UUID and ES document identity;
- `object_id` — snapshot-object UUID, when the envelope contains one;
- `aggregate_id` — authority aggregate UUID;
- `object_kind` — the P7-B discriminator;
- `occurred_at` — UTC date used by historical-event filtering;
- immutable searchable `content`, plus evidence identifiers and minimal rank metadata.

The full source envelope can remain an audit payload, but it is never the only place where a field needed by a temporal query exists. The index requires a backfill and an explicit completeness gate before API/MCP exposure. A missing or partially backfilled v2 index is a degraded temporal source, not permission to use v1.

## 3. State Snapshot Is a Separate Operation

A reconstructed-state view is not an event search. A future snapshot operation uses a separate request contract with a required aggregate/state scope and a distinct `valid_at` instant.

It selects authoritative state intervals satisfying:

```text
validity.start <= valid_at
and (validity.end is absent or valid_at < validity.end)
```

If multiple authoritative states remain, deterministic event-time ordering is a tie-breaker only. The operation returns ambiguity or no match when authority does not establish a state; it never infers a missing state from observations, graph relationships, or an LLM. Snapshot responses retain the envelopes that justify them and never reuse the event-search meaning of `event_time_lte`.

## Rejected Alternatives

- **Filter v1 ES documents by opaque `source_envelope` fields.** The mapping does not establish those fields as indexed temporal boundaries.
- **Use an event-time cutoff then call `resolve_latest`.** This converts a historical hit into a current object and breaks evidence provenance.
- **Run Neo4j alongside ES for historical text queries.** Current graph topology has no immutable temporal contract.
- **Call event history an `as_of` state view.** Events and valid-time state intervals answer different questions.
- **Infer state or safety readiness from partial memory.** Planning, device control, and safety controllers remain consumers outside this repository.

## Rollout and Acceptance Gates

### P9-D2: immutable event projection and exact rehydration

Create the v2 mapping/writer, backfill contract, envelope-ID candidate proposal, and PostgreSQL exact-envelope resolver. Acceptance proves an older text match rehydrates that exact older envelope even when the same object has a newer row.

### P9-D3: FastAPI and MCP historical event-search exposure

Expose nonblank `query` plus `event_time_lte` only after the v2 completeness gate is observable. Acceptance proves inclusive cutoff behavior, ES-only source traces, fail-closed degradation, and exact JSON forwarding through real MCP stdio.

### P9-D4: valid-time state snapshot

Implement the separate scoped `valid_at` operation. Acceptance proves interval boundaries, ambiguity/no-match behavior, evidence preservation, and that no event-history endpoint is repurposed as a snapshot.

Every slice requires focused regression tests, type/lint gates, and an independent API/MCP acceptance check. Latency is measured separately for candidate retrieval and authority rehydration; this contract makes no unverified 100ms or zero-hallucination claim.
