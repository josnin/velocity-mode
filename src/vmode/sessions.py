from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .protocol import Message, ToolCall

MAX_SESSION_BYTES = 10_000_000
MAX_SESSIONS = 25
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")


def validate_session_base_url(base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
        parsed.port
    except ValueError as exc:
        raise ValueError("Session base URL is invalid.") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Session base URL must not contain credentials, a query, or a fragment."
        )


@dataclass(frozen=True, slots=True)
class SessionInfo:
    id: str
    updated_at: str
    provider: str
    model: str
    title: str


def _serialize_message(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "extra": call.extra,
                "function_extra": call.function_extra,
            }
            for call in message.tool_calls
        ],
    }


def _deserialize_message(value: Any) -> Message:
    if not isinstance(value, dict):
        raise ValueError("Session message must be an object.")
    role = value.get("role")
    content = value.get("content")
    if role not in {"system", "user", "assistant", "tool"} or not isinstance(content, str):
        raise ValueError("Session contains an invalid message.")
    raw_calls = value.get("tool_calls", [])
    if not isinstance(raw_calls, list) or len(raw_calls) > 8:
        raise ValueError("Session contains invalid tool calls.")
    calls = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            raise ValueError("Session contains an invalid tool call.")
        call_id, name, arguments = raw.get("id"), raw.get("name"), raw.get("arguments")
        extra = raw.get("extra", {})
        function_extra = raw.get("function_extra", {})
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments, dict)
            or not isinstance(extra, dict)
            or not isinstance(function_extra, dict)
        ):
            raise ValueError("Session contains an invalid tool call.")
        calls.append(ToolCall(call_id, name, arguments, extra, function_extra))
    tool_call_id = value.get("tool_call_id")
    name = value.get("name")
    if tool_call_id is not None and not isinstance(tool_call_id, str):
        raise ValueError("Session contains an invalid tool result.")
    if name is not None and not isinstance(name, str):
        raise ValueError("Session contains an invalid tool name.")
    return Message(role, content, calls, tool_call_id, name)


class SessionStore:
    def __init__(self, workspace: Path, state_home: Path | None = None):
        self.workspace = workspace.resolve()
        base = state_home or Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
        workspace_key = hashlib.sha256(
            str(self.workspace).encode("utf-8")
        ).hexdigest()[:24]
        self.directory = base / "vmode" / "sessions" / workspace_key

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def _path(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid session ID.")
        return self.directory / f"{session_id}.json"

    def list(self) -> list[SessionInfo]:
        if not self.directory.exists():
            return []
        result = []
        for path in self.directory.glob("*.json"):
            try:
                record = self.load(path.stem)
                result.append(
                    SessionInfo(
                        path.stem,
                        record["updated_at"],
                        record["provider"],
                        record["model"],
                        record.get("title", ""),
                    )
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return sorted(result, key=lambda item: item.updated_at, reverse=True)

    def latest_id(self) -> str | None:
        sessions = self.list()
        return sessions[0].id if sessions else None

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        try:
            path.unlink()
        except OSError as exc:
            raise ValueError(f"Cannot delete session {session_id}: {exc}") from exc

    def load(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Cannot read session {session_id}: {exc}") from exc
        if len(raw) > MAX_SESSION_BYTES:
            raise ValueError("Session file exceeds the size limit.")
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Session {session_id} is not valid JSON.") from exc
        if not isinstance(record, dict) or record.get("schema") != 1:
            raise ValueError("Unsupported session format.")
        if (
            record.get("id") != session_id
            or not isinstance(record.get("updated_at"), str)
            or not isinstance(record.get("provider"), str)
            or not isinstance(record.get("model"), str)
            or not isinstance(record.get("title"), str)
        ):
            raise ValueError("Session metadata is invalid.")
        if "base_url" in record:
            if not isinstance(record["base_url"], str):
                raise ValueError("Session base URL is invalid.")
            validate_session_base_url(record["base_url"])
        if "tool_calling" in record and type(record["tool_calling"]) is not bool:
            raise ValueError("Session tool-calling setting is invalid.")
        if record.get("workspace") != str(self.workspace):
            raise ValueError("Session belongs to a different workspace.")
        messages = record.get("messages")
        if not isinstance(messages, list) or len(messages) > 1000:
            raise ValueError("Session has invalid conversation history.")
        record["messages"] = [_deserialize_message(message) for message in messages]
        ui = record.get("ui", {})
        if not isinstance(ui, dict):
            raise ValueError("Session UI state is invalid.")
        return record

    def save(
        self,
        session_id: str,
        provider: str,
        model: str,
        messages: list[Message],
        ui: dict[str, Any],
        tool_calling: bool | None = None,
        base_url: str | None = None,
    ) -> None:
        if base_url:
            validate_session_base_url(base_url)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        title = next(
            (message.content.splitlines()[0][:160] for message in messages if message.role == "user"),
            "",
        )
        record = {
            "schema": 1,
            "id": session_id,
            "workspace": str(self.workspace),
            "provider": provider,
            "model": model,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "title": title,
            "messages": [_serialize_message(message) for message in messages],
            "ui": ui,
        }
        if tool_calling is not None:
            record["tool_calling"] = tool_calling
        if base_url:
            record["base_url"] = base_url
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_SESSION_BYTES:
            raise ValueError("Session exceeds the storage size limit.")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".session-", suffix=".tmp", dir=self.directory
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self._path(session_id))
            files = sorted(
                self.directory.glob("*.json"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
            for old_session in files[MAX_SESSIONS:]:
                old_session.unlink()
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
