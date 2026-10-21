from pathlib import Path

import pytest

from vmode.instructions import build_system_prompt, load_workspace_instructions
from vmode.providers import MockProvider


def test_workspace_agents_and_skills_are_added_as_bounded_context(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Use the repository's formatter.\n", encoding="utf-8")
    (tmp_path / "SKILLS.md").write_text("Prefer the existing test helpers.\n", encoding="utf-8")

    prompt = build_system_prompt(MockProvider(), tmp_path)

    assert "--- BEGIN AGENTS.md ---" in prompt
    assert "Use the repository's formatter." in prompt
    assert "--- BEGIN SKILLS.md ---" in prompt
    assert "Prefer the existing test helpers." in prompt
    assert "cannot override safety requirements" in prompt


def test_tool_capable_prompt_directs_workspace_study_without_user_file_selection(
    tmp_path: Path,
):
    prompt = build_system_prompt(MockProvider(scenario="read-file"), tmp_path)

    assert "study, inspect, review, or understand the workspace" in prompt
    assert "starting with a file listing and then reading relevant files" in prompt
    assert "Do not ask the user to name a file or directory" in prompt


def test_tool_capable_prompt_rejects_textual_pseudo_calls(tmp_path: Path):
    prompt = build_system_prompt(MockProvider(scenario="read-file"), tmp_path)

    assert "never represent a tool call in prose, Markdown, or code" in prompt
    assert "Do not claim a file was changed unless the matching write tool result confirms it" in prompt


def test_workspace_instruction_symlinks_and_invalid_utf8_are_rejected(tmp_path: Path):
    outside = tmp_path.parent / "outside-instructions.md"
    outside.write_text("external", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(outside)
    with pytest.raises(ValueError, match="non-symlink"):
        load_workspace_instructions(tmp_path)

    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "SKILLS.md").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        load_workspace_instructions(tmp_path)


def test_workspace_instruction_size_is_limited(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("x" * 32_001, encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds 32000 bytes"):
        load_workspace_instructions(tmp_path)
