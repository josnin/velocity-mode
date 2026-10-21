import json
import subprocess
import sys
from pathlib import Path

import pytest

from vmode.tools import ToolError, ToolRegistry, definitions


def _result_content(result):
    return json.loads(result.content)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )


def _git_repository(path: Path) -> None:
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "--", "base.txt")
    _git(path, "commit", "-m", "initial")


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
async def test_ripgrep_search_supports_regex_smart_case_and_literals(tmp_path: Path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "search file [1].txt").write_text(
        "needle\nNeedle\na+b\nab\n", encoding="utf-8"
    )
    registry = ToolRegistry(tmp_path)

    smart_case = _result_content(
        await registry.invoke("search_files", {"query": "needle"})
    )
    case_sensitive = _result_content(
        await registry.invoke("search_files", {"query": "Needle"})
    )
    literal = _result_content(
        await registry.invoke("search_files", {"query": "a+b", "literal": True})
    )
    regex = _result_content(
        await registry.invoke("search_files", {"query": "a+b"})
    )

    assert smart_case["backend"] == "rg"
    assert [match["text"] for match in smart_case["matches"]] == ["needle", "Needle"]
    assert [match["text"] for match in case_sensitive["matches"]] == ["Needle"]
    assert [match["text"] for match in literal["matches"]] == ["a+b"]
    assert [match["text"] for match in regex["matches"]] == ["ab"]
    assert literal["matches"][0]["path"] == "nested/search file [1].txt"
    assert literal["matches"][0]["line"] == 3


@pytest.mark.asyncio
async def test_file_discovery_includes_hidden_and_respects_ignore_policy(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("ignored/\n*.secret\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".hidden.py").write_text("visible", encoding="utf-8")
    (tmp_path / "nested" / "kept.txt").write_text("visible", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "skip.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / "token.secret").write_text("hidden", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.js").write_text("hidden", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("hidden", encoding="utf-8")

    result = _result_content(await ToolRegistry(tmp_path).invoke("list_files", {}))

    assert result["backend"] == "rg"
    assert "nested/.hidden.py" in result["paths"]
    assert "nested/kept.txt" in result["paths"]
    assert "ignored/skip.txt" not in result["paths"]
    assert "token.secret" not in result["paths"]
    assert "node_modules/skip.js" not in result["paths"]
    assert ".git/config" not in result["paths"]


@pytest.mark.asyncio
async def test_search_distinguishes_no_matches_and_invalid_regex(tmp_path: Path):
    (tmp_path / "source.py").write_text("needle\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    no_matches = _result_content(
        await registry.invoke("search_files", {"query": "absent"})
    )
    assert no_matches["matches"] == []
    assert not no_matches["truncated"]
    assert no_matches["backend"] == "rg"
    with pytest.raises(ToolError, match="invalid search regex"):
        await registry.invoke("search_files", {"query": "["})


@pytest.mark.asyncio
async def test_missing_ripgrep_is_reported_explicitly(tmp_path: Path):
    registry = ToolRegistry(tmp_path, rg_executable="missing-rg")

    with pytest.raises(ToolError, match=r"ripgrep \(rg\) is required"):
        await registry.invoke("list_files", {})
    with pytest.raises(ToolError, match=r"ripgrep \(rg\) is required"):
        await registry.invoke("search_files", {"query": "needle"})


@pytest.mark.asyncio
async def test_search_output_limits_and_result_caps_are_reported(tmp_path: Path):
    (tmp_path / "many.txt").write_text(
        "".join(f"needle {number}\n" for number in range(600)), encoding="utf-8"
    )

    capped = _result_content(
        await ToolRegistry(tmp_path, output_limit=300).invoke(
            "search_files", {"query": "needle"}
        )
    )
    result_capped = _result_content(
        await ToolRegistry(tmp_path).invoke("search_files", {"query": "needle"})
    )

    assert capped["truncated"]
    assert len(json.dumps(capped, separators=(",", ":"))) <= 300
    assert result_capped["truncated"]
    assert len(result_capped["matches"]) == 500


@pytest.mark.asyncio
async def test_ripgrep_timeout_and_model_arguments_cannot_inject_flags(
    tmp_path: Path,
):
    sleeper = tmp_path / "sleeping-rg"
    sleeper.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(2)\n", encoding="utf-8"
    )
    sleeper.chmod(0o755)
    with pytest.raises(ToolError, match="timed out"):
        await ToolRegistry(
            tmp_path, rg_executable=str(sleeper), search_timeout=0.01
        ).invoke("search_files", {"query": "needle"})

    (tmp_path / "source.txt").write_text("needle", encoding="utf-8")
    result = _result_content(
        await ToolRegistry(tmp_path).invoke(
            "search_files", {"query": "--glob=!*", "literal": True}
        )
    )
    assert result["matches"] == []
    with pytest.raises(ToolError, match="literal must be a boolean"):
        await ToolRegistry(tmp_path).invoke(
            "search_files", {"query": "needle", "literal": "true"}
        )


@pytest.mark.asyncio
async def test_discovery_and_search_do_not_follow_symlink_escapes(
    tmp_path: Path, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("outside-search") / "secret.txt"
    outside.write_text("needle", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)
    registry = ToolRegistry(tmp_path)

    listed = _result_content(await registry.invoke("list_files", {}))
    searched = _result_content(await registry.invoke("search_files", {"query": "needle"}))

    assert "escape.txt" not in listed["paths"]
    assert searched["matches"] == []


def test_search_tool_schema_keeps_existing_calls_compatible():
    search = next(
        item["function"] for item in definitions() if item["function"]["name"] == "search_files"
    )
    assert search["parameters"]["required"] == ["query"]
    assert search["parameters"]["properties"]["literal"] == {"type": "boolean"}


@pytest.mark.asyncio
async def test_write_and_command_require_approval(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    denied = await registry.invoke("write_file", {"path": "new.txt", "content": "no"})
    assert not denied.approved
    assert not (tmp_path / "new.txt").exists()
    command = await registry.invoke("run_command", {"argv": ["printf", "no"]})
    assert not command.approved


@pytest.mark.asyncio
async def test_git_status_and_diff_handle_baseline_and_unusual_paths(tmp_path: Path):
    _git_repository(tmp_path)
    (tmp_path / "base.txt").write_text("changed\n", encoding="utf-8")
    unusual = tmp_path / "space\tname.txt"
    unusual.write_text("untracked content\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)
    await registry.begin_task()

    status = _result_content(await registry.invoke("git_status", {}))
    by_path = {entry["path"]: entry for entry in status["entries"]}
    assert by_path["base.txt"]["unstaged"]
    assert by_path["space\tname.txt"]["untracked"]

    diff = _result_content(await registry.invoke("git_diff", {"paths": ["base.txt"]}))
    assert "changed" in diff["diff"]
    untracked = _result_content(await registry.invoke("git_diff", {}))
    assert untracked["untracked"] == ["space\tname.txt"]
    assert "untracked content" not in untracked["diff"]


@pytest.mark.asyncio
async def test_git_stage_preserves_baseline_changes_and_commits_verified_paths(
    tmp_path: Path,
):
    _git_repository(tmp_path)
    (tmp_path / "user.txt").write_text("user work\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "user.txt")
    (tmp_path / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    async def approve(name, arguments):
        return True

    registry = ToolRegistry(tmp_path, approve=approve)
    await registry.begin_task()
    await registry.invoke("write_file", {"path": "task.txt", "content": "task work\n"})
    with pytest.raises(ToolError, match="baseline user changes"):
        await registry.invoke("git_stage", {"paths": ["user.txt"]})
    staged = _result_content(await registry.invoke("git_stage", {"paths": ["task.txt"]}))
    assert "task.txt" in staged["staged_paths"]
    with pytest.raises(ToolError, match="differ from VMode-verified"):
        await registry.invoke("git_commit", {"message": "feat: add task"})
    assert (tmp_path / "notes.txt").exists()


@pytest.mark.asyncio
async def test_git_commit_requires_approval_and_adds_one_trailer(tmp_path: Path):
    _git_repository(tmp_path)

    async def approve(name, arguments):
        return True

    registry = ToolRegistry(tmp_path, approve=approve)
    await registry.begin_task()
    await registry.invoke("write_file", {"path": "task.txt", "content": "task work\n"})
    with pytest.raises(ToolError, match="at least one explicit path"):
        await registry.invoke("git_stage", {"paths": []})
    await registry.invoke("git_stage", {"paths": ["task.txt"]})
    committed = _result_content(
        await registry.invoke(
            "git_commit",
            {
                "message": (
                    "feat: add task\n\n"
                    "Co-authored-by: Velocity-Mode <velocity-mode@users.noreply.github.com>"
                )
            },
        )
    )
    assert len(committed["commit"]) == 40
    message = _git(tmp_path, "log", "-1", "--format=%B").stdout
    assert message.count("Co-authored-by: Velocity-Mode <velocity-mode@users.noreply.github.com>") == 1


@pytest.mark.asyncio
async def test_git_stage_and_commit_require_approval(tmp_path: Path):
    _git_repository(tmp_path)

    async def approve(name, arguments):
        return name == "write_file"

    registry = ToolRegistry(tmp_path, approve=approve)
    await registry.begin_task()
    await registry.invoke("write_file", {"path": "task.txt", "content": "task work\n"})
    staged = await registry.invoke("git_stage", {"paths": ["task.txt"]})
    assert not staged.approved


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
async def test_edit_file_requires_approval_and_one_exact_target(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text("# Project\nAuthor: unknown\n", encoding="utf-8")
    denied = await ToolRegistry(tmp_path).invoke(
        "edit_file",
        {
            "path": "README.md",
            "old_text": "Author: unknown",
            "new_text": "Author: Josnin",
        },
    )
    assert not denied.approved
    assert path.read_text(encoding="utf-8") == "# Project\nAuthor: unknown\n"

    async def approve(name, arguments):
        return True

    registry = ToolRegistry(tmp_path, approve=approve)
    result = await registry.invoke(
        "edit_file",
        {
            "path": "README.md",
            "old_text": "Author: unknown",
            "new_text": "Author: Josnin",
        },
    )
    assert result.content == "Edited README.md"
    assert path.read_text(encoding="utf-8") == "# Project\nAuthor: Josnin\n"
    with pytest.raises(ToolError, match="exactly once"):
        await registry.invoke(
            "edit_file",
            {"path": "README.md", "old_text": "missing", "new_text": "value"},
        )


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
