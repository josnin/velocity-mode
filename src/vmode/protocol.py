from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def as_api_message(self) -> dict[str, Any]:
        if self.role == "assistant" and self.tool_calls:
            return {
                "role": "assistant",
                "content": self.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in self.tool_calls
                ],
            }
        if self.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": self.tool_call_id,
                "name": self.name,
                "content": self.content,
            }
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    streaming: bool
    tool_calling: bool


@dataclass(slots=True)
class ProviderEvent:
    kind: Literal["text", "response"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None


@dataclass(slots=True)
class AgentEvent:
    kind: Literal["text", "activity", "usage", "error", "done"]
    text: str = ""
    usage: Usage | None = None


@dataclass(slots=True)
class AgentLimits:
    max_iterations: int = 24
    max_tool_calls: int = 32
    max_tool_calls_per_response: int = 8
    timeout_seconds: float = 300.0
    max_history_turns: int = 4
    max_history_chars: int = 60_000
    max_response_chars: int = 1_000_000
    max_tool_output_chars: int = 100_000


class ProviderError(RuntimeError):
    pass
