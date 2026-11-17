# World-state decision kernel

The decision kernel is a small, provider-neutral boundary between passive world
memory and any future decision or execution system. It receives a caller-built,
time-scoped `WorldContextPack`, compiles a bounded `DecisionFrame`, and returns
an explicit model result plus a route. The kernel never executes an action or
changes world memory.

`DecisionKernel` is the supported in-process composition point for this path.
Callers inject already constructed fast and slow `DecisionModel` instances and
a `DecisionRoutePolicy`; `decide` returns the compiled frame, routed decision,
and passive `AgentDecisionHandoff` as one inspectable outcome. Provider
construction, execution, authorization, persistence, API exposure, and
hard-real-time control remain external responsibilities.

## Storage ownership

World memory remains the owner of observations, states, hypotheses, facts, and
evidence references. The authority store keeps the durable record; projections
and hot caches serve retrieval. A caller supplies an already time-scoped
`WorldContextPack` and an `as_of` value. Frame compilation records that value,
uses a stable prefix under the item budget, and preserves evidence IDs in
first-seen order. It does not infer freshness, re-query storage, or rewrite the
pack.

Decision results are ephemeral decision-plane values. If an external system
chooses to persist an outcome, the outcome write-back returns through the world
memory write path so the authority store and its audit trail remain canonical.

## Decision path

The path is deterministic after retrieval:

1. The caller supplies legal actions and an evidence-backed context pack.
2. The compiler rejects empty legal actions, invalid budgets, and retained
   factual state without evidence. Evidence-free retained context also fails
   closed.
3. A fast model is called once. The optional implemented Jev adapter is a
   vendor-neutral boundary for bounded structured choices and returns the same
   immutable decision contract as other fast models. It is offline-verified;
   no configured credential or live-provider result is claimed.
4. A high-risk fast selection goes directly to `human_review`. A sufficiently
   confident low-risk fast selection uses `fast_model`. Abstention, low
   confidence, and every medium-risk fast selection call the slow model once.
5. The slow path can use the implemented OpenAI-compatible structured LLM
   adapter or another `DecisionModel`. High-risk selections, abstentions, and
   below-threshold confidence resolve to `human_review`; an adequately
   confident low- or medium-risk selection is reported as `slow_model`.

The router validates every selected action against the caller's legal action
set. Unknown action IDs are typed contract errors and never trigger a fallback.
The ordered model trace makes escalation inspectable without granting either
model execution authority.

## In-process composition

`DecisionKernel` is the supported provider-neutral library entry point for the
complete in-process path. A caller injects a fast `DecisionModel`, a slow
`DecisionModel`, and `DecisionRoutePolicy`, then passes one
`DecisionFrameRequest` to `decide`. The kernel compiles one frame, routes it
through the fast/slow policy, builds one passive Agent handoff, and returns all
three immutable artifacts in `DecisionKernelOutcome` for inspection and audit.

The composition layer does not choose or construct providers, read environment
configuration, create HTTP clients, retry failures, cache results, serialize an
API response, authorize an action, execute a tool, persist a result, or write an
outcome back to memory. Typed errors from each stage propagate unchanged, so a
failed stage cannot produce a partial outcome or hidden fallback.

## Offline replay and quality gate

`run_decision_replay` evaluates the in-process decision boundary without adding
another provider or execution path. A caller supplies immutable replay cases and
any object implementing the narrow `DecisionSystem.decide` protocol. Each case
declares an expected disposition, route, and legal action ID or abstention. The
runner calls the system once per case in input order and returns immutable
per-case results plus aggregate metrics.

The metrics report exact outcome, disposition, action, route, and evidence
preservation rates using the full case count as the denominator. They also count
fast, slow, and human-review routes and flag every unexpected automatic action
proposal. Evidence is preserved only when the handoff carries a non-empty set of
evidence IDs identical to the compiled frame.

`evaluate_decision_replay` is a deterministic fail-closed gate. Its default
thresholds require perfect exact-outcome accuracy, perfect evidence
preservation, and zero unexpected automatic proposals. An empty report fails.
Model or kernel exceptions propagate unchanged; replay performs no retry or
fallback. File formats, corpus sealing, CLI/report export, wall-clock latency,
provider construction, authorization, execution, persistence, and write-back
remain outside this layer.

## Versioned replay JSON artifact

`build_decision_replay_artifact` provides a pure, provider-neutral projection
of a completed `DecisionReplayReport`, its applied
`DecisionReplayThresholds`, and the result of the fail-closed replay gate. The
returned typed record is JSON-native, preserves replay and gate ordering, and
uses stable enum values. Its top-level `schema_version` is the literal string
`"1"`; callers can use that version to identify the record shape before
encoding it with their chosen JSON transport.

The builder evaluates the existing gate once and then copies every report
item, metric, route count, threshold, verdict, and failed check into fresh
lists and nested records. It does not recalculate metrics or apply independent
gate rules. Evaluation errors propagate unchanged, so a failed gate evaluation
cannot produce a partial artifact.

This projection deliberately excludes JSON text encoding, files, CLI or
report export, timestamps, hashing, corpus loading or sealing, network and
provider calls, configuration, authorization, execution, persistence, and
outcome write-back. Those responsibilities remain with later integration
layers.

## Sealed decision replay dataset

The `backend/eval/decision_kernel` directory is a sealed contract fixture for
the deterministic routing boundary. `manifest.json` identifies the schema,
case count, and lowercase SHA-256 of the exact UTF-8 bytes in `cases.jsonl`.
`load_decision_replay_dataset` verifies that seal, parses every nonblank JSONL
record through strict frozen Pydantic models, rebuilds the existing typed
request, replay case, scripted results, and route policy, and returns immutable
scenarios in file order. Unknown fields, coercions, malformed identifiers or
timestamps, duplicate IDs, unsafe filenames, empty corpora, tampering, and
count mismatches fail closed with typed dataset errors; existing decision
contract errors remain unchanged.

The four committed records cover a confident low-risk fast proposal, a
medium-risk slow-model proposal, a high-risk human-review route, and double
abstention. They contain no provider settings, credentials, network targets,
model calls, execution, persistence, or write-back. These records prove the
kernel's request and routing contracts only. They are separate from live-
provider quality evidence, which requires its own provider, corpus, evaluation,
and operational validation surface and must not be inferred from this sealed
fixture.

## Deterministic dataset runner

`run_decision_replay_dataset` executes a loaded dataset through a real
`DecisionKernel` for each scenario in file order. It supplies the scenario's
already validated immutable fast and slow results and route policy, then passes
the completed report and caller thresholds through the existing replay gate and
JSON-native artifact builder. Kernel, replay, threshold, and artifact errors
propagate unchanged; the runner performs no provider lookup, network call,
retry, authorization, execution, persistence, or outcome write-back.

This is deterministic contract replay. Its artifact is evidence that the
sealed scripted cases satisfy the kernel and replay contracts; it is not live-
model quality evidence and it does not authorize or execute any action. Live
quality claims require a separate provider, corpus, evaluation, and operational
validation surface.

## Provider adapters

`DecisionModel` is the only model boundary. The implemented Jev adapter and a
slow LLM adapter return the same immutable result contract as any local
classifier or deterministic policy. Neither adapter owns storage, policy,
action execution, or retries in this kernel.

## Agent Plane handoff

`AgentDecisionHandoff` is a frozen, provider-neutral envelope that joins a
compiled `DecisionFrame` to its `RoutedDecision`. It preserves frame identity,
the bounded evidence IDs, legal action metadata, route, disposition, result,
and ordered model trace. The builder resolves a selected action to the exact
object in the frame's legal action set, checks evidence and trace consistency,
and rejects route/risk combinations that the kernel cannot safely propose.

The envelope is passive: `execution_authorized` is permanently `False`. It has
no executor, transport, credential, persistence, serialization, or write-back
dependency. An external Agent or control service owns authorization, human
approval, and execution after inspecting the handoff.

An external Agent or control service owns execution after a routed result has
passed its own authorization and human approval checks. That Agent boundary is
deliberately outside these modules and the passive handoff. A provider can
therefore be replaced without changing the evidence, time-scope, legal-action,
or routing contracts.

## Safety boundary

The kernel is advisory and fail closed. It can abstain, escalate uncertainty,
or require human review; it cannot mark a high-risk action as automatic. The
frame compiler, router, and Agent handoff do not mutate `WorldContextPack`,
world-memory records, storage projections, or external systems, and make no
network calls. Optional provider adapters perform only their injected model
request and still have no execution authority.

Hard-real-time control is excluded. Safety controllers that require hard
deadlines must read their dedicated control inputs and enforce their own
constraints without routing through this kernel, an LLM, retrieval, or a
provider adapter. This slice also excludes API routes, database schemas,
provider SDKs, action payloads, runtime configuration wiring, and frontend
behavior.
