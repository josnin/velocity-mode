# Provider contract

Providers implement the asynchronous `Provider.stream(messages, tools)` API
from `src/vmode/providers.py`. They consume provider-independent `Message`
objects, explicitly expose streaming/tool-calling capability metadata, and
yield text deltas followed by one final response event containing tool calls,
finish reason, and optional usage. Provider-specific HTTP, JSON, SSE framing,
and error responses stay inside the adapter.

Messages use `system`, `user`, `assistant`, and `tool` roles. Assistant tool
requests serialize to the Chat Completions function-call shape. Tool results
remain dedicated `tool` messages correlated by `tool_call_id`; do not flatten
them into user text. Tool calls are requests, not authorization: the runtime
checks IDs and dispatches names/arguments only through the fixed registry.

## Hugging Face

`HuggingFaceProvider` uses the shared Chat Completions implementation and
targets the official Inference Providers router:

```text
POST https://router.huggingface.co/v1/chat/completions
```

It sends streaming Chat Completions requests, parses incremental SSE frames
(including fragmented UTF-8/events), aggregates text and tool-call deltas,
parses finish reasons and returned token usage, and maps HTTP/network/protocol
errors to `ProviderError`. Model IDs are passed through exactly, including
routing suffixes. `HF_TOKEN` is read at runtime and sent as an HTTPS Bearer
header only. TLS verification is enabled and redirects are disabled.

One bounded retry is attempted for transient HTTP statuses and non-credit
429 responses, respecting a bounded `Retry-After`. Authentication, credit,
and payment failures are not retried. A returned token count is measured
provider metadata; Velocity-Mode does not estimate cost.

The Responses API, model listing, other Inference Endpoint protocols, and
universal model capability discovery are not implemented. Models and routed
providers may differ in streaming, tool support, context/output limits,
availability, pricing, and response behavior. A successful streaming request
does not imply reliable tool use. The runtime handles calls through its own
fixed tool/approval system and stops at configured iteration/time/output
limits.

All provider tests inject an `httpx` transport and use fake credentials. They
do not make live requests. `vmode --live-smoke` is a separate explicit opt-in
that requires `HF_TOKEN` and `HF_MODEL` if no model is provided through normal
configuration; it may incur usage charges.

## Generic OpenAI-compatible endpoint

`OpenAICompatibleProvider` uses streaming Chat Completions against a
user-configured HTTPS API base URL. It appends `/chat/completions`, reads
bearer credentials from `VMODE_API_KEY`, and redacts the key from streamed
output and reported HTTP error bodies. The adapter is intended for endpoints
documenting compatibility with streaming OpenAI Chat Completions; it is not a
universal provider API translator.

Tool calling is disabled by default and must be explicitly enabled because
compatible endpoints and models vary in function/tool support. Even when
enabled, the user approval layer and fixed tool registry remain authoritative.
Request options, streaming usage events, token limits, error details, routing,
and billing can vary across vendors; the adapter has offline protocol tests
and does not claim live compatibility with every endpoint.

Official references: [Inference Providers](https://huggingface.co/docs/inference-providers/en/index),
[Responses API](https://huggingface.co/docs/inference-providers/guides/responses-api),
and [Hugging Face tokens](https://huggingface.co/docs/hub/main/security-tokens).

## Google Gemini

`GeminiProvider` uses Google's documented OpenAI-compatible streaming Chat
Completions endpoint,
`https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`.
It reads `GEMINI_API_KEY` at runtime and defaults tool calling on, while
retaining the shared adapter's validation, redaction, retry, and HTTP safety
rules. Gemini model availability, free-tier quotas, and function-calling
support vary by model and account; offline tests only verify the documented
request and streaming protocol. Opaque tool-call extension fields, including
Gemini thought signatures, are retained in assistant history and returned
unchanged with the next tool-result request.

Official reference: [Gemini API OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai).
