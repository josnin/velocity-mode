from pathlib import Path

from prompt_toolkit.document import Document

from vmode.tui import (
    MAX_COMMAND_TRANSCRIPT_CHARS,
    MAX_PROMPT_VIEW_PAYLOAD_CHARS,
    SELECTED_RESPONSE_PREFIX,
    TUI_HELP_TEXT,
    TUI_STYLE_RULES,
    _TranscriptItem,
    _TranscriptLexer,
    _Response,
    _UIState,
    _approval_preview,
    _command_transcript_text,
    _safe_transcript_text,
    _prompt_view_text,
    _register_response,
)
from vmode.protocol import RequestSnapshot


def test_edit_approval_preview_shows_targeted_diff(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text("# Project\nAuthor: unknown\n", encoding="utf-8")

    preview = _approval_preview(
        "edit_file",
        {
            "path": "README.md",
            "old_text": "Author: unknown",
            "new_text": "Author: Josnin",
        },
        tmp_path,
    )

    assert preview.startswith("Approve edit to README.md?")
    assert "-Author: unknown" in preview
    assert "+Author: Josnin" in preview


def test_git_approval_previews_show_paths_and_message(tmp_path: Path):
    stage_preview = _approval_preview(
        "git_stage",
        {"paths": ["src/main.py"], "baseline_paths": ["src/main.py"]},
        tmp_path,
    )
    commit_preview = _approval_preview(
        "git_commit",
        {"paths": ["src/main.py"], "message": "feat: add main"},
        tmp_path,
    )

    assert "Approve Git staging" in stage_preview
    assert "src/main.py" in stage_preview
    assert "all current hunks" in stage_preview
    assert "Approve Git commit" in commit_preview
    assert "feat: add main" in commit_preview


def test_write_approval_preview_never_reads_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private content", encoding="utf-8")

    traversal_preview = _approval_preview(
        "write_file",
        {"path": "../secret.txt", "content": "replacement"},
        workspace,
    )
    assert "private content" not in traversal_preview
    assert "outside-workspace" in traversal_preview

    (workspace / "link.txt").symlink_to(secret)
    symlink_preview = _approval_preview(
        "write_file",
        {"path": "link.txt", "content": "replacement"},
        workspace,
    )
    assert "private content" not in symlink_preview
    assert "outside-workspace" in symlink_preview


def test_incomplete_terminal_error_is_registered_as_a_selectable_response():
    state = _UIState()
    error = _TranscriptItem(
        "assistant",
        "Error: Provider returned malformed tool-call arguments.",
        incomplete=True,
    )

    _register_response(state, error)

    assert error.response_id == 0
    assert state.selected_response == 0
    assert state.responses[0].text == error.text


def test_transcript_lexer_distinguishes_errors_and_fenced_code():
    document = Document(
        "Velocity-Mode · error\n"
        "  Error: Rate limit reached.\n"
        "Velocity-Mode\n"
        "  ```python\n"
        "  print('hello')\n"
        "  ```\n"
        "! Error: Provider stream interrupted."
    )
    get_line = _TranscriptLexer().lex_document(document)

    assert get_line(0)[0][0] == "class:transcript-error"
    assert get_line(1)[0][0] == "class:transcript-error"
    assert get_line(2)[0][0] == "class:transcript-assistant-label"
    assert get_line(3)[0][0] == "class:transcript-code"
    assert get_line(4)[0][0] == "class:transcript-code"
    assert get_line(6)[0][0] == "class:transcript-error"


def test_selected_response_uses_a_compact_underlined_target_label():
    document = Document(
        f"{SELECTED_RESPONSE_PREFIX}2/5 · Velocity-Mode\n"
        "  Selected response body"
    )
    get_line = _TranscriptLexer().lex_document(document)

    assert get_line(0)[0][0] == "class:transcript-selected"
    assert get_line(1)[0][0] == "class:transcript-assistant"
    assert "bg:" not in TUI_STYLE_RULES["transcript-selected"]
    assert TUI_STYLE_RULES["transcript-selected"].endswith("ansibrightblue")


def test_tui_styles_do_not_assume_terminal_background_or_truecolor():
    assert all("bg:" not in value and "#" not in value for value in TUI_STYLE_RULES.values())


def test_tui_help_collects_vim_controls_and_commands():
    assert "h/j/k/l" in TUI_HELP_TEXT
    assert "Ctrl+S send" in TUI_HELP_TEXT
    assert ":prompt" in TUI_HELP_TEXT
    assert ":provider <name> <model> [url]" in TUI_HELP_TEXT
    assert ":w <file>" in TUI_HELP_TEXT


def test_prompt_view_selects_one_pretty_printed_request_without_mutation():
    snapshots = [
        RequestSnapshot(1, '{"model":"mock","messages":[{"role":"user"}]}'),
        RequestSnapshot(2, '{"model":"mock","messages":[{"role":"tool"}]}'),
    ]
    response = _Response(4, "Completed", snapshots)

    rendered = _prompt_view_text(response, selected_snapshot=1)

    assert "PROMPT SENT — request 2 · 2/2" in rendered
    assert "request 1" not in rendered
    assert '"role": "tool"' in rendered
    assert snapshots == response.request_snapshots


def test_prompt_view_explains_missing_or_oversized_snapshots():
    assert "No captured request" in _prompt_view_text(_Response(1, "Completed"))
    rendered = _prompt_view_text(
        _Response(
            2,
            "Completed",
            [RequestSnapshot(3, None, "Request payload exceeds the local limit.")],
        )
    )
    assert "Request payload exceeds the local limit." in rendered
    rendered = _prompt_view_text(
        _Response(
            3,
            "Completed",
            [RequestSnapshot(4, "x" * (MAX_PROMPT_VIEW_PAYLOAD_CHARS + 1))],
        )
    )
    assert "[payload display truncated;" in rendered


def test_command_transcript_text_sanitizes_control_characters_and_caps_output():
    rendered = _command_transcript_text(
        {"argv": ["printf", "hello\x1b[31m"]},
        "exit code: 0\n" + ("x" * (MAX_COMMAND_TRANSCRIPT_CHARS + 1)),
    )

    assert "Command · printf 'hello�[31m'" in rendered
    assert "\x1b" not in rendered
    assert rendered.endswith("[display truncated]")
    assert _safe_transcript_text("line\rnext") == "line�next"
