import asyncio
from pathlib import Path

import pytest

from vmode.agent import Agent
from vmode.protocol import (
    AgentLimits,
    Message,
    ProviderCapabilities,
    ProviderEvent,
    ToolCall,
)
from vmode.providers import MockProvider
from vmode.tools import ToolRegistry


@pytest.mark.asyncio
async def test_complete_mock_tool_cycle_streams_and_returns_file_result(tmp_path: Path):
    source = tmp_path / "sample.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    provider = MockProvider(scenario="read-file")
    agent = Agent(provider, ToolRegistry(tmp_path))
    events = [event async for event in agent.run("Inspect @sample.py")]

    assert [event.text for event in events if event.kind == "text"] == [
        "I’ll inspect ",
        "the requested file.",
        "The file contains ",
        "the requested source.",
    ]
    assert events[-1].kind == "done"
    assert agent.request_count == 2
    assert agent.tool_call_count == 1
    assert len(provider.requests) == 2

    second_request = provider.requests[1]
    assert [message.role for message in second_request] == [
        "user",
        "assistant",
        "tool",
    ]
    assert second_request[1].tool_calls[0].name == "read_file"
    assert second_request[2].tool_call_id == "mock-read-1"
    assert "return 42" in second_request[2].content
    assert "[Referenced file: sample.py]" in second_request[0].content


@pytest.mark.asyncio
async def test_agent_stops_at_iteration_limit(tmp_path: Path):
    agent = Agent(
        MockProvider(scenario="read-file"),
        ToolRegistry(tmp_path),
        AgentLimits(max_iterations=1),
    )
    events = [event async for event in agent.run("Inspect the project")]
    assert events[-1].kind == "error"
    assert "iteration limit" in events[-1].text


@pytest.mark.asyncio
async def test_agent_enforces_tool_call_limit(tmp_path: Path):
    agent = Agent(
        MockProvider(scenario="read-file"),
        ToolRegistry(tmp_path),
        AgentLimits(max_tool_calls=0),
    )
    events = [event async for event in agent.run("inspect a file")]
    assert events[-1].kind == "error"
    assert "tool-call limit" in events[-1].text
    assert agent.tool_call_count == 0


@pytest.mark.asyncio
async def test_agent_enforces_wall_clock_timeout(tmp_path: Path):
    class SlowProvider:
        name = "slow"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=False)

        async def stream(self, messages, tools):
            await asyncio.sleep(1)
            yield ProviderEvent("response", finish_reason="stop")

    agent = Agent(
        SlowProvider(),
        ToolRegistry(tmp_path),
        AgentLimits(timeout_seconds=0.02),
    )
    events = [event async for event in agent.run("wait")]
    assert events[-1].kind == "error"
    assert "time limit" in events[-1].text


@pytest.mark.asyncio
async def test_agent_stream_cancellation_propagates_to_provider(tmp_path: Path):
    cancelled = asyncio.Event()

    class SlowProvider:
        name = "slow"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=False)

        async def stream(self, messages, tools):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
            yield ProviderEvent("response", finish_reason="stop")

    agent = Agent(SlowProvider(), ToolRegistry(tmp_path))

    async def consume():
        return [event async for event in agent.run("cancel this")]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_agent_bounds_prior_history_at_task_boundaries(tmp_path: Path):
    class RecordingProvider:
        name = "recording"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=False)

        def __init__(self):
            self.requests = []

        async def stream(self, messages, tools):
            self.requests.append(list(messages))
            yield ProviderEvent("response", finish_reason="stop")

    provider = RecordingProvider()
    agent = Agent(
        provider,
        ToolRegistry(tmp_path),
        AgentLimits(max_history_turns=2),
        system_prompt="stable system prefix",
    )
    for task in ("task one", "task two", "task three"):
        events = [event async for event in agent.run(task)]

    assert [message.content for message in provider.requests[-1] if message.role == "user"] == [
        "task two",
        "task three",
    ]
    assert provider.requests[-1][0].role == "system"
    assert provider.requests[-1][0].content == "stable system prefix"
    assert any(
        event.kind == "activity" and "Trimmed 1 older" in event.text
        for event in events
    )


@pytest.mark.asyncio
async def test_history_budget_keeps_tool_call_and_result_messages_together(tmp_path: Path):
    class RecordingProvider:
        name = "recording"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=True)

        def __init__(self):
            self.request = None

        async def stream(self, messages, tools):
            self.request = list(messages)
            yield ProviderEvent("response", finish_reason="stop")

    provider = RecordingProvider()
    agent = Agent(
        provider,
        ToolRegistry(tmp_path),
        AgentLimits(max_history_turns=3, max_history_chars=10_000),
        system_prompt="stable prefix",
    )
    call = ToolCall("read-1", "read_file", {"path": "notes.txt"})
    agent.history.extend(
        [
            Message("user", "earlier task"),
            Message("assistant", tool_calls=[call]),
            Message("tool", "file contents", tool_call_id="read-1", name="read_file"),
            Message("assistant", "earlier task completed"),
            Message("user", "recent task"),
            Message("assistant", "recent task completed"),
        ]
    )

    events = [event async for event in agent.run("current task")]
    assert events[-1].kind == "done"
    assert provider.request is not None
    older_group = provider.request[1:5]
    assert [message.role for message in older_group] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert older_group[1].tool_calls[0].id == older_group[2].tool_call_id
    assert provider.request[-1].role == "user"
    assert provider.request[-1].content == "current task"


@pytest.mark.asyncio
async def test_assistant_tool_messages_serialize_openai_tool_result_shape():
    assistant = Message(
        "assistant",
        "checking",
        tool_calls=[ToolCall("call-1", "read_file", {"path": "a.py"})],
    )
    tool = Message("tool", "contents", tool_call_id="call-1", name="read_file")
    assert assistant.as_api_message()["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path": "a.py"}',
    }
    assert tool.as_api_message() == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "read_file",
        "content": "contents",
    }
