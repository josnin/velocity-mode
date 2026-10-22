import base64
import io
from argparse import Namespace
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from vmode.cli import (
    VModeCompleter,
    _copy_response,
    _execute,
    _settings_for_session,
    make_parser,
    make_provider,
)
from vmode.config import Settings, load_settings
from vmode.protocol import ProviderCapabilities, ProviderEvent, ToolCall


def test_config_precedence_cli_over_environment_over_file(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        '[provider]\nname = "mock"\nmodel = "from-file"\n'
        'tool_calling = true\n'
        '[agent]\nmax_iterations = 7\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("VMODE_MODEL", "from-environment")
    args = Namespace(
        config=str(config),
        provider=None,
        model="from-cli",
        base_url=None,
        timeout=None,
        max_iterations=None,
        workspace=str(tmp_path),
    )
    settings = load_settings(args)
    assert settings.model == "from-cli"
    assert settings.max_iterations == 7
    assert settings.tool_calling
    args.model = None
    assert load_settings(args).model == "from-environment"
    args.tool_calling = False
    assert not load_settings(args).tool_calling


def test_legacy_vima_environment_is_supported_as_a_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VMODE_MODEL", raising=False)
    monkeypatch.setenv("VIMA_MODEL", "legacy-model")
    args = Namespace(
        config=str(tmp_path / "missing.toml"),
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=str(tmp_path),
    )
    assert load_settings(args).model == "legacy-model"
    monkeypatch.setenv("VMODE_MODEL", "new-model")
    assert load_settings(args).model == "new-model"


def test_legacy_default_config_is_used_when_new_config_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VMODE_CONFIG", raising=False)
    monkeypatch.delenv("VIMA_CONFIG", raising=False)
    monkeypatch.delenv("VMODE_MODEL", raising=False)
    monkeypatch.delenv("VIMA_MODEL", raising=False)
    old_config = tmp_path / ".config" / "vima" / "config.toml"
    old_config.parent.mkdir(parents=True)
    old_config.write_text('[provider]\nmodel = "legacy-config-model"\n', encoding="utf-8")
    args = Namespace(
        config=None,
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=str(tmp_path),
    )
    assert load_settings(args).model == "legacy-config-model"


def test_workspace_defaults_to_nearest_git_root_and_honors_explicit_directory(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    nested = project / "src" / "vmode"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    monkeypatch.chdir(nested)
    args = Namespace(
        config=str(tmp_path / "missing.toml"),
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=None,
    )

    assert load_settings(args).workspace == project

    args.workspace = nested
    assert load_settings(args).workspace == nested

    outside_repository = tmp_path / "outside-repository"
    outside_repository.mkdir()
    monkeypatch.chdir(outside_repository)
    args.workspace = None
    assert load_settings(args).workspace == outside_repository


def test_groq_uses_conservative_history_budget_unless_configured(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[provider]
name = "groq"

[agent]
max_history_chars = 24000
""",
        encoding="utf-8",
    )
    args = Namespace(
        config=str(config),
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=None,
    )

    assert load_settings(args).max_history_chars == 24_000

    config.write_text("[provider]\nname = \"groq\"\n", encoding="utf-8")
    assert load_settings(args).max_history_chars == 16_000

    config.write_text("[provider]\nname = \"mock\"\n", encoding="utf-8")
    assert load_settings(args).max_history_chars == 60_000


@pytest.mark.parametrize("provider", ("gemini", "groq", "huggingface"))
def test_tool_capable_provider_defaults_on_and_can_be_disabled(
    tmp_path, monkeypatch, provider
):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    args = Namespace(
        config=str(tmp_path / "missing.toml"),
        provider=provider,
        model="gemini-3.8-flash",
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=str(tmp_path),
    )
    assert load_settings(args).tool_calling
    args.tool_calling = False
    assert not load_settings(args).tool_calling


def test_gemini_provider_uses_developer_api_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    args = make_parser().parse_args(
        [
            "--provider",
            "gemini",
            "--model",
            "gemini-3.8-flash",
            "--workspace",
            str(tmp_path),
        ]
    )
    settings = load_settings(args)
    provider = make_provider(settings, args)

    assert provider.name == "gemini"
    assert provider.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert provider.capabilities.tool_calling


def test_groq_provider_uses_openai_compatible_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    args = make_parser().parse_args(
        [
            "--provider",
            "groq",
            "--model",
            "openai/gpt-oss-20b",
            "--workspace",
            str(tmp_path),
        ]
    )
    settings = load_settings(args)
    provider = make_provider(settings, args)

    assert provider.name == "groq"
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert provider.capabilities.tool_calling


def test_resumed_huggingface_session_restores_tool_calling_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    args = make_parser().parse_args(["--workspace", str(tmp_path)])
    settings = _settings_for_session(
        args,
        {"provider": "huggingface", "model": "vendor/model"},
    )
    provider = make_provider(settings, args)

    assert settings.provider == "huggingface"
    assert settings.model == "vendor/model"
    assert settings.tool_calling
    assert provider.capabilities.tool_calling


def test_resumed_session_restores_saved_tool_calling_override(tmp_path, monkeypatch):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    args = make_parser().parse_args(["--workspace", str(tmp_path)])
    settings = _settings_for_session(
        args,
        {
            "provider": "huggingface",
            "model": "vendor/model",
            "tool_calling": False,
        },
    )

    assert not settings.tool_calling


def test_explicit_cli_tool_calling_overrides_resumed_session(tmp_path, monkeypatch):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    args = make_parser().parse_args(
        ["--workspace", str(tmp_path), "--no-tool-calling"]
    )
    settings = _settings_for_session(
        args,
        {
            "provider": "huggingface",
            "model": "vendor/model",
            "tool_calling": True,
        },
    )

    assert not settings.tool_calling


def test_openai_compatible_provider_is_configurable_and_tools_default_off(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    config = tmp_path / "config.toml"
    config.write_text(
        '[provider]\nname = "openai-compatible"\nmodel = "vendor/model"\n'
        'base_url = "https://api.example.test/v1"\n',
        encoding="utf-8",
    )
    args = Namespace(
        config=str(config),
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=str(tmp_path),
        mock_scenario="simple",
    )
    settings = load_settings(args)
    provider = make_provider(settings, args)
    assert provider.name == "openai-compatible"
    assert provider.model == "vendor/model"
    assert not provider.capabilities.tool_calling

    settings.base_url = ""
    with pytest.raises(ValueError, match="requires --base-url"):
        make_provider(settings, args)


def test_ollama_provider_defaults_to_local_endpoint_and_tools_off(tmp_path, monkeypatch):
    monkeypatch.delenv("VMODE_PROVIDER", raising=False)
    monkeypatch.delenv("VMODE_TOOL_CALLING", raising=False)
    args = make_parser().parse_args(
        ["--provider", "ollama", "--model", "llama3.2", "--workspace", str(tmp_path)]
    )
    settings = load_settings(args)
    provider = make_provider(settings, args)

    assert provider.name == "ollama"
    assert provider.base_url == "http://127.0.0.1:11434/v1"
    assert not provider.capabilities.tool_calling


def test_cli_offers_documented_modes_and_explicit_noninteractive_flag():
    parser = make_parser()
    actions = {action.dest for action in parser._actions}
    assert {"provider", "model", "workspace", "noninteractive"} <= actions


def test_default_agent_iteration_limit_is_24(tmp_path: Path):
    args = Namespace(
        config=str(tmp_path / "missing.toml"),
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=str(tmp_path),
    )
    assert load_settings(args).max_iterations == 24


def test_default_task_timeout_is_300_and_can_be_overridden(tmp_path: Path, monkeypatch):
    args = Namespace(
        config=str(tmp_path / "missing.toml"),
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=str(tmp_path),
    )
    assert load_settings(args).timeout == 300
    args.timeout = 600
    assert load_settings(args).timeout == 600
    args.timeout = None
    monkeypatch.setenv("VMODE_TIMEOUT", "450")
    assert load_settings(args).timeout == 450


def test_default_output_token_budget_is_4096_and_can_be_overridden(
    tmp_path: Path, monkeypatch
):
    args = Namespace(
        config=str(tmp_path / "missing.toml"),
        provider=None,
        model=None,
        base_url=None,
        timeout=None,
        max_iterations=None,
        max_output_tokens=None,
        tool_calling=None,
        workspace=str(tmp_path),
    )
    assert load_settings(args).max_output_tokens == 4096
    args.max_output_tokens = 8192
    assert load_settings(args).max_output_tokens == 8192
    args.max_output_tokens = None
    monkeypatch.setenv("VMODE_MAX_OUTPUT_TOKENS", "6144")
    assert load_settings(args).max_output_tokens == 6144


def test_prompt_completer_provides_commands_and_file_references(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass\n", encoding="utf-8")
    completer = VModeCompleter(tmp_path)
    commands = list(completer.get_completions(Document("/sta"), CompleteEvent()))
    assert any(item.text == "/status" for item in commands)
    paths = list(completer.get_completions(Document("Inspect @src/a"), CompleteEvent()))
    assert any(item.text == "@src/app.py" for item in paths)


def test_copy_response_uses_terminal_osc52_clipboard(monkeypatch):
    class TerminalOutput(io.StringIO):
        def isatty(self):
            return True

    output = TerminalOutput()
    monkeypatch.setattr("sys.stdout", output)
    assert _copy_response("response\ntext")
    assert output.getvalue() == (
        "\x1b]52;c;" + base64.b64encode(b"response\ntext").decode() + "\x07"
    )


@pytest.mark.asyncio
async def test_noninteractive_mode_denies_write_tools_and_returns_failure(tmp_path: Path, capsys):
    class WriteProvider:
        name = "mock"
        model = "write-test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=True)

        async def stream(self, messages, tools):
            yield ProviderEvent(
                "response",
                tool_calls=[
                    ToolCall("write-1", "write_file", {"path": "new.txt", "content": "no"})
                ],
                finish_reason="tool_calls",
            )

    result = await _execute(Settings(workspace=tmp_path), WriteProvider(), "write a file")
    assert result == 1
    assert not (tmp_path / "new.txt").exists()
    assert "Approval required" in capsys.readouterr().err
