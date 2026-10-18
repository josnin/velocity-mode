import stat
from pathlib import Path

import pytest

from vmode.protocol import Message, ToolCall
from vmode.sessions import SessionStore


def test_session_round_trip_preserves_tool_messages_and_private_permissions(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, tmp_path / "state")
    session_id = store.new_id()
    messages = [
        Message("system", "system prompt"),
        Message(
            "assistant",
            "",
            [ToolCall("call-1", "read_file", {"path": "main.py"})],
        ),
        Message("tool", "source", tool_call_id="call-1", name="read_file"),
    ]
    store.save(session_id, "mock", "mock/deterministic", messages, {"items": []})

    loaded = store.load(session_id)
    assert [message.role for message in loaded["messages"]] == [
        "system",
        "assistant",
        "tool",
    ]
    assert loaded["messages"][1].tool_calls[0].arguments == {"path": "main.py"}
    assert loaded["messages"][2].tool_call_id == "call-1"
    assert loaded["ui"] == {"items": []}
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.directory / f"{session_id}.json").stat().st_mode) == 0o600


def test_sessions_are_scoped_to_workspace_and_latest_is_reported(tmp_path: Path):
    state = tmp_path / "state"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = SessionStore(first, state)
    session_id = store.new_id()
    store.save(session_id, "mock", "mock/deterministic", [Message("user", "hello")], {})

    assert store.latest_id() == session_id
    assert store.list()[0].title == "hello"
    other_workspace_store = SessionStore(second, state)
    assert not other_workspace_store.list()
    with pytest.raises(ValueError, match="Cannot read session"):
        other_workspace_store.load(session_id)


def test_session_loader_rejects_invalid_ids_and_malformed_records(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, tmp_path / "state")
    with pytest.raises(ValueError, match="Invalid session ID"):
        store.load("../session")

    session_id = store.new_id()
    store.directory.mkdir(parents=True)
    (store.directory / f"{session_id}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported session format"):
        store.load(session_id)
