from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import os
import email.utils
from datetime import datetime, timezone
from collections.abc import AsyncIterator, Sequence
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .protocol import (
    Message,
    ProviderCapabilities,
    ProviderError,
    ProviderEvent,
    ToolCall,
    Usage,
)


class Provider(Protocol):
    name: str
    model: str
    capabilities: ProviderCapabilities

    def stream(
        self, messages: Sequence[Message], tools: Sequence[dict]
    ) -> AsyncIterator[ProviderEvent]: ...


class MockProvider:
    """Deterministic provider, optionally scripted for a tool-cycle test."""

    name = "mock"

    def __init__(self, model: str = "mock/deterministic", scenario: str = "simple"):
        self.model = model
        self.scenario = scenario
        self.capabilities = ProviderCapabilities(
            streaming=True,
            tool_calling=(scenario == "read-file"),
        )
        self.requests: list[list[Message]] = []
        self._turn = 0

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[dict]
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append(list(messages))
        if self.scenario == "read-file":
            if self._turn == 0:
                for part in ("I’ll inspect ", "the requested file."):
                    await asyncio.sleep(0)
                    yield ProviderEvent("text", text=part)
                yield ProviderEvent(
                    "response",
                    tool_calls=[
                        ToolCall(
                            id="mock-read-1",
                            name="read_file",
                            arguments={"path": _file_requested(messages)},
                        )
                    ],
                    finish_reason="tool_calls",
                )
            else:
                for part in ("The file contains ", "the requested source."):
                    await asyncio.sleep(0)
                    yield ProviderEvent("text", text=part)
                yield ProviderEvent(
                    "response",
                    finish_reason="stop",
                    usage=Usage(input_tokens=18, output_tokens=9),
                )
            self._turn += 1
            return
        task = messages[-1].content.split("\n\nUser-selected file context:", 1)[0]
        answer = f"Mock response: {task}"
        for offset in range(0, len(answer), 12):
            yield ProviderEvent("text", text=answer[offset : offset + 12])
            await asyncio.sleep(0)
        yield ProviderEvent(
            "response", finish_reason="stop", usage=Usage(input_tokens=None, output_tokens=None)
        )


def _file_requested(messages: Sequence[Message]) -> str:
    import re

    found = re.search(r"@([^\s]+)", messages[-1].content)
    return found.group(1) if found else "README.md"


class OpenAICompatibleProvider:
    """Streaming OpenAI Chat Completions-compatible endpoint."""

    name = "openai-compatible"

    def __init__(
        self,
        model: str,
        base_url: str,
        timeout: float = 60.0,
        max_output_tokens: int = 4096,
        supports_tool_calling: bool = False,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        token_env: str = "VMODE_API_KEY",
        provider_label: str = "OpenAI-compatible provider",
        huggingface_routing: bool = False,
        allow_loopback_http: bool = False,
        allow_http: bool = False,
        loopback_only: bool = False,
        requires_token: bool = True,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.token_env = token_env
        self.provider_label = provider_label
        self.huggingface_routing = huggingface_routing
        self.allow_loopback_http = allow_loopback_http
        self.allow_http = allow_http
        self.loopback_only = loopback_only
        self.requires_token = requires_token
        self.capabilities = ProviderCapabilities(
            streaming=True,
            tool_calling=supports_tool_calling,
        )
        self._token = token if token is not None else os.environ.get(token_env, "")
        self._transport = transport

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[dict]
    ) -> AsyncIterator[ProviderEvent]:
        if self.requires_token and not self._token:
            raise ProviderError(f"{self.token_env} is missing. Set it in your environment.")
        try:
            parsed_url = urlsplit(self.base_url)
            parsed_url.port
        except ValueError as exc:
            raise ProviderError(f"{self.provider_label} base URL is invalid.") from exc
        is_loopback = _is_loopback_host(parsed_url.hostname)
        valid_scheme = parsed_url.scheme == "https" or (
            parsed_url.scheme == "http"
            and (self.allow_http or (self.allow_loopback_http and is_loopback))
        )
        if (
            not valid_scheme
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            scheme_requirement = "HTTP or HTTPS" if self.allow_http else "HTTPS"
            raise ProviderError(
                f"{self.provider_label} base URL must use {scheme_requirement} "
                "without URL credentials."
            )
        if self.loopback_only and not is_loopback:
            raise ProviderError(
                f"{self.provider_label} base URL must use a loopback host."
            )
        payload: dict = {
            "model": self.model,
            "messages": [message.as_api_message() for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.max_output_tokens,
        }
        if tools and self.capabilities.tool_calling:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        timeout = httpx.Timeout(self.timeout, connect=min(self.timeout, 15))
        tool_deltas: dict[int, dict] = {}
        finish_reason = None
        usage = None
        redactor = _SecretRedactor(self._token)
        done_received = False
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=timeout, follow_redirects=False
            ) as client:
                attempt = 0
                while True:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code != 200:
                            body = (await response.aread())[:4000].decode(errors="replace")
                            if self._token:
                                body = body.replace(self._token, "******")
                            credit_error = any(word in body.lower() for word in ("credit", "payment"))
                            transient = (
                                response.status_code in {408, 500, 502, 503, 504}
                                and not credit_error
                            )
                            if response.status_code == 429 and not credit_error:
                                transient = True
                            if transient and attempt == 0:
                                await asyncio.sleep(
                                    min(
                                        self.timeout,
                                        _retry_delay(response.headers.get("retry-after")),
                                    )
                                )
                                attempt += 1
                                continue
                            raise ProviderError(
                                _http_error(
                                    response.status_code,
                                    body,
                                    response.headers,
                                    self.provider_label,
                                    self.token_env,
                                    self.huggingface_routing,
                                    self.model,
                                )
                            )
                        async for data in _sse_data(response.aiter_bytes()):
                            if data == "[DONE]":
                                done_received = True
                                break
                            try:
                                item = json.loads(data)
                            except json.JSONDecodeError as exc:
                                raise ProviderError(
                                    f"Malformed SSE JSON from {self.provider_label}."
                                ) from exc
                            if not isinstance(item, dict):
                                raise ProviderError(
                                    f"Malformed SSE event from {self.provider_label}."
                                )
                            raw_usage = item.get("usage")
                            if raw_usage is not None:
                                if not isinstance(raw_usage, dict):
                                    raise ProviderError(
                                        f"Malformed usage metadata from {self.provider_label}."
                                    )
                                input_tokens = raw_usage.get("prompt_tokens")
                                output_tokens = raw_usage.get("completion_tokens")
                                prompt_details = raw_usage.get("prompt_tokens_details") or {}
                                if not isinstance(prompt_details, dict):
                                    raise ProviderError(
                                        f"Malformed prompt usage details from {self.provider_label}."
                                    )
                                cached_input_tokens = prompt_details.get("cached_tokens")
                                if any(
                                    value is not None
                                    and (type(value) is not int or value < 0)
                                    for value in (input_tokens, output_tokens, cached_input_tokens)
                                ):
                                    raise ProviderError(
                                        f"Malformed usage token counts from {self.provider_label}."
                                    )
                                usage = Usage(
                                    input_tokens, output_tokens, cached_input_tokens
                                )
                            choices = item.get("choices", [])
                            if not isinstance(choices, list):
                                raise ProviderError(
                                    f"Malformed choices in {self.provider_label} response."
                                )
                            if not choices:
                                continue
                            choice = choices[0]
                            if not isinstance(choice, dict):
                                raise ProviderError(
                                    f"Malformed choice in {self.provider_label} response."
                                )
                            delta = choice.get("delta", {})
                            if not isinstance(delta, dict):
                                raise ProviderError(
                                    f"Malformed response delta from {self.provider_label}."
                                )
                            text = delta.get("content")
                            if text is not None and not isinstance(text, str):
                                raise ProviderError("Unsupported non-text response content.")
                            if isinstance(text, str) and text:
                                if self._token:
                                    text = text.replace(self._token, "******")
                                safe_text = redactor.feed(text)
                                if safe_text:
                                    yield ProviderEvent("text", text=safe_text)
                            tool_parts = delta.get("tool_calls")
                            if tool_parts is None:
                                tool_parts = []
                            elif isinstance(tool_parts, dict):
                                tool_parts = [tool_parts]
                            if not isinstance(tool_parts, list):
                                raise ProviderError(
                                    f"Malformed tool-call deltas from {self.provider_label}."
                                )
                            for part in tool_parts:
                                if not isinstance(part, dict):
                                    raise ProviderError(
                                        f"Malformed tool-call delta from {self.provider_label}."
                                    )
                                index = part.get("index", 0)
                                if type(index) is not int or index < 0:
                                    raise ProviderError(
                                        f"Malformed tool-call index from {self.provider_label}."
                                    )
                                if index not in tool_deltas and len(tool_deltas) >= 8:
                                    raise ProviderError("Model returned too many tool calls.")
                                current = tool_deltas.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                if part.get("id"):
                                    if not isinstance(part["id"], str):
                                        raise ProviderError(
                                            f"Malformed tool-call ID from {self.provider_label}."
                                        )
                                    current["id"] = part["id"]
                                function = part.get("function", {})
                                if not isinstance(function, dict):
                                    raise ProviderError(
                                        f"Malformed tool-call function from {self.provider_label}."
                                    )
                                name_delta = function.get("name", "")
                                argument_delta = function.get("arguments", "")
                                if not isinstance(name_delta, str) or not isinstance(argument_delta, str):
                                    raise ProviderError(
                                        f"Malformed tool-call fields from {self.provider_label}."
                                    )
                                current["name"] += name_delta
                                current["arguments"] += argument_delta
                                if len(current["arguments"]) > 250_000:
                                    raise ProviderError("Model tool-call arguments exceed the size limit.")
                            choice_finish = choice.get("finish_reason")
                            if choice_finish is not None and not isinstance(choice_finish, str):
                                raise ProviderError(
                                    f"Malformed finish reason from {self.provider_label}."
                                )
                            finish_reason = choice_finish or finish_reason
                    if not done_received:
                        raise ProviderError(
                            f"{self.provider_label} stream ended before its completion marker."
                        )
                    break
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.provider_label} request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.provider_label} connection failed: {type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise ProviderError(
                f"Malformed streaming response from {self.provider_label}."
            ) from exc
        trailing_text = redactor.flush()
        if trailing_text:
            yield ProviderEvent("text", text=trailing_text)
        calls = []
        for index in sorted(tool_deltas):
            item = tool_deltas[index]
            try:
                args = json.loads(item["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    "Model returned malformed tool-call arguments. The response may "
                    "have been truncated; retry with a new or cleared context."
                ) from exc
            if not isinstance(args, dict):
                raise ProviderError("Model tool-call arguments must be a JSON object.")
            calls.append(ToolCall(item["id"], item["name"], args))
        if calls and not self.capabilities.tool_calling:
            raise ProviderError(
                "The model returned tool calls, but tool calling is not enabled for this provider configuration."
            )
        yield ProviderEvent(
            "response", tool_calls=calls, finish_reason=finish_reason, usage=usage
        )


class HuggingFaceProvider(OpenAICompatibleProvider):
    name = "huggingface"

    def __init__(
        self,
        model: str,
        base_url: str = "https://router.huggingface.co/v1",
        timeout: float = 60.0,
        max_output_tokens: int = 4096,
        supports_tool_calling: bool = False,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(
            model,
            base_url,
            timeout,
            max_output_tokens,
            supports_tool_calling,
            token,
            transport,
            token_env="HF_TOKEN",
            provider_label="Hugging Face",
            huggingface_routing=True,
        )

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[dict]
    ) -> AsyncIterator[ProviderEvent]:
        saw_text = False
        try:
            async for event in super().stream(messages, tools):
                if event.kind == "text" and event.text:
                    saw_text = True
                elif event.kind == "response" and not saw_text and not event.tool_calls:
                    raise ProviderError(
                        "Hugging Face completed without text or tool calls. Try a "
                        "named provider route from the model's Inference Providers "
                        "mapping instead of an automatic route such as :cheapest."
                    )
                yield event
        except ProviderError as exc:
            message = str(exc)
            if message.startswith("OpenAI-compatible provider"):
                message = message.replace("OpenAI-compatible provider", "Hugging Face")
            raise ProviderError(message) from exc


class GeminiProvider(OpenAICompatibleProvider):
    """Streaming Google Gemini Developer API OpenAI-compatible endpoint."""

    name = "gemini"

    def __init__(
        self,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
        timeout: float = 60.0,
        max_output_tokens: int = 4096,
        supports_tool_calling: bool = True,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(
            model,
            base_url,
            timeout,
            max_output_tokens,
            supports_tool_calling,
            token,
            transport,
            token_env="GEMINI_API_KEY",
            provider_label="Google Gemini Developer API",
        )

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[dict]
    ) -> AsyncIterator[ProviderEvent]:
        async for event in super().stream(messages, tools):
            if (
                event.kind == "response"
                and event.tool_calls
                and event.finish_reason == "stop"
            ):
                yield ProviderEvent(
                    "response",
                    tool_calls=event.tool_calls,
                    finish_reason="tool_calls",
                    usage=event.usage,
                )
            else:
                yield event


class OllamaProvider(OpenAICompatibleProvider):
    """Streaming Ollama OpenAI-compatible endpoint."""

    name = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434/v1",
        timeout: float = 60.0,
        max_output_tokens: int = 4096,
        supports_tool_calling: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(
            model,
            base_url,
            timeout,
            max_output_tokens,
            supports_tool_calling,
            token="",
            transport=transport,
            token_env="",
            provider_label="Ollama",
            allow_http=True,
            requires_token=False,
        )


def _is_loopback_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class _SecretRedactor:
    def __init__(self, secret: str):
        self.secret = secret
        self.buffer = ""

    def feed(self, value: str) -> str:
        self.buffer += value
        if not self.secret:
            safe, self.buffer = self.buffer, ""
            return safe
        safe_end = max(0, len(self.buffer) - len(self.secret) + 1)
        match_start = self.buffer.find(self.secret)
        if 0 <= match_start < safe_end and match_start + len(self.secret) > safe_end:
            safe_end = match_start
        if safe_end == 0:
            return ""
        safe, self.buffer = self.buffer[:safe_end], self.buffer[safe_end:]
        return safe.replace(self.secret, "******")

    def flush(self) -> str:
        safe, self.buffer = self.buffer, ""
        return safe.replace(self.secret, "******") if self.secret else safe


async def _sse_data(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        if len(buffer) > 4_000_000:
            raise ValueError("SSE frame exceeds limit")
        while True:
            boundary = buffer.find("\n\n")
            cr_boundary = buffer.find("\r\n\r\n")
            lone_cr_boundary = buffer.find("\r\r")
            candidates = [(boundary, 2), (cr_boundary, 4), (lone_cr_boundary, 2)]
            boundary, sep = min(
                (candidate for candidate in candidates if candidate[0] >= 0),
                default=(-1, 0),
                key=lambda candidate: candidate[0],
            )
            if boundary < 0:
                break
            block, buffer = buffer[:boundary], buffer[boundary + sep :]
            data_lines = [
                line[5:].lstrip()
                for line in block.replace("\r\n", "\n").split("\n")
                if line.startswith("data:")
            ]
            if data_lines:
                yield "\n".join(data_lines)
    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        data_lines = [
            line[5:].lstrip()
            for line in buffer.replace("\r\n", "\n").split("\n")
            if line.startswith("data:")
        ]
        if data_lines:
            yield "\n".join(data_lines)


def _http_error(
    status: int,
    body: str,
    headers: httpx.Headers,
    provider_label: str,
    token_env: str,
    huggingface_routing: bool,
    model: str,
) -> str:
    folded = body.lower()
    if status == 401:
        reason = (
            f"{token_env} was rejected. Check that the key is valid and has access to the endpoint."
            if token_env
            else f"{provider_label} rejected the request."
        )
    elif status == 403 or status == 402 or "credit" in folded or "payment" in folded:
        reason = f"{provider_label} access was denied or credits are unavailable. Check endpoint permissions and billing. No retry was performed."
    elif status == 404:
        reason = (
            f"{provider_label} returned HTTP 404 for model {model!r} or its "
            "endpoint. Confirm the model ID and base URL; for Gemini, use the "
            "default /v1beta/openai API prefix rather than the full "
            "/chat/completions URL and an available compatibility model such as "
            f"'gemini-3.8-flash'. Provider response: {body[:500]}"
        )
    elif status == 400 and (
        "model" in folded
        and any(
            phrase in folded
            for phrase in ("not found", "does not exist", "unknown model")
        )
    ):
        if huggingface_routing:
            reason = (
                "Hugging Face could not find the model on the selected route. Check "
                "the model's Inference Providers mapping; if it lists a provider, "
                "specify it with a model suffix such as :featherless-ai. "
                f"Router response: {body[:500]}"
            )
        else:
            reason = f"{provider_label} could not find the requested model: {body[:500]}"
    elif (
        status == 400
        and huggingface_routing
        and "provider" in folded
        and "not valid" in folded
    ):
        reason = (
            "Hugging Face rejected the provider route in the model suffix. "
            "Use a provider identifier listed in the model's Inference Providers "
            f"mapping, or use :cheapest. Router response: {body[:500]}"
        )
    elif status == 429:
        retry = headers.get("retry-after")
        reason = "Rate limit reached." + (f" Retry-After: {retry}." if retry else "")
    elif status >= 500:
        reason = f"{provider_label} service error ({status})."
    else:
        reason = f"{provider_label} rejected the request ({status}): {body[:500]}"
    return reason


def _retry_delay(value: str | None) -> float:
    if not value:
        return 0.25
    try:
        return min(30.0, max(0.0, float(value)))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return min(30.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0.25
