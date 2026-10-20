from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from .protocol import AgentEvent, AgentLimits, Message, ProviderError, ProviderEvent
from .providers import Provider
from .skills import SkillCatalog
from .tools import ToolError, ToolRegistry, definitions


class Agent:
    def __init__(self, provider: Provider, tools: ToolRegistry,
                 limits: AgentLimits | None = None, system_prompt: str | None = None):
        self.provider = provider
        self.tools = tools
        self.limits = limits or AgentLimits()
        if self.limits.max_history_turns < 1:
            raise ValueError("max_history_turns must be at least 1")
        if self.limits.max_history_chars < 1:
            raise ValueError("max_history_chars must be at least 1")
        self.history: list[Message] = []
        if system_prompt:
            self.history.append(Message("system", system_prompt))
        self.request_count = 0
        self.tool_call_count = 0
        self._cancelled = False
        self.skills = SkillCatalog(self.tools.root)

    def cancel(self) -> None:
        self._cancelled = True

    async def run(self, task: str) -> AsyncIterator[AgentEvent]:
        self._cancelled = False
        started = time.monotonic()
        pruned_turns = self._prune_history(
            self.limits.max_history_turns - 1,
            self.limits.max_history_chars,
        )
        if pruned_turns:
            yield AgentEvent(
                "activity",
                f"Trimmed {pruned_turns} older conversation turn(s) to limit context.",
            )
        try:
            request_task = self.tools.attach_references(task)
        except ToolError as exc:
            yield AgentEvent("error", str(exc))
            return
        try:
            remaining = self.limits.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                selected_skills = await self.skills.select(task, self.provider)
        except TimeoutError:
            yield AgentEvent("error", "Agent execution time limit reached.")
            return
        except ValueError as exc:
            yield AgentEvent("error", str(exc))
            return
        if selected_skills.skills:
            skill_ids = ", ".join(
                f"{skill.identifier}@{skill.version}" for skill in selected_skills.skills
            )
            yield AgentEvent("activity", f"Selected skills ({selected_skills.source}): {skill_ids}")
        request_task = selected_skills.append_to_task(request_task)
        self.history.append(Message("user", request_task))
        response_chars = 0
        run_tool_calls = 0
        for _ in range(self.limits.max_iterations):
            if not self.provider.capabilities.streaming:
                yield AgentEvent("error", "The selected provider does not support streaming.")
                return
            if self._cancelled:
                yield AgentEvent("activity", "Cancelled.")
                return
            remaining = self.limits.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                yield AgentEvent("error", "Agent execution time limit reached.")
                return
            self.request_count += 1
            yield AgentEvent("activity", f"Requesting {self.provider.name}/{self.provider.model}")
            response_text = ""
            try:
                tool_definitions = (
                    definitions() if self.provider.capabilities.tool_calling else []
                )
                events = self.provider.stream(self.history, tool_definitions)
                final: ProviderEvent | None = None
                async with asyncio.timeout(remaining):
                    async for event in events:
                        if self._cancelled:
                            yield AgentEvent("activity", "Cancelled.")
                            return
                        if event.kind == "text":
                            response_chars += len(event.text)
                            if response_chars > self.limits.max_response_chars:
                                yield AgentEvent("error", "Model response size limit reached.")
                                return
                            response_text += event.text
                            yield AgentEvent("text", event.text)
                        else:
                            final = event
                if final is None:
                    raise ProviderError("Provider stream ended without a final response.")
            except TimeoutError:
                yield AgentEvent("error", "Agent execution time limit reached.")
                return
            except ProviderError as exc:
                yield AgentEvent("error", str(exc))
                return
            self.history.append(
                Message("assistant", response_text, tool_calls=final.tool_calls)
            )
            if final.usage is not None:
                yield AgentEvent("usage", usage=final.usage)
            if final.finish_reason == "length":
                yield AgentEvent(
                    "error",
                    f"Model stopped at its output limit (request cap: "
                    f"{self.limits.max_output_tokens} tokens); the response may be "
                    "incomplete. Increase --max-output-tokens or "
                    "[provider].max_output_tokens if the model supports a larger "
                    "output, or ask for a shorter response.",
                )
                return
            if not final.tool_calls:
                yield AgentEvent("done")
                return
            if len(final.tool_calls) > self.limits.max_tool_calls_per_response:
                yield AgentEvent("error", "Provider returned too many tool calls in one response.")
                return
            if final.finish_reason not in ("tool_calls", None):
                yield AgentEvent("error", "Provider returned tool calls with an incompatible finish reason.")
                return
            call_ids = [call.id for call in final.tool_calls]
            if any(not call_id for call_id in call_ids) or len(set(call_ids)) != len(call_ids):
                yield AgentEvent("error", "Provider returned missing or duplicate tool-call IDs.")
                return
            for call in final.tool_calls:
                if run_tool_calls >= self.limits.max_tool_calls:
                    yield AgentEvent("error", "Agent tool-call limit reached.")
                    return
                run_tool_calls += 1
                self.tool_call_count += 1
                yield AgentEvent("activity", f"Running {call.name}")
                try:
                    remaining = self.limits.timeout_seconds - (
                        time.monotonic() - started
                    )
                    if remaining <= 0:
                        raise TimeoutError
                    async with asyncio.timeout(remaining):
                        result = await self.tools.invoke(call.name, call.arguments)
                except TimeoutError:
                    yield AgentEvent("error", "Agent execution time limit reached.")
                    return
                except (ToolError, OSError) as exc:
                    content = f"Tool error: {exc}"
                else:
                    content = result.content[: self.limits.max_tool_output_chars]
                    if not result.approved:
                        self.history.append(
                            Message("tool", content, tool_call_id=call.id, name=call.name)
                        )
                        yield AgentEvent(
                            "error",
                            f"Approval required for {call.name}; operation denied and agent stopped.",
                        )
                        yield AgentEvent("done")
                        return
                self.history.append(
                    Message("tool", content, tool_call_id=call.id, name=call.name)
                )
        request_word = "request" if self.limits.max_iterations == 1 else "requests"
        yield AgentEvent(
            "error",
            f"Agent iteration limit reached after {self.limits.max_iterations} "
            f"provider {request_word}. Increase [agent].max_iterations or "
            "--max-iterations if this task needs more tool cycles.",
        )

    def _prune_history(self, keep_turns: int, max_chars: int) -> int:
        system_messages = [message for message in self.history if message.role == "system"]
        turns: list[list[Message]] = []
        current: list[Message] = []
        for message in self.history:
            if message.role == "system":
                continue
            if message.role == "user":
                if current:
                    turns.append(current)
                current = [message]
            elif current:
                current.append(message)
        if current:
            turns.append(current)
        candidates = turns[-keep_turns:] if keep_turns else []
        retained_reversed: list[list[Message]] = []
        retained_chars = 0
        for turn in reversed(candidates):
            turn_chars = sum(
                len(json.dumps(message.as_api_message(), ensure_ascii=False))
                for message in turn
            )
            if retained_chars + turn_chars > max_chars:
                break
            retained_reversed.append(turn)
            retained_chars += turn_chars
        retained = list(reversed(retained_reversed))
        removed = len(turns) - len(retained)
        if removed:
            self.history = system_messages + [
                message for turn in retained for message in turn
            ]
        return removed
