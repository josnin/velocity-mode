from pathlib import Path

from vmode.tui import _approval_preview


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
    stage_preview = _approval_preview("git_stage", {"paths": ["src/main.py"]}, tmp_path)
    commit_preview = _approval_preview(
        "git_commit",
        {"paths": ["src/main.py"], "message": "feat: add main"},
        tmp_path,
    )

    assert "Approve Git staging" in stage_preview
    assert "src/main.py" in stage_preview
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
