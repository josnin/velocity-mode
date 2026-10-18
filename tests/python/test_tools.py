from pathlib import Path

import pytest

from vmode.tools import ToolError, ToolRegistry


@pytest.mark.asyncio
async def test_file_tools_are_workspace_limited_and_search_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("needle = 1\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)
    assert (await registry.invoke("read_file", {"path": "src/a.py"})).content == "needle = 1\n"
    assert "src/a.py" in (await registry.invoke("list_files", {})).content
    assert "needle" in (await registry.invoke("search_files", {"query": "needle"})).content
    with pytest.raises(ToolError, match="workspace"):
        await registry.invoke("read_file", {"path": "../outside.txt"})


@pytest.mark.asyncio
async def test_write_and_command_require_approval(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    denied = await registry.invoke("write_file", {"path": "new.txt", "content": "no"})
    assert not denied.approved
    assert not (tmp_path / "new.txt").exists()
    command = await registry.invoke("run_command", {"argv": ["printf", "no"]})
    assert not command.approved


@pytest.mark.asyncio
async def test_approved_write_and_command_use_workspace(tmp_path: Path):
    async def approve(name, arguments):
        return True

    registry = ToolRegistry(tmp_path, approve=approve)
    result = await registry.invoke("write_file", {"path": "out.txt", "content": "ok"})
    assert result.approved
    assert (tmp_path / "out.txt").read_text() == "ok"
    result = await registry.invoke("run_command", {"argv": ["pwd"]})
    assert str(tmp_path) in result.content


@pytest.mark.asyncio
async def test_command_timeout_stops_subprocess(tmp_path: Path):
    async def approve(name, arguments):
        return True

    registry = ToolRegistry(tmp_path, approve=approve, command_timeout=0.05)
    with pytest.raises(TimeoutError):
        await registry.invoke(
            "run_command",
            {"argv": ["python3", "-c", "import time; time.sleep(2)"]},
        )


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected(tmp_path: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret"
    outside.write_text("secret")
    (tmp_path / "escape").symlink_to(outside)
    with pytest.raises(ToolError, match="outside"):
        await ToolRegistry(tmp_path).invoke("read_file", {"path": "escape"})


def test_selected_file_references_are_attached_and_secure(tmp_path: Path, tmp_path_factory):
    (tmp_path / "source.py").write_text("secret_value = 42\n")
    registry = ToolRegistry(tmp_path)
    attached = registry.attach_references("Inspect @source.py")
    assert attached.startswith("Inspect @source.py")
    assert "secret_value = 42" in attached
    assert registry.attach_references("user@example.com") == "user@example.com"

    outside = tmp_path_factory.mktemp("external") / "outside.py"
    outside.write_text("outside")
    (tmp_path / "escape.py").symlink_to(outside)
    with pytest.raises(ToolError, match="outside"):
        registry.attach_references("Inspect @escape.py")
    with pytest.raises(ToolError, match="workspace"):
        registry.attach_references("Inspect @../outside.py")
