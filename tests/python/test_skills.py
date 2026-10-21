from pathlib import Path

import pytest

from vmode.agent import Agent
from vmode.protocol import ProviderCapabilities, ProviderEvent
from vmode.skills import SkillCatalog
from vmode.tools import ToolRegistry


def write_skill(
    workspace: Path,
    identifier: str,
    *,
    version: str = "1.0.0",
    tags: tuple[str, ...] = (),
    triggers: tuple[str, ...] = (),
    content: str = "Use the project helper.",
) -> Path:
    directory = workspace / ".vmode" / "skills"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identifier}.md"
    path.write_text(
        "+++\n"
        f'id = "{identifier}"\n'
        f'version = "{version}"\n'
        'description = "Test skill"\n'
        f"tags = {list(tags)!r}\n"
        f"triggers = {list(triggers)!r}\n"
        "+++\n"
        f"{content}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_catalog_selects_an_unambiguous_deterministic_match(tmp_path: Path):
    write_skill(tmp_path, "python-tests", tags=("python", "pytest"))

    class UnusedProvider:
        async def stream(self, messages, tools):
            raise AssertionError("unambiguous matching must not select with the model")
            yield

    selected = await SkillCatalog(tmp_path).select("Run the pytest suite", UnusedProvider())
    assert selected.source == "deterministic"
    assert [skill.identifier for skill in selected.skills] == ["python-tests"]


@pytest.mark.asyncio
async def test_catalog_uses_strict_validated_model_selection_for_tied_matches(tmp_path: Path):
    write_skill(tmp_path, "alpha", triggers=("review",))
    write_skill(tmp_path, "beta", triggers=("review",))

    class Selector:
        async def stream(self, messages, tools):
            assert tools == []
            assert messages[0].role == "system"
            yield ProviderEvent("text", text='{"skills":["beta"]}')
            yield ProviderEvent("response", finish_reason="stop")

    selected = await SkillCatalog(tmp_path).select("Review this change", Selector())
    assert selected.source == "model"
    assert [skill.identifier for skill in selected.skills] == ["beta"]


@pytest.mark.asyncio
async def test_catalog_falls_back_to_tied_deterministic_candidates_when_selector_is_invalid(
    tmp_path: Path,
):
    write_skill(tmp_path, "alpha", triggers=("review",))
    write_skill(tmp_path, "beta", triggers=("review",))

    class InvalidSelector:
        async def stream(self, messages, tools):
            yield ProviderEvent("text", text='{"skills":["outside-catalog"]}')
            yield ProviderEvent("response", finish_reason="stop")

    selected = await SkillCatalog(tmp_path).select("Review this change", InvalidSelector())
    assert selected.source == "deterministic fallback"
    assert [skill.identifier for skill in selected.skills] == ["alpha", "beta"]


def test_catalog_rejects_symlinks_and_invalid_manifests(tmp_path: Path):
    directory = tmp_path / ".vmode" / "skills"
    directory.mkdir(parents=True)
    external = tmp_path / "external.md"
    external.write_text("+++\n+++\ntext\n", encoding="utf-8")
    (directory / "linked.md").symlink_to(external)
    with pytest.raises(ValueError, match="non-symlink"):
        SkillCatalog(tmp_path).load()

    (directory / "linked.md").unlink()
    (directory / "invalid.md").write_text("no front matter", encoding="utf-8")
    with pytest.raises(ValueError, match="TOML front matter"):
        SkillCatalog(tmp_path).load()


def test_catalog_rejects_a_symlinked_skill_container(tmp_path: Path):
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / ".vmode").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="Skill container"):
        SkillCatalog(tmp_path).load()


@pytest.mark.asyncio
async def test_agent_freezes_skill_snapshot_for_a_task_and_reloads_on_the_next_task(
    tmp_path: Path,
):
    skill = write_skill(tmp_path, "testing", triggers=("test",), content="Use version one.")

    class RecordingProvider:
        name = "recording"
        model = "test"
        capabilities = ProviderCapabilities(streaming=True, tool_calling=False)

        def __init__(self):
            self.requests = []

        async def stream(self, messages, tools):
            self.requests.append(list(messages))
            yield ProviderEvent("text", text="completed")
            yield ProviderEvent("response", finish_reason="stop")

    provider = RecordingProvider()
    agent = Agent(provider, ToolRegistry(tmp_path), system_prompt="stable system prefix")
    events = [event async for event in agent.run("Test the project")]
    assert events[-1].kind == "done"
    assert provider.requests[0][0].content == "stable system prefix"
    assert "testing@1.0.0" in provider.requests[0][-1].content
    assert "Use version one." in provider.requests[0][-1].content

    write_skill(
        tmp_path,
        "testing",
        version="2.0.0",
        triggers=("test",),
        content="Use version two.",
    )
    events = [event async for event in agent.run("Test the project")]
    assert events[-1].kind == "done"
    assert provider.requests[1][0].content == "stable system prefix"
    assert "testing@2.0.0" in provider.requests[1][-1].content
    assert "Use version two." in provider.requests[1][-1].content
    assert skill.exists()
