import subprocess
from pathlib import Path
from types import SimpleNamespace

from vmode.tui import _approval_preview, _copy_to_system_clipboard


def test_system_clipboard_prefers_wayland_helper(monkeypatch):
    calls = []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        "vmode.tui.shutil.which",
        lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None,
    )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("vmode.tui.subprocess.run", run)

    assert _copy_to_system_clipboard("terminal error")
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["wl-copy"]
    assert kwargs == {
        "input": "terminal error",
        "text": True,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 1,
        "check": False,
    }


def test_system_clipboard_reports_unavailable_helpers(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("vmode.tui.shutil.which", lambda name: None)

    assert not _copy_to_system_clipboard("terminal error")


def test_system_clipboard_continues_after_timed_out_helper(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        "vmode.tui.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"xclip", "xsel"} else None,
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "xclip":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("vmode.tui.subprocess.run", run)

    assert _copy_to_system_clipboard("terminal error")
    assert calls == [
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]


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
