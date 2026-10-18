from pathlib import Path

from vmode.tui import _approval_preview


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
