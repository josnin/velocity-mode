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
from vmode.tui import _Response, _prompt_view_text


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
    snapshots = [event.request for event in events if event.kind == "request"]
    assert [snapshot.identifier for snapshot in snapshots if snapshot is not None] == [1, 2]
    assert snapshots[0] is not None
    assert "sample.py" in snapshots[0].payload
    assert snapshots[1] is not None
    assert '"role":"tool"' in snapshots[1].payload

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
async def test_agent_emits_command_start_and_bounded_completion_events(tmp_path: Path):
    class CommandProvider:
        name = "command"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=True)

        def __init__(self):
            self.requests = 0

        async def stream(self, messages, tools):
            self.requests += 1
            if self.requests == 1:
                yield ProviderEvent(
                    "response",
                    tool_calls=[
                        ToolCall(
                            "command-1",
                            "run_command",
                            {"argv": ["printf", "command output"]},
                        )
                    ],
                    finish_reason="tool_calls",
                )
            else:
                yield ProviderEvent("text", text="done")
                yield ProviderEvent("response", finish_reason="stop")

    async def approve(name, arguments):
        return name == "run_command"

    events = [
        event
        async for event in Agent(
            CommandProvider(), ToolRegistry(tmp_path, approve=approve)
        ).run("Run a command")
    ]

    command_events = [event for event in events if event.kind == "tool"]
    assert len(command_events) == 2
    assert command_events[0].tool_name == "run_command"
    assert command_events[0].tool_result is None
    assert command_events[1].tool_call_id == "command-1"
    assert command_events[1].tool_result == "exit code: 0\ncommand output"


@pytest.mark.asyncio
async def test_inspecting_captured_request_does_not_call_provider_or_mutate_history(
    tmp_path: Path,
):
    class CountingProvider:
        name = "counting"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=False)

        def __init__(self):
            self.calls = 0

        async def stream(self, messages, tools):
            self.calls += 1
            yield ProviderEvent(
                "request",
                request_payload='{"model":"test","messages":[{"role":"user"}]}',
            )
            yield ProviderEvent("text", text="completed")
            yield ProviderEvent("response", finish_reason="stop")

    provider = CountingProvider()
    agent = Agent(provider, ToolRegistry(tmp_path))
    events = [event async for event in agent.run("inspect")]
    snapshots = [event.request for event in events if event.kind == "request"]
    history_before = list(agent.history)

    rendered = _prompt_view_text(_Response(1, "completed", [snapshots[0]]))

    assert "PROMPT SENT" in rendered
    assert provider.calls == 1
    assert agent.history == history_before


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
    assert "--max-iterations" in events[-1].text
    assert "1 provider request." in events[-1].text


@pytest.mark.asyncio
async def test_agent_explains_output_limit_and_preserves_partial_response(tmp_path: Path):
    class TruncatedProvider:
        name = "truncated"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=False)

        async def stream(self, messages, tools):
            yield ProviderEvent("text", text="Partial code review")
            yield ProviderEvent("response", finish_reason="length")

    agent = Agent(
        TruncatedProvider(),
        ToolRegistry(tmp_path),
        AgentLimits(max_output_tokens=1234),
    )
    events = [event async for event in agent.run("Review this repository")]

    assert [event.text for event in events if event.kind == "text"] == [
        "Partial code review"
    ]
    assert events[-1].kind == "error"
    assert "1234 tokens" in events[-1].text
    assert "--max-output-tokens" in events[-1].text
    assert "may be incomplete" in events[-1].text


@pytest.mark.asyncio
async def test_agent_rejects_empty_final_response(tmp_path: Path):
    class EmptyProvider:
        name = "empty"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=False)

        async def stream(self, messages, tools):
            yield ProviderEvent("response", finish_reason="stop")

    agent = Agent(EmptyProvider(), ToolRegistry(tmp_path))
    events = [event async for event in agent.run("Reply with OK")]

    assert events[-1].kind == "error"
    assert "without text or tool calls" in events[-1].text


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
            yield ProviderEvent("text", text="current task completed")
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
        tool_calls=[
            ToolCall(
                "call-1",
                "read_file",
                {"path": "a.py"},
                {"thought_signature": "top-level-signature"},
                {"thought_signature": "function-signature"},
            )
        ],
    )
    tool = Message("tool", "contents", tool_call_id="call-1", name="read_file")
    assert assistant.as_api_message()["tool_calls"][0]["function"] == {
        "thought_signature": "function-signature",
        "name": "read_file",
        "arguments": '{"path": "a.py"}',
    }
    assert assistant.as_api_message()["tool_calls"][0]["thought_signature"] == (
        "top-level-signature"
    )
    assert tool.as_api_message() == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "read_file",
        "content": "contents",
    }
