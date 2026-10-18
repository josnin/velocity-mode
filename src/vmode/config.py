from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    provider: str = "mock"
    model: str = "mock/deterministic"
    base_url: str = ""
    timeout: float = 60.0
    max_iterations: int = 12
    max_output_tokens: int = 2048
    tool_calling: bool = False
    workspace: Path = Path.cwd()


def load_settings(args: object) -> Settings:
    env = os.environ
    config_path = (
        getattr(args, "config", None)
        or env.get("VMODE_CONFIG")
        or env.get("VIMA_CONFIG")
    )
    if config_path is None:
        config_path = Path.home() / ".config/vmode/config.toml"
        legacy_config = Path.home() / ".config/vima/config.toml"
        if not config_path.exists() and legacy_config.exists():
            config_path = legacy_config
    file_values: dict = {}
    path = Path(config_path).expanduser()
    if path.exists():
        try:
            with path.open("rb") as stream:
                file_values = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Cannot read config {path}: {exc}") from exc
    provider = file_values.get("provider", {})
    agent = file_values.get("agent", {})
    if not isinstance(provider, dict) or not isinstance(agent, dict):
        raise ValueError("Config sections [provider] and [agent] must be tables")
    selected_provider = (
        getattr(args, "provider", None)
        or env.get("VMODE_PROVIDER", env.get("VIMA_PROVIDER"))
        or provider.get("name", "mock")
    )
    file_tool_calling = provider.get(
        "tool_calling", selected_provider == "huggingface"
    )
    if type(file_tool_calling) is not bool:
        raise ValueError("provider.tool_calling must be true or false")
    result = Settings(
        provider=selected_provider,
        model=provider.get("model", "mock/deterministic"),
        base_url=provider.get("base_url", ""),
        timeout=float(provider.get("timeout", 60)),
        max_iterations=int(agent.get("max_iterations", 12)),
        max_output_tokens=int(provider.get("max_output_tokens", 2048)),
        tool_calling=file_tool_calling,
        workspace=Path.cwd(),
    )
    result.model = getattr(args, "model", None) or env.get(
        "VMODE_MODEL", env.get("VIMA_MODEL", result.model)
    )
    result.base_url = (
        getattr(args, "base_url", None)
        or env.get("VMODE_BASE_URL", env.get("VIMA_BASE_URL"))
        or result.base_url
    )
    timeout = env.get("VMODE_TIMEOUT", env.get("VIMA_TIMEOUT"))
    if timeout is not None:
        result.timeout = float(timeout)
    if getattr(args, "timeout", None) is not None:
        result.timeout = args.timeout
    max_output_tokens = env.get(
        "VMODE_MAX_OUTPUT_TOKENS", env.get("VIMA_MAX_OUTPUT_TOKENS")
    )
    if max_output_tokens is not None:
        result.max_output_tokens = int(max_output_tokens)
    if getattr(args, "max_output_tokens", None) is not None:
        result.max_output_tokens = args.max_output_tokens
    env_tool_calling = env.get(
        "VMODE_TOOL_CALLING", env.get("VIMA_TOOL_CALLING")
    )
    if env_tool_calling is not None:
        normalized = env_tool_calling.strip().lower()
        if normalized not in {"1", "0", "true", "false", "yes", "no"}:
            raise ValueError("VMODE_TOOL_CALLING must be true or false")
        result.tool_calling = normalized in {"1", "true", "yes"}
    if getattr(args, "tool_calling", None) is not None:
        result.tool_calling = args.tool_calling
    if getattr(args, "max_iterations", None) is not None:
        result.max_iterations = args.max_iterations
    result.workspace = Path(
        getattr(args, "workspace", None) or Path.cwd()
    ).expanduser().resolve()
    if not result.workspace.is_dir():
        raise ValueError(f"Workspace is not a directory: {result.workspace}")
    if not 1 <= result.timeout <= 600:
        raise ValueError("Timeout must be between 1 and 600 seconds")
    if not 1 <= result.max_iterations <= 100:
        raise ValueError("Max iterations must be between 1 and 100")
    if not 1 <= result.max_output_tokens <= 100_000:
        raise ValueError("Max output tokens must be between 1 and 100000")
    if not result.model.strip():
        raise ValueError("Model ID cannot be empty")
    return result
