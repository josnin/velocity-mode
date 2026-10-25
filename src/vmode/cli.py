from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from . import __version__
from .agent import Agent
from .config import Settings, load_settings
from .instructions import build_system_prompt
from .protocol import AgentLimits
from .providers import (
    GeminiProvider,
    GroqProvider,
    HuggingFaceProvider,
    MockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    Provider,
)
from .sessions import SessionStore
from .tools import ToolRegistry

COMMANDS = {
    "/help": "Show commands and key bindings",
    "/status": "Show provider, model, and usage",
    "/usage": "Show token usage",
    "/s": "Submit the current prompt (fallback)",
    "/results": "Browse previous responses",
    "/prompt": "Inspect the selected response's sent request",
    "/copy": "Yank the selected response",
    "/clear": "Clear the visible transcript",
    "/quit": "Exit Velocity-Mode",
    "/exit": "Exit Velocity-Mode",
}


class VModeCompleter(Completer):
    def __init__(self, workspace: Path, commands_enabled: bool = True):
        self.files = ToolRegistry(workspace)
        self.commands_enabled = commands_enabled

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if (
            self.commands_enabled
            and text.startswith("/")
            and "\n" not in text
            and " " not in text
        ):
            query = text.lower()
            for command, description in COMMANDS.items():
                if query in command.lower() or query in description.lower():
                    yield Completion(
                        command,
                        start_position=-len(text),
                        display=command,
                        display_meta=description,
                    )
            return
        boundary = max(text.rfind(" "), text.rfind("\n"), text.rfind("("))
        token = text[boundary + 1 :]
        if not token.startswith("@"):
            return
        prefix = token[1:]
        for path in self.files.iter_files():
            value = path.relative_to(self.files.root).as_posix()
            if value.startswith(prefix) or Path(value).name.startswith(Path(prefix).name):
                reference = "@" + value
                yield Completion(
                    reference,
                    start_position=-len(token),
                    display=reference,
                    display_meta="Project file",
                )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmode",
        description="Velocity-Mode coding agent",
        epilog=(
            "Interactive: Enter adds lines; Ctrl+S submits; Ctrl+D quits from the prompt "
            "and scrolls half a page down in results; :s is a fallback. "
            "Normal-mode / opens commands in the prompt and searches results; "
            ":w <file> saves the selected response. "
            "Insert-mode @ completes project files. "
            "Writes and commands require interactive approval; --exec refuses "
            "approval-required operations."
        ),
    )
    parser.add_argument("prompt", nargs="?", help="initial task or prompt")
    parser.add_argument("--version", action="version", version=f"vmode {__version__}")
    parser.add_argument(
        "--provider",
        choices=(
            "mock",
            "gemini",
            "groq",
            "huggingface",
            "ollama",
            "openai-compatible",
        ),
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument(
        "--timeout",
        type=float,
        help="overall task and per-request timeout in seconds (default: 300; max: 600)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        help="maximum provider requests per task (default: 24; max: 100)",
    )
    parser.add_argument(
        "--max-history-chars",
        type=int,
        help="maximum serialized characters retained from prior turns (default: 60000)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help="maximum generated tokens per provider response (default: 4096)",
    )
    parser.add_argument(
        "--tool-calling",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable model tool calls for a compatible provider/model",
    )
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--config", help="TOML config path")
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="SESSION_ID",
        help="resume the latest session for this workspace, or a specific session ID",
    )
    session_group.add_argument(
        "--list-sessions",
        action="store_true",
        help="list saved sessions for this workspace",
    )
    session_group.add_argument(
        "--delete-session",
        metavar="SESSION_ID",
        help="delete a saved session",
    )
    parser.add_argument(
        "--exec",
        dest="noninteractive",
        action="store_true",
        help="run one task without the interactive terminal",
    )
    parser.add_argument(
        "--mock-scenario",
        choices=("simple", "read-file"),
        default="simple",
        help="mock behavior; read-file demonstrates the complete tool cycle",
    )
    parser.add_argument(
        "--live-smoke",
        action="store_true",
        help="send a minimal live request (requires HF_TOKEN)",
    )
    return parser


def make_provider(settings: Settings, args: argparse.Namespace) -> Provider:
    if settings.provider == "mock":
        return MockProvider(settings.model, args.mock_scenario)
    if settings.provider == "huggingface":
        if settings.model == "mock/deterministic":
            raise ValueError(
                "Hugging Face requires a model ID; set --model or VMODE_MODEL"
            )
        return HuggingFaceProvider(
            settings.model,
            settings.base_url or "https://router.huggingface.co/v1",
            settings.timeout,
            settings.max_output_tokens,
            supports_tool_calling=settings.tool_calling,
        )
    if settings.provider == "gemini":
        if settings.model == "mock/deterministic":
            raise ValueError(
                "Google Gemini requires a model ID; set --model or VMODE_MODEL"
            )
        return GeminiProvider(
            settings.model,
            settings.base_url
            or "https://generativelanguage.googleapis.com/v1beta/openai",
            settings.timeout,
            settings.max_output_tokens,
            supports_tool_calling=settings.tool_calling,
        )
    if settings.provider == "groq":
        if settings.model == "mock/deterministic":
            raise ValueError("Groq requires a model ID; set --model or VMODE_MODEL")
        return GroqProvider(
            settings.model,
            settings.base_url or "https://api.groq.com/openai/v1",
            settings.timeout,
            settings.max_output_tokens,
            supports_tool_calling=settings.tool_calling,
        )
    if settings.provider == "openai-compatible":
        if settings.model == "mock/deterministic":
            raise ValueError(
                "The OpenAI-compatible provider requires a model ID; "
                "set --model or VMODE_MODEL"
            )
        if not settings.base_url:
            raise ValueError(
                "The OpenAI-compatible provider requires --base-url or "
                "provider.base_url in config"
            )
        return OpenAICompatibleProvider(
            settings.model,
            settings.base_url,
            settings.timeout,
            settings.max_output_tokens,
            supports_tool_calling=settings.tool_calling,
        )
    if settings.provider == "ollama":
        if settings.model == "mock/deterministic":
            raise ValueError("Ollama requires a model ID; set --model or VMODE_MODEL")
        return OllamaProvider(
            settings.model,
            settings.base_url or "http://127.0.0.1:11434/v1",
            settings.timeout,
            settings.max_output_tokens,
            supports_tool_calling=settings.tool_calling,
        )
    raise ValueError(f"Unsupported provider: {settings.provider}")


def _settings_for_session(
    args: argparse.Namespace, session_record: dict[str, Any]
) -> Settings:
    session_args = argparse.Namespace(**vars(args))
    session_args.provider = session_record["provider"]
    settings = load_settings(session_args)
    settings.model = session_record["model"]
    saved_tool_calling = session_record.get("tool_calling")
    tool_calling_overridden = (
        args.tool_calling is not None
        or "VMODE_TOOL_CALLING" in os.environ
        or "VIMA_TOOL_CALLING" in os.environ
    )
    if isinstance(saved_tool_calling, bool) and not tool_calling_overridden:
        settings.tool_calling = saved_tool_calling
    return settings


async def _interactive(settings: Settings, provider: Provider, initial: str = "") -> int:
    from .tui import run_tui

    return await run_tui(
        settings,
        provider,
        initial,
        lambda workspace, commands: VModeCompleter(workspace, commands),
    )


def _copy_response(response: str) -> bool:
    if len(response.encode("utf-8")) > 100_000 or not sys.stdout.isatty():
        return False
    payload = base64.b64encode(response.encode("utf-8")).decode("ascii")
    sys.stdout.write(f"\x1b]52;c;{payload}\x07")
    sys.stdout.flush()
    return True


async def _execute(settings: Settings, provider: Provider, prompt: str) -> int:
    async def deny(name, arguments):
        return False

    agent = Agent(
        provider,
        ToolRegistry(settings.workspace, approve=deny),
        AgentLimits(
            max_iterations=settings.max_iterations,
            max_history_chars=settings.max_history_chars,
            max_output_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout,
        ),
        system_prompt=build_system_prompt(provider, settings.workspace),
    )
    errors = False
    async for event in agent.run(prompt):
        if event.kind == "text":
            print(event.text, end="", flush=True)
        elif event.kind == "activity":
            print(f"\n  {event.text}", file=sys.stderr)
        elif event.kind == "error":
            errors = True
            print(f"\nvmode: {event.text}", file=sys.stderr)
    print()
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args)
        sessions = SessionStore(settings.workspace)
        if args.list_sessions:
            saved = sessions.list()
            if not saved:
                print("No saved sessions for this workspace.")
                return 0
            for item in saved:
                updated = item.updated_at.replace("T", " ")[:19]
                title = item.title.replace("\n", " ")
                print(f"{item.id}  {updated}  {item.provider}/{item.model}  {title}")
            return 0
        if args.delete_session:
            try:
                sessions.delete(args.delete_session)
            except ValueError as exc:
                parser.error(str(exc))
            print(f"Deleted session {args.delete_session}")
            return 0
        if args.resume:
            session_id = sessions.latest_id() if args.resume == "latest" else args.resume
            if session_id is None:
                parser.error("No saved sessions for this workspace")
            session_record = sessions.load(session_id)
            saved_provider = session_record["provider"]
            saved_model = session_record["model"]
            if args.provider and args.provider != saved_provider:
                parser.error(f"Session uses provider {saved_provider}")
            if args.model and args.model != saved_model:
                parser.error(f"Session uses model {saved_model}")
            settings = _settings_for_session(args, session_record)
            sessions = SessionStore(settings.workspace)
        else:
            session_id = None
            session_record = None
        if args.live_smoke:
            settings.provider = "huggingface"
            if settings.model == "mock/deterministic":
                settings.model = os.environ.get("HF_MODEL", "")
        provider = make_provider(settings, args)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.live_smoke:
        if not getattr(provider, "_token", ""):
            parser.error("--live-smoke requires HF_TOKEN")
        args.noninteractive = True
        args.prompt = args.prompt or "Reply with the single word Velocity-Mode."
    if args.resume and args.noninteractive:
        parser.error("--resume is available only in interactive mode")
    if args.noninteractive:
        if not args.prompt:
            parser.error("--exec requires a task argument")
        try:
            return asyncio.run(_execute(settings, provider, args.prompt))
        except ValueError as exc:
            parser.error(str(exc))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode requires a terminal; use --exec for scripts")
    if session_record and (session_record["provider"], session_record["model"]) != (
        provider.name,
        provider.model,
    ):
        parser.error("Selected provider cannot resume this session.")
    if session_id:
        print(f"Resuming session {session_id}", file=sys.stderr)
    try:
        from .tui import run_tui

        return asyncio.run(
            run_tui(
                settings,
                provider,
                args.prompt or "",
                lambda workspace, commands: VModeCompleter(workspace, commands),
                sessions=sessions,
                session_id=session_id,
                session_record=session_record,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
