from __future__ import annotations

import asyncio
import base64
import difflib
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition, vi_insert_mode, vi_navigation_mode
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.selection import SelectionType
from prompt_toolkit.search import SearchDirection, SearchState
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import SearchToolbar, TextArea

from . import __version__
from .agent import Agent
from .config import Settings
from .instructions import build_system_prompt
from .protocol import AgentLimits, Message, Usage
from .providers import Provider
from .sessions import SessionStore
from .tools import ToolRegistry

MAX_TRANSCRIPT_CHARS = 250_000
MAX_RESPONSES = 100
SELECTED_RESPONSE_PREFIX = "target: "

TUI_STYLE_RULES = {
    "header": "",
    "brand": "bold",
    "dim": "dim",
    "rule": "dim",
    "transcript": "",
    "transcript-user": "bold",
    "transcript-assistant": "",
    "transcript-assistant-label": "bold",
    "transcript-notice": "bold ansiyellow",
    "transcript-error": "bold ansired",
    "transcript-code": "bold",
    "editor": "",
    "command": "",
    "activity": "dim",
    "approval": "bold ansiyellow",
    "toolbar": "dim",
    "mode": "bold ansibrightblue",
    "transcript-selected": "bold underline ansibrightblue",
    "transcript-selected-error": "bold underline ansired",
    "selected-target": "bold ansibrightblue",
}


@dataclass
class _TranscriptItem:
    role: str
    text: str = ""
    response_id: int | None = None
    incomplete: bool = False
    kind: str = ""


@dataclass
class _Response:
    identifier: int
    text: str


@dataclass
class _UIState:
    items: list[_TranscriptItem] = field(default_factory=list)
    responses: list[_Response] = field(default_factory=list)
    yanked_responses: dict[int, str] = field(default_factory=dict)
    next_response_id: int = 0
    activities: list[str] = field(default_factory=list)
    selected_response: int = -1
    yank_entries: list[str] = field(default_factory=list)
    latest_usage: Usage | None = None
    busy: bool = False
    pending_approval: str = ""
    approval_future: asyncio.Future | None = None
    command_active: bool = False
    command_prefix: str = ":"
    search_active: bool = False
    search_query: str = ""


def _register_response(state: _UIState, item: _TranscriptItem) -> None:
    if not item.text or item.response_id is not None:
        return
    item.response_id = state.next_response_id
    state.next_response_id += 1
    state.responses.append(_Response(item.response_id, item.text))
    del state.responses[:-MAX_RESPONSES]
    state.selected_response = len(state.responses) - 1


class _TranscriptLexer(Lexer):
    def lex_document(self, document: Document):
        lines = document.lines
        styles: list[str] = []
        section_style = ""
        in_code_block = False

        for line in lines:
            stripped = line.lstrip()
            if line.startswith((SELECTED_RESPONSE_PREFIX, "You", "Velocity-Mode", "! ")):
                in_code_block = False
            if line.startswith(SELECTED_RESPONSE_PREFIX):
                section_style = (
                    "class:transcript-error"
                    if "· error" in line
                    else "class:transcript-assistant"
                )
                style = (
                    "class:transcript-selected-error"
                    if section_style == "class:transcript-error"
                    else "class:transcript-selected"
                )
            elif line.startswith("You"):
                section_style = "class:transcript-user"
                style = section_style
            elif line.startswith("Velocity-Mode"):
                section_style = (
                    "class:transcript-error"
                    if "· error" in line
                    else "class:transcript-assistant"
                )
                style = (
                    section_style
                    if "· error" in line
                    else "class:transcript-assistant-label"
                )
            elif line.startswith("! Error:") or line.startswith("! Agent failed:"):
                section_style = "class:transcript-error"
                style = section_style
            elif line.startswith("! "):
                section_style = "class:transcript-notice"
                style = section_style
            elif stripped.startswith("```"):
                style = "class:transcript-code"
                in_code_block = not in_code_block
            elif in_code_block:
                style = "class:transcript-code"
            else:
                style = section_style
            styles.append(style)

        def get_line(index: int) -> list[tuple[str, str]]:
            return [(styles[index], lines[index])]

        return get_line


def _approval_preview(name: str, arguments: dict[str, Any], workspace: Path) -> str:
    if name == "run_command":
        return "Command argv: " + repr(arguments.get("argv", []))
    if name == "git_stage":
        preview = "Approve Git staging for:\n" + "\n".join(
            str(path) for path in arguments.get("paths", [])
        )
        baseline_paths = arguments.get("baseline_paths", [])
        if baseline_paths:
            preview += (
                "\n\nWarning: these files had changes before this task. "
                "Staging them includes all current hunks:\n"
                + "\n".join(str(path) for path in baseline_paths)
            )
        return preview
    if name == "git_commit":
        return (
            "Approve Git commit for:\n"
            + "\n".join(str(path) for path in arguments.get("paths", []))
            + "\n\n"
            + str(arguments.get("message", ""))
        )
    path = str(arguments.get("path", ""))
    old: list[str] = []
    preview_note = ""
    try:
        root = workspace.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            preview_note = "(outside-workspace path; existing content not read)"
        elif candidate.is_file():
            with candidate.open(encoding="utf-8") as stream:
                old = stream.read(100_001).splitlines(keepends=True)
    except (OSError, UnicodeError, ValueError, RuntimeError):
        preview_note = "(existing content unavailable; diff may be incomplete)"
    if name == "edit_file":
        old_text, new_text = arguments.get("old_text"), arguments.get("new_text")
        original = "".join(old)
        if (
            isinstance(old_text, str)
            and isinstance(new_text, str)
            and original.count(old_text) == 1
        ):
            content = original.replace(old_text, new_text, 1)
        else:
            content = original
            preview_note = "(exact edit target is unavailable; diff may be incomplete)"
    else:
        content = arguments.get("content", "")
        if not isinstance(content, str):
            content = ""
    diff = "".join(
        difflib.unified_diff(
            old,
            content.splitlines(keepends=True),
            fromfile=f"a/{path}" if old else "/dev/null",
            tofile=f"b/{path}",
        )
    )
    if len(diff) > 8_000:
        diff = diff[:8_000] + "\n[approval diff truncated]\n"
    operation = "edit" if name == "edit_file" else "write"
    heading = f"Approve {operation} to {path}?\n"
    if preview_note:
        heading += preview_note + "\n"
    return heading + (diff or "(no content change)")


async def run_tui(
    settings: Settings,
    provider: Provider,
    initial: str,
    completer_factory: Callable[[Path, bool], Any],
    sessions: SessionStore | None = None,
    session_id: str | None = None,
    session_record: dict[str, Any] | None = None,
) -> int:
    state = _UIState()
    active_task: asyncio.Task | None = None
    agent: Agent | None = None
    session_id = session_id or (sessions.new_id() if sessions else None)
    persist_handle: asyncio.TimerHandle | None = None
    if session_record:
        raw_items = session_record.get("ui", {}).get("items", [])
        if isinstance(raw_items, list):
            for item in raw_items[-2000:]:
                if (
                    isinstance(item, dict)
                    and item.get("role") in {"user", "assistant", "notice"}
                    and isinstance(item.get("text"), str)
                ):
                    response_id = item.get("response_id")
                    if response_id is not None and (
                        not isinstance(response_id, int) or response_id < 0
                    ):
                        response_id = None
                    incomplete = item.get("incomplete", False)
                    if type(incomplete) is not bool:
                        incomplete = False
                    kind = item.get("kind", "")
                    if kind not in {"", "error"}:
                        kind = ""
                    state.items.append(
                        _TranscriptItem(
                            item["role"], item["text"], response_id, incomplete, kind
                        )
                    )
        raw_responses = session_record.get("ui", {}).get("responses", [])
        if isinstance(raw_responses, list):
            for response in raw_responses[-MAX_RESPONSES:]:
                if (
                    isinstance(response, dict)
                    and isinstance(response.get("id"), int)
                    and isinstance(response.get("text"), str)
                ):
                    state.responses.append(
                        _Response(response["id"], response["text"])
                    )
        state.next_response_id = max(
            (response.identifier for response in state.responses), default=-1
        ) + 1
        selected = session_record.get("ui", {}).get("selected_response", -1)
        if isinstance(selected, int) and -1 <= selected < len(state.responses):
            state.selected_response = selected
        raw_yanks = session_record.get("ui", {}).get("yank_entries", [])
        if isinstance(raw_yanks, list):
            state.yank_entries = [
                entry for entry in raw_yanks[-100:] if isinstance(entry, str)
            ]
        state.search_query = session_record.get("ui", {}).get("search_query", "")
        if not isinstance(state.search_query, str):
            state.search_query = ""

    def transcript_text() -> str:
        chunks: list[str] = []
        selected_response = (
            state.responses[state.selected_response]
            if 0 <= state.selected_response < len(state.responses)
            else None
        )
        response_positions = {
            response.identifier: index + 1
            for index, response in enumerate(state.responses)
        }
        for item in state.items:
            if item.role == "user":
                chunks.append("\nYou\n")
                chunks.extend(f"  {line}\n" for line in (item.text.splitlines() or [""]))
            elif item.role == "assistant":
                response_position = (
                    response_positions.get(item.response_id)
                    if item.response_id is not None
                    else None
                )
                selected_label = (
                    response_position is not None
                    and selected_response is not None
                    and item.response_id == selected_response.identifier
                )
                label = (
                    "Velocity-Mode · error"
                    if item.kind == "error"
                    else (
                        "Velocity-Mode · partial"
                        if item.incomplete
                        else (
                            "Velocity-Mode · working"
                            if item.response_id is None and state.busy
                            else "Velocity-Mode"
                        )
                    )
                )
                if selected_label:
                    label = (
                        f"{SELECTED_RESPONSE_PREFIX}{response_position}/"
                        f"{len(state.responses)} · {label}"
                    )
                chunks.append(f"\n{label}\n")
                chunks.extend(f"  {line}\n" for line in (item.text.splitlines() or [""]))
            elif item.role == "notice":
                chunks.append(f"\n! {item.text}\n")
        return "".join(chunks)[-MAX_TRANSCRIPT_CHARS:]

    def redraw_transcript(transcript: TextArea, app: Application) -> None:
        transcript.buffer.set_document(
            Document(transcript_text()), bypass_readonly=True
        )
        if app.layout.has_focus(transcript):
            transcript.buffer.cursor_position = len(transcript.buffer.text)
        else:
            app.invalidate()

    def refresh_selection_marker(app: Application) -> None:
        text = transcript_text()
        selected_marker = (
            f"{SELECTED_RESPONSE_PREFIX}{state.selected_response + 1}/"
            f"{len(state.responses)} ·"
            if 0 <= state.selected_response < len(state.responses)
            else ""
        )
        cursor = (
            text.find(selected_marker)
            if selected_marker
            else transcript.buffer.cursor_position
        )
        if cursor < 0:
            cursor = transcript.buffer.cursor_position
        transcript.buffer.set_document(
            Document(text, cursor_position=min(cursor, len(text))),
            bypass_readonly=True,
        )
        app.invalidate()

    def toolbar_text() -> list[tuple[str, str]]:
        app = get_app()
        if state.command_active:
            mode = "COMMAND"
        else:
            mode = "NORMAL" if app.vi_state.input_mode.value == "vi-navigation" else "INSERT"
        if state.pending_approval:
            hint = "Approval pending · y allow · n deny · Ctrl+C cancel"
        elif app.layout.has_focus(transcript):
            if state.busy:
                hint = "Task running · :status · :usage · :cancel · Ctrl+C cancel · Ctrl+D half-page down · Tab prompt"
            else:
                hint = "Ctrl+D half-page down · / search · n/N repeat · [ older · ] newer result (save/copy) · v/V select · y copy · Tab prompt"
        elif state.busy:
            hint = "Task running · draft next prompt · Esc :status/:usage/:cancel · Ctrl+C cancel · Tab results"
        else:
            hint = "Enter newline · Ctrl+S send · Ctrl+D quit · Esc Normal · / commands · @ files"
        result = [("class:mode", f" {mode} ")]
        if 0 <= state.selected_response < len(state.responses):
            result.append(
                (
                    "class:selected-target",
                    f" target {state.selected_response + 1}/{len(state.responses)} ",
                )
            )
        result.append(("class:toolbar", f" · {hint}"))
        return result

    transcript = TextArea(
        text=transcript_text(),
        lexer=_TranscriptLexer(),
        read_only=Condition(lambda: True),
        focusable=True,
        wrap_lines=True,
        scrollbar=True,
        height=Dimension(weight=1),
        style="class:transcript",
        name="transcript",
    )
    completer = completer_factory(settings.workspace, False)
    command_completer = completer_factory(settings.workspace, True)
    editor = TextArea(
        text=(
            initial
            or (
                session_record.get("ui", {}).get("draft", "")
                if session_record
                else ""
            )
        ),
        multiline=True,
        completer=completer,
        complete_while_typing=True,
        wrap_lines=True,
        scrollbar=False,
        height=Dimension(min=3, max=7, preferred=4),
        prompt="› ",
        style="class:editor",
        name="editor",
    )
    command_editor = TextArea(
        completer=command_completer,
        complete_while_typing=True,
        multiline=False,
        height=1,
        prompt="",
        style="class:command",
        name="command",
    )
    editor.buffer.on_text_changed += lambda _: schedule_persist()
    search_toolbar = SearchToolbar(vi_mode=True, ignore_case=True)
    bindings = KeyBindings()
    in_editor = Condition(lambda: get_app().layout.has_focus(editor))
    in_transcript = Condition(lambda: get_app().layout.has_focus(transcript))
    command_active = Condition(lambda: state.command_active)
    search_active = Condition(lambda: state.search_active)
    has_approval = Condition(lambda: state.approval_future is not None)

    def persist_session(app: Application | None = None) -> None:
        nonlocal persist_handle
        persist_handle = None
        if sessions is None or session_id is None or agent is None:
            return
        if not agent.history and not state.items and not editor.text:
            return
        ui = {
            "items": [
                {
                    "role": item.role,
                    "text": item.text,
                    "response_id": item.response_id,
                    "incomplete": item.incomplete,
                    "kind": item.kind,
                }
                for item in state.items
            ],
            "responses": [
                {"id": response.identifier, "text": response.text}
                for response in state.responses
            ],
            "selected_response": state.selected_response,
            "yank_entries": state.yank_entries[-100:],
            "search_query": state.search_query,
            "draft": editor.text,
        }
        messages = list(agent.history)
        latest_task = next(
            (item.text for item in reversed(state.items) if item.role == "user"),
            None,
        )
        if latest_task and not any(
            message.role == "user" and message.content.startswith(latest_task)
            for message in messages
        ):
            messages.append(Message("user", latest_task))
        try:
            sessions.save(
                session_id,
                provider.name,
                provider.model,
                messages,
                ui,
                tool_calling=provider.capabilities.tool_calling,
            )
        except (OSError, ValueError) as exc:
            state.activities.append(f"Session save failed: {exc}")
            state.activities = state.activities[-3:]
            if app is not None:
                app.invalidate()
            else:
                print(f"vmode: session save failed: {exc}", file=sys.stderr)

    def schedule_persist() -> None:
        nonlocal persist_handle
        if sessions is None:
            return
        if persist_handle is not None:
            persist_handle.cancel()
        app = get_app()
        persist_handle = app.loop.call_later(0.4, persist_session, app)

    def add_notice(text: str) -> None:
        state.items.append(_TranscriptItem("notice", text))
        redraw_transcript(transcript, get_app())
        persist_session(get_app())

    def focus_editor(app: Application) -> None:
        state.command_active = False
        app.layout.focus(editor)
        app.vi_state.input_mode = app.vi_state.input_mode.__class__.NAVIGATION
        app.invalidate()

    def set_approval(allow: bool) -> None:
        future = state.approval_future
        if future is not None and not future.done():
            future.set_result(allow)
        state.approval_future = None
        state.pending_approval = ""
        get_app().invalidate()

    def copy_selected(app: Application, selected_text: str | None = None) -> None:
        if not state.responses:
            add_notice("No response is available to copy.")
            return
        index = min(max(state.selected_response, 0), len(state.responses) - 1)
        response = state.responses[index]
        is_new_response = (
            selected_text is None
            and response.identifier not in state.yanked_responses
        )
        yank_text = selected_text if selected_text is not None else response.text
        add_to_register = selected_text is not None or is_new_response
        entries = [*state.yank_entries]
        if add_to_register:
            entries.append(yank_text)
        text = "\n\n".join(entries)
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        if len(payload) > 140_000:
            add_notice("Yank register exceeds the terminal clipboard size limit.")
            return
        if not sys.stdout.isatty():
            add_notice("Clipboard copy requires a terminal with OSC 52 support.")
            return
        if add_to_register:
            state.yank_entries.append(yank_text)
        if is_new_response:
            state.yanked_responses[response.identifier] = response.text
        app.output.write_raw(f"\x1b]52;c;{payload}\x07")
        app.output.flush()
        if selected_text is None:
            add_notice(f"Yanked response {index + 1}.")
        else:
            add_notice(f"Yanked visual selection ({len(selected_text)} characters).")

    def open_transcript(app: Application) -> None:
        if not state.items:
            add_notice("No responses yet.")
            return
        if state.responses:
            state.selected_response = len(state.responses) - 1
        refresh_selection_marker(app)
        app.layout.focus(transcript)
        app.vi_state.input_mode = app.vi_state.input_mode.__class__.NAVIGATION
        app.invalidate()

    def start_agent(prompt: str, app: Application) -> None:
        nonlocal active_task
        cleaned = prompt.strip()
        if not cleaned:
            add_notice("Prompt is empty; enter a task before submitting.")
            return
        if active_task is not None and not active_task.done():
            add_notice(
                "Task still running; your draft was kept. Check :status, browse "
                "results, or use :cancel, then submit the draft."
            )
            return
        state.items.append(_TranscriptItem("user", cleaned))
        assistant_item = _TranscriptItem("assistant")
        state.items.append(assistant_item)
        state.busy = True
        state.activities = ["Starting request"]
        editor.text = ""
        redraw_transcript(transcript, app)

        async def run() -> None:
            try:
                assert agent is not None
                async for event in agent.run(cleaned):
                    schedule_persist()
                    if event.kind == "text":
                        assistant_item.text += event.text
                        redraw_transcript(transcript, app)
                    elif event.kind == "activity":
                        state.activities.append(event.text)
                        state.activities = state.activities[-3:]
                        app.invalidate()
                    elif event.kind == "usage" and event.usage is not None:
                        state.latest_usage = event.usage
                        cached = (
                            f" · cached input {event.usage.cached_input_tokens}"
                            if event.usage.cached_input_tokens is not None
                            else ""
                        )
                        state.activities.append(
                            f"Usage: input {event.usage.input_tokens if event.usage.input_tokens is not None else '—'} · "
                            f"output {event.usage.output_tokens if event.usage.output_tokens is not None else '—'}"
                            f"{cached} tokens"
                        )
                        state.activities = state.activities[-3:]
                        app.invalidate()
                    elif event.kind == "error":
                        assistant_item.incomplete = True
                        if assistant_item.text:
                            state.items.append(
                                _TranscriptItem("notice", f"Error: {event.text}")
                            )
                        else:
                            assistant_item.text = f"Error: {event.text}"
                            assistant_item.kind = "error"
                        app.invalidate()
                    elif event.kind == "done":
                        state.activities.append("Turn complete")
                        _register_response(state, assistant_item)
            except asyncio.CancelledError:
                assistant_item.incomplete = bool(assistant_item.text)
                state.activities.append("Task cancelled")
                raise
            except Exception as exc:
                assistant_item.incomplete = bool(assistant_item.text)
                if assistant_item.text:
                    state.items.append(
                        _TranscriptItem("notice", f"Agent failed: {exc}")
                    )
                else:
                    assistant_item.text = f"Agent failed: {exc}"
                    assistant_item.incomplete = True
                    assistant_item.kind = "error"
            finally:
                if assistant_item.text and assistant_item.response_id is None:
                    assistant_item.incomplete = True
                _register_response(state, assistant_item)
                state.busy = False
                state.pending_approval = ""
                state.approval_future = None
                redraw_transcript(transcript, app)
                app.invalidate()
                persist_session(app)

        try:
            active_task = app.create_background_task(run())
        except Exception as exc:
            state.items.append(_TranscriptItem("notice", f"Cannot start agent task: {exc}"))
            redraw_transcript(transcript, app)

    async def approve(name: str, arguments: dict[str, Any]) -> bool:
        future = asyncio.get_running_loop().create_future()
        state.pending_approval = _approval_preview(name, arguments, settings.workspace)
        state.approval_future = future
        app = get_app()
        app.vi_state.input_mode = app.vi_state.input_mode.__class__.NAVIGATION
        app.invalidate()
        try:
            return await future
        finally:
            state.pending_approval = ""
            state.approval_future = None
            get_app().invalidate()

    registry = ToolRegistry(settings.workspace, approve=approve)
    agent = Agent(
        provider,
        registry,
        AgentLimits(
            max_iterations=settings.max_iterations,
            max_history_chars=settings.max_history_chars,
            max_output_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout,
        ),
        system_prompt=build_system_prompt(provider, settings.workspace),
    )
    if session_record:
        restored = session_record.get("messages", [])
        if isinstance(restored, list):
            agent.history = [
                message for message in agent.history if message.role == "system"
            ] + [message for message in restored if message.role != "system"]

    @bindings.add(
        "enter",
        filter=Condition(
            lambda: in_editor()
            and vi_insert_mode()
            and event_completion_active(editor)
        ),
        eager=True,
    )
    def accept_completion(event) -> None:
        completion = editor.buffer.complete_state
        if completion is not None:
            editor.buffer.apply_completion(completion.current_completion)

    def event_completion_active(area: TextArea) -> bool:
        return (
            area.buffer.complete_state is not None
            and area.buffer.complete_state.current_completion is not None
        )

    @bindings.add("enter", filter=Condition(lambda: state.command_active and event_completion_active(command_editor)), eager=True)
    def accept_command_completion(event) -> None:
        completion = command_editor.buffer.complete_state
        if completion is not None:
            command_editor.buffer.apply_completion(completion.current_completion)

    @bindings.add(
        "escape",
        filter=Condition(lambda: in_editor() and event_completion_active(editor)),
        eager=True,
    )
    def dismiss_completion(event) -> None:
        editor.buffer.cancel_completion()

    @bindings.add(
        "@",
        filter=Condition(lambda: in_editor() and vi_insert_mode()),
        eager=True,
    )
    def trigger_prompt_completion(event) -> None:
        event.current_buffer.insert_text(event.data)
        event.current_buffer.start_completion(select_first=False)

    @bindings.add(
        "enter",
        filter=Condition(
            lambda: state.command_active
            and not event_completion_active(command_editor)
        ),
        eager=True,
    )
    def run_command(event) -> None:
        command = command_editor.text.strip()
        editor.buffer.cancel_completion()
        command_editor.buffer.cancel_completion()
        command_editor.text = ""
        state.command_active = False
        event.app.layout.focus(editor)
        event.app.vi_state.input_mode = event.app.vi_state.input_mode.__class__.NAVIGATION
        handle_command(command, event.app)
        if not event.app.is_done and not event.app.layout.has_focus(transcript):
            focus_editor(event.app)

    def apply_response_search(
        app: Application,
        direction: SearchDirection,
        wrap: bool,
        include_current: bool = False,
    ) -> None:
        query = state.search_query
        if not query:
            add_notice("Enter text to search responses.")
            return
        if query.casefold() not in transcript.buffer.text.casefold():
            add_notice(f"No match for {query!r}.")
            return
        old_position = transcript.buffer.cursor_position
        search_state = SearchState(query, direction=direction, ignore_case=True)
        search_toolbar.control.searcher_search_state = search_state
        transcript.buffer.apply_search(
            search_state, include_current_position=include_current
        )
        if transcript.buffer.cursor_position == old_position and wrap:
            boundary = (
                0 if direction == SearchDirection.FORWARD else len(transcript.buffer.text)
            )
            transcript.buffer.cursor_position = boundary
            transcript.buffer.apply_search(
                search_state, include_current_position=False
            )
            if transcript.buffer.cursor_position == boundary:
                add_notice(f"No next match for {query!r}.")
        app.invalidate()

    def start_response_search(event) -> None:
        state.search_active = True
        search_toolbar.search_buffer.text = ""
        event.app.layout.search_links[search_toolbar.control] = transcript.control
        event.app.layout.focus(search_toolbar.control)
        event.app.vi_state.input_mode = event.app.vi_state.input_mode.__class__.INSERT
        event.app.invalidate()

    @bindings.add("enter", filter=search_active, eager=True)
    def finish_response_search(event) -> None:
        state.search_query = search_toolbar.search_buffer.text
        state.search_active = False
        event.app.layout.focus(transcript)
        event.app.vi_state.input_mode = event.app.vi_state.input_mode.__class__.NAVIGATION
        transcript.buffer.cursor_position = 0
        apply_response_search(
            event.app,
            SearchDirection.FORWARD,
            wrap=False,
            include_current=True,
        )
        persist_session(event.app)

    @bindings.add("escape", filter=search_active, eager=True)
    def cancel_response_search(event) -> None:
        state.search_active = False
        event.app.layout.search_links.pop(search_toolbar.control, None)
        event.app.layout.focus(transcript)
        event.app.vi_state.input_mode = event.app.vi_state.input_mode.__class__.NAVIGATION
        event.app.invalidate()

    @bindings.add(
        "c-s",
        filter=Condition(lambda: in_editor() and not has_approval()),
        eager=True,
    )
    def submit_prompt(event) -> None:
        start_agent(editor.text, event.app)

    @bindings.add("escape", filter=command_active, eager=True)
    def cancel_command(event) -> None:
        command_editor.buffer.cancel_completion()
        command_editor.text = ""
        state.command_active = False
        event.app.layout.focus(editor)
        event.app.vi_state.input_mode = event.app.vi_state.input_mode.__class__.NAVIGATION
        event.app.invalidate()

    @bindings.add(
        ":",
        filter=Condition(lambda: vi_navigation_mode() and not state.command_active),
        eager=True,
    )
    def enter_command_mode(event) -> None:
        state.command_active = True
        state.command_prefix = ":"
        editor.buffer.cancel_completion()
        command_editor.buffer.cancel_completion()
        command_editor.text = ":"
        command_editor.buffer.cursor_position = len(command_editor.text)
        event.app.layout.focus(command_editor)
        event.app.vi_state.input_mode = event.app.vi_state.input_mode.__class__.INSERT
        event.app.invalidate()

    @bindings.add(
        "/",
        filter=Condition(lambda: in_editor() and vi_navigation_mode() and not state.command_active),
        eager=True,
    )
    def open_slash_palette(event) -> None:
        state.command_active = True
        state.command_prefix = "/"
        editor.buffer.cancel_completion()
        command_editor.buffer.cancel_completion()
        command_editor.text = "/"
        command_editor.buffer.cursor_position = len(command_editor.text)
        event.app.layout.focus(command_editor)
        event.app.vi_state.input_mode = event.app.vi_state.input_mode.__class__.INSERT
        command_editor.buffer.start_completion(select_first=True)
        event.app.invalidate()

    @bindings.add(
        "/",
        filter=Condition(lambda: in_transcript() and vi_navigation_mode()),
        eager=True,
    )
    def search_responses(event) -> None:
        start_response_search(event)

    @bindings.add(
        "n",
        filter=Condition(lambda: in_transcript() and vi_navigation_mode() and bool(state.search_query)),
    )
    def next_response_match(event) -> None:
        apply_response_search(event.app, SearchDirection.FORWARD, wrap=True)

    @bindings.add(
        "N",
        filter=Condition(lambda: in_transcript() and vi_navigation_mode() and bool(state.search_query)),
    )
    def previous_response_match(event) -> None:
        apply_response_search(event.app, SearchDirection.BACKWARD, wrap=True)

    @bindings.add("tab", filter=Condition(lambda: in_editor() and vi_navigation_mode()), eager=True)
    @bindings.add("c-i", filter=Condition(lambda: in_editor() and vi_navigation_mode()), eager=True)
    def focus_results(event) -> None:
        open_transcript(event.app)

    @bindings.add("tab", filter=in_transcript, eager=True)
    @bindings.add("c-i", filter=in_transcript, eager=True)
    def return_to_editor(event) -> None:
        focus_editor(event.app)

    @bindings.add("[", filter=in_transcript, eager=True)
    def previous_response(event) -> None:
        state.selected_response = max(0, state.selected_response - 1)
        refresh_selection_marker(event.app)

    @bindings.add("]", filter=in_transcript, eager=True)
    def next_response(event) -> None:
        state.selected_response = min(len(state.responses) - 1, state.selected_response + 1)
        refresh_selection_marker(event.app)

    @bindings.add(
        "v",
        filter=Condition(lambda: in_transcript() and vi_navigation_mode()),
        eager=True,
    )
    def begin_character_selection(event) -> None:
        transcript.buffer.start_selection(SelectionType.CHARACTERS)

    @bindings.add(
        "V",
        filter=Condition(lambda: in_transcript() and vi_navigation_mode()),
        eager=True,
    )
    def begin_line_selection(event) -> None:
        transcript.buffer.start_selection(SelectionType.LINES)

    @bindings.add("y", filter=Condition(lambda: in_transcript() and not has_approval()))
    def yank_response(event) -> None:
        if transcript.buffer.selection_state is not None:
            selected = transcript.buffer.copy_selection().text
            if not selected:
                position = min(
                    transcript.buffer.cursor_position,
                    max(0, len(transcript.buffer.text) - 1),
                )
                selected = transcript.buffer.text[position : position + 1]
            copy_selected(event.app, selected)
        else:
            copy_selected(event.app)

    @bindings.add("escape", filter=Condition(lambda: in_transcript() and transcript.buffer.selection_state is not None), eager=True)
    def cancel_visual_selection(event) -> None:
        transcript.buffer.exit_selection()
        event.app.invalidate()

    @bindings.add("y", filter=Condition(lambda: has_approval() and vi_navigation_mode()))
    def approve_operation(event) -> None:
        set_approval(True)

    @bindings.add("n", filter=Condition(lambda: has_approval() and vi_navigation_mode()))
    def deny_operation(event) -> None:
        set_approval(False)

    @bindings.add("c-c", eager=True)
    def cancel_or_exit(event) -> None:
        nonlocal active_task
        if state.approval_future is not None:
            set_approval(False)
        if active_task is not None and not active_task.done():
            agent.cancel()
            active_task.cancel()
            state.activities.append("Cancelling task…")
            event.app.invalidate()
        else:
            event.app.exit(result=130)

    @bindings.add(
        "c-d",
        filter=Condition(lambda: in_transcript() and vi_navigation_mode()),
        eager=True,
    )
    def scroll_results_half_page(event) -> None:
        render_info = transcript.window.render_info
        page_rows = render_info.window_height if render_info is not None else 1
        transcript.buffer.cursor_down(count=max(1, page_rows // 2))

    @bindings.add("c-d", filter=in_editor, eager=True)
    def exit_control_d(event) -> None:
        if state.approval_future is not None:
            add_notice("Resolve the pending approval before exiting.")
        elif active_task is not None and not active_task.done():
            add_notice("Task is running. Use :cancel before exiting.")
        else:
            event.app.exit(result=0)

    @bindings.add("^", filter=Condition(lambda: in_editor() and vi_navigation_mode()))
    def first_character(event) -> None:
        event.current_buffer.cursor_position = 0

    @bindings.add("$", filter=Condition(lambda: in_editor() and vi_navigation_mode()))
    def last_character(event) -> None:
        event.current_buffer.cursor_position = max(0, len(event.current_buffer.text) - 1)

    @bindings.add("g", "g", filter=Condition(lambda: in_editor() and vi_navigation_mode()))
    def first_line(event) -> None:
        event.current_buffer.cursor_position = 0

    @bindings.add("G", filter=Condition(lambda: in_editor() and vi_navigation_mode()))
    def last_line(event) -> None:
        event.current_buffer.cursor_position = max(0, len(event.current_buffer.text) - 1)

    @bindings.add("h", filter=Condition(lambda: in_transcript() and vi_navigation_mode()))
    def transcript_left(event) -> None:
        transcript.buffer.cursor_left()

    @bindings.add("l", filter=Condition(lambda: in_transcript() and vi_navigation_mode()))
    def transcript_right(event) -> None:
        transcript.buffer.cursor_right()

    @bindings.add("j", filter=Condition(lambda: in_transcript() and vi_navigation_mode()))
    def transcript_down(event) -> None:
        transcript.buffer.cursor_down()

    @bindings.add("k", filter=Condition(lambda: in_transcript() and vi_navigation_mode()))
    def transcript_up(event) -> None:
        transcript.buffer.cursor_up()

    @bindings.add("c-u", filter=in_transcript)
    def transcript_page_up(event) -> None:
        transcript.buffer.cursor_up(count=max(1, event.app.output.get_size().rows // 2))

    def handle_command(command: str, app: Application) -> None:
        nonlocal active_task
        command = command.strip()
        if command.startswith(":") or command.startswith("/"):
            command = command[1:]
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            add_notice(f"Invalid command syntax: {exc}")
            return
        if not parts:
            return
        name, *arguments = parts
        if name in {"w", "write"}:
            if len(arguments) != 1:
                add_notice("Usage: :w <workspace-relative-file> (saves the selected result)")
                return
            if not state.responses:
                add_notice("No completed result to save.")
                return
            if not 0 <= state.selected_response < len(state.responses):
                add_notice("Select a result before saving.")
                return
            relative = Path(arguments[0])
            root = settings.workspace.resolve()
            if relative.is_absolute() or ".." in relative.parts:
                add_notice("Result path must stay within the workspace.")
                return
            target = root / relative
            try:
                resolved = target.resolve(strict=False)
                if not resolved.is_relative_to(root):
                    raise ValueError("Result path resolves outside the workspace.")
                current = root
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError("Result path cannot contain symlinks.")
                if not target.parent.is_dir():
                    raise ValueError("Result directory does not exist.")
                if target.exists() and not target.is_file():
                    raise ValueError("Result target is not a regular file.")
                response = state.responses[state.selected_response]
                target.write_text(response.text, encoding="utf-8")
            except (OSError, ValueError, RuntimeError) as exc:
                add_notice(f"Could not save result: {exc}")
            else:
                add_notice(
                    f"Saved result {state.selected_response + 1} to {relative.as_posix()}."
                )
        elif name == "s":
            if arguments:
                add_notice("Usage: :s")
            else:
                start_agent(editor.text, app)
        elif name in {"q", "q!", "quit", "exit"}:
            if active_task is not None and not active_task.done():
                state.activities.append("Task is running; use :cancel before quitting.")
                app.invalidate()
            else:
                app.exit(result=0)
        elif name == "cancel":
            if active_task is not None and not active_task.done():
                agent.cancel()
                active_task.cancel()
                state.activities.append("Cancelling current task…")
                state.activities = state.activities[-3:]
                app.invalidate()
            else:
                add_notice("No active task.")
        elif name in {"results", "r"}:
            open_transcript(app)
        elif name in {"y", "copy"}:
            copy_selected(app)
        elif name == "status":
            add_notice(
                f"Provider {provider.name}/{provider.model} · requests {agent.request_count} · "
                f"tool calling {'on' if provider.capabilities.tool_calling else 'off'} · "
                f"tool calls {agent.tool_call_count} · workspace {settings.workspace}"
            )
        elif name == "usage":
            usage = state.latest_usage
            if usage is None:
                add_notice("Usage metadata unavailable.")
            else:
                cache_usage = (
                    str(usage.cached_input_tokens)
                    if usage.cached_input_tokens is not None
                    else "unavailable"
                )
                add_notice(
                    f"Usage · input {usage.input_tokens if usage.input_tokens is not None else 'unavailable'} · "
                    f"output {usage.output_tokens if usage.output_tokens is not None else 'unavailable'} · "
                    f"cached input {cache_usage}"
                )
        elif name in {"allow", "deny"}:
            set_approval(name == "allow")
        elif name == "clear":
            state.items.clear()
            redraw_transcript(transcript, app)
        elif name == "help":
            add_notice(
                "Enter newline · Ctrl+S send (:s fallback) · "
                "Ctrl+D quit from prompt / half-page down in results · "
                ":w <file> save response · @ files · Tab responses · "
                "/ search, n/N repeat · [ older / ] newer response for save/copy · "
                "v/V select · y copy · :status tool state"
            )
        else:
            prefix = state.command_prefix if state.command_prefix == "/" else ":"
            add_notice(f"Unknown command {prefix}{name}. Use /help or :help.")

    def header() -> list[tuple[str, str]]:
        return [
            ("class:brand", f" Velocity-Mode {__version__} "),
            ("class:dim", f" {provider.name}/{provider.model} "),
            (
                "class:dim",
                f" tools:{'on' if provider.capabilities.tool_calling else 'off'} ",
            ),
            ("class:dim", f"\n Workspace: {settings.workspace}"),
        ]

    def activity_text() -> list[tuple[str, str]]:
        if state.pending_approval:
            return [("class:approval", " Approval required · press y to approve or n to deny")]
        if state.activities:
            return [("class:activity", "  · " + "   · ".join(state.activities[-2:]))]
        return [("", "")]

    def approval_text() -> list[tuple[str, str]]:
        if not state.pending_approval:
            return []
        return [("class:approval", state.pending_approval)]

    command_row = ConditionalContainer(
        command_editor,
        filter=command_active,
    )
    search_row = ConditionalContainer(search_toolbar.container, filter=search_active)
    approval_row = ConditionalContainer(
        Window(
            FormattedTextControl(approval_text),
            height=Dimension(min=1, max=8),
            wrap_lines=True,
            style="class:approval",
        ),
        filter=Condition(lambda: bool(state.pending_approval)),
    )
    root = HSplit(
        [
            Window(FormattedTextControl(header), height=2, style="class:header"),
            Window(height=1, char="─", style="class:rule"),
            transcript,
            approval_row,
            Window(FormattedTextControl(activity_text), height=1),
            Window(height=1, char="─", style="class:rule"),
            editor,
            command_row,
            search_row,
            Window(FormattedTextControl(toolbar_text), height=1, style="class:toolbar"),
        ]
    )
    style = Style.from_dict(TUI_STYLE_RULES)
    app = Application(
        layout=Layout(
            FloatContainer(
                content=root,
                floats=[
                    Float(
                        xcursor=True,
                        ycursor=True,
                        content=CompletionsMenu(
                            max_height=8,
                            extra_filter=Condition(
                                lambda: in_editor() or state.command_active
                            ),
                        ),
                    )
                ],
            ),
            focused_element=editor,
        ),
        key_bindings=bindings,
        style=style,
        full_screen=True,
        mouse_support=True,
        editing_mode=EditingMode.VI,
    )

    def initialize() -> None:
        app.vi_state.input_mode = app.vi_state.input_mode.__class__.INSERT
        editor.buffer.cursor_position = len(editor.text)
        if session_id is not None:
            add_notice(f"Session {session_id} · automatically saved")

    try:
        result = await app.run_async(pre_run=initialize)
        return result if isinstance(result, int) else 0
    finally:
        if persist_handle is not None:
            persist_handle.cancel()
        persist_session()
        if active_task is not None and not active_task.done():
            agent.cancel()
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
