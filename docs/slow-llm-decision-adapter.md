# Slow structured LLM decision adapter

`backend/app/memory_core/slow_llm_decision_model.py` implements a synchronous
`DecisionModel` adapter for OpenAI-compatible `/chat/completions` providers.
It sends one bounded `DecisionFrame`, requests one JSON object, validates the
provider envelope and the inner decision with strict Pydantic v2 models, and
returns the existing `SelectedActionDecision` or `AbstainedDecision` type.

The adapter is advisory only. It receives no executor, does not call tools,
does not write memory, does not retry, and does not fall back internally. The
`DecisionRouter` remains responsible for deciding whether the slow model is
called and calls it at most once.

## Portable wire contract

The request uses `response_format: {"type":"json_object"}` because that is
the common documented format across the target OpenAI-compatible APIs.
OpenAI and Qwen also document JSON Schema structured output for supported
models, while DeepSeek's public JSON-output contract documents JSON-object
mode. Strict local validation is therefore mandatory even when a provider can
constrain generation more strongly.

References:

- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat/create)
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Qwen structured output](https://help.aliyun.com/en/model-studio/qwen-structured-output)

The system message requires JSON-only output, treats instructions embedded in
state as untrusted data, permits only supplied legal action IDs, and requires
abstention when evidence is insufficient. The user message starts with
`DECISION_STATE_JSON` and contains a deterministic compact serialization of
the already bounded frame.

## Failure boundary

The provider envelope must contain a nonblank response model and exactly one
choice with index `0`, nonblank string content, and `finish_reason: "stop"`.
The inner object must be exactly one of these shapes:

```json
{"decision":"select","action_id":"inspect","probability":0.8,"confidence":0.7}
```

```json
{"decision":"abstain","confidence":0.3,"reason":"evidence is insufficient"}
```

Unknown actions, extra fields, malformed or truncated JSON, invalid or
non-finite scores, blank reasons, non-success HTTP statuses, invalid request
construction, and transport failures raise typed adapter errors. Error text
does not include API keys, authorization headers, request state, provider
response bodies, or generated content.

## Privacy and validation status

The bounded frame is sent to the configured hosted provider. Deployment
owners must review that provider's retention and data-processing terms before
sending sensitive state. This adapter makes no zero-data-retention claim.

All current tests are offline and use injected transports or
`httpx.MockTransport`. No live provider smoke result is claimed because this
slice does not configure provider credentials or select a production model.
Provider configuration wiring, live latency and quality benchmarks, action
execution, and persistence remain separate integration slices.
