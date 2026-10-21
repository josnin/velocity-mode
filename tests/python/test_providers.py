import asyncio
import json

import httpx
import pytest

from vmode.protocol import Message, ProviderError, ToolCall
from vmode.providers import (
    HuggingFaceProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    _sse_data,
)


@pytest.mark.asyncio
async def test_sse_parser_handles_fragmented_frames_and_utf8():
    async def chunks():
        for chunk in [
            b'data: {"choices":[{"delta":{"content":"caf\xc3',
            b'\xa9"}}]}\r\n\r',
            b'\ndata: [DONE]\n\n',
        ]:
            yield chunk

    assert [frame async for frame in _sse_data(chunks())] == [
        '{"choices":[{"delta":{"content":"café"}}]}',
        "[DONE]",
    ]


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_huggingface_stream_parses_text_tool_calls_usage_and_request():
    captured = {}
    frames = [
        {"choices": [{"delta": {"content": "Reading "}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "id-1", "function": {"name": "read_file", "arguments": "{\"path\":"}},
        ]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"README.md\"}"}},
        ]}, "finish_reason": "tool_calls"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        },
    ]
    data = b"".join(
        b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames
    ) + b"data: [DONE]\n\n"
    chunks = [data[i : i + 7] for i in range(0, len(data), 7)]

    async def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, stream=ByteStream(chunks))

    provider = HuggingFaceProvider(
        "vendor/model:fastest",
        supports_tool_calling=True,
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    events = [
        event
        async for event in provider.stream(
            [Message("user", "Inspect @README.md")],
            [{"type": "function", "function": {"name": "read_file"}}],
        )
    ]
    assert [event.text for event in events if event.kind == "text"] == ["Reading "]
    response = events[-1]
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.tool_calls[0].name == "read_file"
    assert response.usage.input_tokens == 12
    assert response.usage.cached_input_tokens == 5
    assert captured["url"] == "https://router.huggingface.co/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer fake-token"
    assert captured["body"]["stream"] is True
    assert captured["body"]["model"] == "vendor/model:fastest"


@pytest.mark.asyncio
async def test_huggingface_accepts_null_and_single_object_tool_call_deltas():
    data = (
        b'data: {"choices":[{"delta":{"content":"Checking","tool_calls":null}}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":{"index":0,"id":"id-1",'
        b'"function":{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}},'
        b'"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def handler(request):
        return httpx.Response(200, stream=ByteStream([data]))

    provider = HuggingFaceProvider(
        "vendor/model:fastest",
        supports_tool_calling=True,
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    events = [
        event
        async for event in provider.stream([Message("user", "Inspect README.md")], [])
    ]

    assert [event.text for event in events if event.kind == "text"] == ["Checking"]
    assert events[-1].tool_calls == [
        ToolCall("id-1", "read_file", {"path": "README.md"})
    ]


@pytest.mark.asyncio
async def test_huggingface_missing_token_and_credits_are_actionable():
    provider = HuggingFaceProvider("vendor/model", token="")
    with pytest.raises(ProviderError, match="HF_TOKEN"):
        async for _ in provider.stream([Message("user", "hello")], []):
            pass

    async def handler(request):
        return httpx.Response(402, text="credits unavailable")

    provider = HuggingFaceProvider(
        "vendor/model", token="fake-token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderError, match="credits"):
        async for _ in provider.stream([Message("user", "hello")], []):
            pass


@pytest.mark.asyncio
async def test_huggingface_model_not_found_explains_provider_routing():
    async def handler(request):
        return httpx.Response(400, json={"error": "model not found"})

    provider = HuggingFaceProvider(
        "microsoft/Phi-3-mini-4k-instruct:cheapest",
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match="Inference Providers mapping") as error:
        async for _ in provider.stream([Message("user", "hello")], []):
            pass
    assert ":featherless-ai" in str(error.value)
    assert "model not found" in str(error.value)


@pytest.mark.asyncio
async def test_huggingface_invalid_provider_route_is_actionable():
    async def handler(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "the provider or policy you attempted to specify "
                        "'featherless-ui' is not valid."
                    )
                }
            },
        )

    provider = HuggingFaceProvider(
        "openai/gpt-oss-120b:featherless-ui",
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match="provider route") as error:
        async for _ in provider.stream([Message("user", "hello")], []):
            pass
    assert "Inference Providers mapping" in str(error.value)
    assert ":cheapest" in str(error.value)


@pytest.mark.asyncio
async def test_huggingface_rejects_empty_successful_response():
    data = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def handler(request):
        return httpx.Response(200, stream=ByteStream([data]))

    provider = HuggingFaceProvider(
        "openai/gpt-oss-120b:cheapest",
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match="without text or tool calls"):
        async for _ in provider.stream([Message("user", "hello")], []):
            pass


@pytest.mark.asyncio
async def test_huggingface_redacts_token_split_across_stream_deltas():
    frames = [
        {"choices": [{"delta": {"content": "before fake-"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "token after"}, "finish_reason": "stop"}]},
    ]
    data = b"".join(
        b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames
    ) + b"data: [DONE]\n\n"

    async def handler(request):
        return httpx.Response(200, stream=ByteStream([data]))

    provider = HuggingFaceProvider(
        "vendor/model",
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    text = "".join([
        event.text
        async for event in provider.stream([Message("user", "hello")], [])
        if event.kind == "text"
    ])
    assert text == "before ****** after"
    assert "fake-token" not in text


@pytest.mark.asyncio
async def test_huggingface_retries_transient_errors_once_and_never_retries_auth_or_credit():
    attempts = 0
    success = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def retry_handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": "0"}, text="temporary")
        return httpx.Response(200, stream=ByteStream([success]))

    provider = HuggingFaceProvider(
        "vendor/model",
        token="fake-token",
        transport=httpx.MockTransport(retry_handler),
    )
    events = [
        event async for event in provider.stream([Message("user", "hello")], [])
    ]
    assert attempts == 2
    assert "".join(event.text for event in events if event.kind == "text") == "ok"

    for status, body, expected in (
        (401, "invalid token", "HF_TOKEN"),
        (429, "insufficient credits", "credits"),
        (500, "inference credits unavailable", "credits"),
    ):
        attempts = 0

        async def failure_handler(request, *, status=status, body=body):
            nonlocal attempts
            attempts += 1
            return httpx.Response(status, text=body)

        provider = HuggingFaceProvider(
            "vendor/model",
            token="fake-token",
            transport=httpx.MockTransport(failure_handler),
        )
        with pytest.raises(ProviderError, match=expected):
            async for _ in provider.stream([Message("user", "hello")], []):
                pass
        assert attempts == 1


@pytest.mark.asyncio
async def test_huggingface_rejects_non_https_or_credential_urls():
    for url in ("http://localhost/v1", "https://user:pass@example.com/v1"):
        provider = HuggingFaceProvider(base_url=url, model="model", token="fake-token")
        with pytest.raises(ProviderError, match="HTTPS"):
            async for _ in provider.stream([Message("user", "hello")], []):
                pass


@pytest.mark.asyncio
async def test_huggingface_does_not_assume_tool_calling_support():
    captured = {}
    frame = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    data = b"data: " + json.dumps(frame).encode() + b"\n\ndata: [DONE]\n\n"

    async def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, stream=ByteStream([data]))

    provider = HuggingFaceProvider(
        "vendor/model",
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match="tool calling is not enabled"):
        async for _ in provider.stream(
            [Message("user", "read a file")],
            [{"type": "function", "function": {"name": "read_file"}}],
        ):
            pass
    assert "tools" not in captured["body"]


@pytest.mark.asyncio
async def test_huggingface_rejects_malformed_or_interrupted_sse_streams():
    valid_frame = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    for data, expected in (
        (b"data: []\n\ndata: [DONE]\n\n", "Malformed SSE event"),
        (valid_frame, "ended before its completion marker"),
    ):
        async def handler(request, *, data=data):
            return httpx.Response(200, stream=ByteStream([data]))

        provider = HuggingFaceProvider(
            "vendor/model",
            token="fake-token",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(ProviderError, match=expected):
            async for _ in provider.stream([Message("user", "hello")], []):
                pass


@pytest.mark.asyncio
async def test_huggingface_redacts_token_from_http_error_details():
    async def handler(request):
        return httpx.Response(400, text="request rejected with bearer fake-token")

    provider = HuggingFaceProvider(
        "vendor/model",
        token="fake-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError) as error:
        async for _ in provider.stream([Message("user", "hello")], []):
            pass
    assert "fake-token" not in str(error.value)


@pytest.mark.asyncio
async def test_openai_compatible_provider_uses_generic_key_and_parses_sse(monkeypatch):
    captured = {}
    data = (
        b'data: {"choices":[{"delta":{"content":"compatible"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, stream=ByteStream([data]))

    monkeypatch.setenv("VMODE_API_KEY", "generic-fake-key")
    provider = OpenAICompatibleProvider(
        "vendor/model",
        "https://api.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    events = [
        event async for event in provider.stream([Message("user", "hello")], [])
    ]

    assert provider.name == "openai-compatible"
    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer generic-fake-key"
    assert "".join(event.text for event in events if event.kind == "text") == "compatible"


@pytest.mark.asyncio
async def test_openai_compatible_provider_requires_explicit_key_and_tool_capability():
    provider = OpenAICompatibleProvider(
        "vendor/model", "https://api.example.test/v1", token=""
    )
    with pytest.raises(ProviderError, match="VMODE_API_KEY"):
        async for _ in provider.stream([Message("user", "hello")], []):
            pass

    data = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"read_file","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def handler(request):
        return httpx.Response(200, stream=ByteStream([data]))

    provider = OpenAICompatibleProvider(
        "vendor/model",
        "https://api.example.test/v1",
        token="fake-key",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match="tool calling is not enabled"):
        async for _ in provider.stream([Message("user", "read file")], []):
            pass


@pytest.mark.asyncio
async def test_ollama_uses_local_http_without_an_api_key():
    captured = {}
    data = (
        b'data: {"choices":[{"delta":{"content":"local"},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, stream=ByteStream([data]))

    provider = OllamaProvider(
        "llama3.2",
        transport=httpx.MockTransport(handler),
    )
    events = [
        event async for event in provider.stream([Message("user", "hello")], [])
    ]

    assert provider.name == "ollama"
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert captured["authorization"] is None
    assert "".join(event.text for event in events if event.kind == "text") == "local"


@pytest.mark.asyncio
async def test_ollama_allows_remote_http_endpoints():
    captured = {}
    data = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, stream=ByteStream([data]))

    provider = OllamaProvider(
        "llama3.2",
        base_url="http://192.168.64.1:11434/v1",
        transport=httpx.MockTransport(handler),
    )
    async for _ in provider.stream([Message("user", "hello")], []):
        pass

    assert captured["url"] == "http://192.168.64.1:11434/v1/chat/completions"


@pytest.mark.asyncio
async def test_ollama_rejects_unsafe_endpoint_urls():
    for url in (
        "ftp://ollama.example.test/v1",
        "http://token@192.168.64.1:11434/v1",
        "http://192.168.64.1:11434/v1?token=value",
    ):
        provider = OllamaProvider("llama3.2", base_url=url)
        with pytest.raises(ProviderError, match="HTTP or HTTPS"):
            async for _ in provider.stream([Message("user", "hello")], []):
                pass
