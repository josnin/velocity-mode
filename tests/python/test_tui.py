from pathlib import Path

from prompt_toolkit.document import Document

from vmode.tui import (
    _TranscriptItem,
    _TranscriptLexer,
    _UIState,
    _approval_preview,
    _register_response,
)


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
