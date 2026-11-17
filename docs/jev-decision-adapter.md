# TypeSafe AI Jev decision adapter

`backend/app/memory_core/jev_decision_model.py` implements the synchronous
vendor adapter behind the existing `DecisionModel` protocol. It sends one
`POST https://api.typesafe.ai/v1/systemone` request with the pinned
`jev-1.13.0` model, a single `next_action` Choice question, and the legal
actions from the already compiled `DecisionFrame`.

The wire shape follows the official [TypeSafe OpenAPI document](https://api.typesafe.ai/openapi.json):
the request has `state`, `model`, and named `questions`; the response has the
provider `model`, named `answers`, and integer `usage` token counts. The
adapter uses the exact question instructions and reserves
`__adapter_abstain__` as a final Choice criterion. Jev supports up to 255
Choice options according to the [official Jev announcement](https://typesafe.ai/blog/introducing-system-one-models-and-jev),
so this adapter accepts at most 254 legal actions and makes the 255th option
the reserved abstention. A returned legal choice becomes
`SelectedActionDecision`; the reserved choice becomes `AbstainedDecision`.

Jev is a hosted, stateless decision service for this boundary: the request
contains the state for one evaluation and the response contains typed
probabilities and confidence. It does not execute tools or actions. The
adapter never gives Jev an executor and never retries a request. The caller
and router retain ownership of action execution, routing, and memory.

The context limit is set before the adapter by `compile_decision_frame`'s
`max_context_items` budget. Only that bounded frame is externalized. Its
compact UTF-8 state includes the frame identifiers, goal, timestamp, typed
assertions, observations, uncertainties, legal actions, and evidence IDs; it
does not query or serialize the complete memory store. Choice cardinality and
the reserved option are checked before transport, and 255 legal actions fail
closed without a request.

Responses are parsed through strict frozen Pydantic v2 models. The adapter
requires exactly one `next_action` answer, forbids nested response extras,
requires nonnegative strict integer usage counts, checks the pinned response
model, checks that probability keys equal the sent criteria, and validates
finite `[0, 1]` scores, a unit distribution, and maximum-probability choice.
HTTP status errors, transport errors, schema errors, model mismatches, and
request boundary errors are typed and never include the API key, bearer
header, or raw response body.

## Privacy boundary

The state is sent to TypeSafe's hosted API, so deployment owners must decide
whether the bounded frame is permitted to cross that boundary. This adapter
makes no zero-data-retention (ZDR) promise and does not infer ZDR from the
transport code. Review the provider's [privacy policy](https://typesafe.ai/legal/privacy-policy)
and [data-processing addendum](https://typesafe.ai/legal/data-processing) for
retention, processing, and transfer terms before sending sensitive content.
Keep API keys outside logs and error messages; the adapter only places the key
in the outbound Authorization header owned by the caller's HTTP client.

## Validation status

All adapter tests use `httpx.MockTransport` or an injected fake transport; no
live TypeSafe request is part of the test suite. No live smoke result is
claimed because `TYPESAFE_API_KEY` is absent in this environment. A later
benchmark should record the pinned response model, latency percentiles, error
rate, candidate count, input size, action accuracy, abstain accuracy, and
confidence calibration.
