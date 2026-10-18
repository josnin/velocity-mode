from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

EXCLUDED = {".git", ".hg", ".svn", "build", "dist", "target", "node_modules", ".venv", "__pycache__"}


class ToolError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolResult:
    content: str
    approved: bool = True


Approval = Callable[[str, dict[str, Any]], Awaitable[bool]]


def definitions() -> list[dict[str, Any]]:
    return [
        _function("read_file", "Read a UTF-8 project file", {"path": {"type": "string"}}),
        _function("list_files", "List permitted project files", {"pattern": {"type": "string"}}, required=[]),
        _function("search_files", "Search text in project files", {"query": {"type": "string"}}),
        _function("write_file", "Write UTF-8 content to a project file; requires approval", {"path": {"type": "string"}, "content": {"type": "string"}}),
        _function("run_command", "Run an approved command using argv in the project directory", {"argv": {"type": "array", "items": {"type": "string"}}}),
    ]


def _function(name: str, description: str, properties: dict,
              required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}}}


class ToolRegistry:
    def __init__(self, workspace: Path, approve: Approval | None = None,
                 output_limit: int = 100_000, command_timeout: float = 60.0):
        self.root = workspace.resolve()
        self.approve = approve
        self.output_limit = output_limit
        self.command_timeout = command_timeout
        self._ignore_patterns: tuple[str, ...] | None = None

    def attach_references(self, prompt: str, per_file_limit: int = 200_000,
                          total_limit: int = 500_000) -> str:
        references = re.finditer(
            r'(?<![^\s([{])@(?:"((?:\\.|[^"])*)"|([^\s,;)]+))', prompt
        )
        attached: list[tuple[str, str]] = []
        seen: set[Path] = set()
        total = 0
        for match in references:
            raw_path = match.group(1) if match.group(1) is not None else match.group(2)
            raw_path = raw_path.replace('\\"', '"')
            path = self._path(raw_path)
            if self._ignored(path):
                raise ToolError(f"file reference @{raw_path} is ignored")
            try:
                canonical = path.resolve(strict=True)
                if not canonical.is_relative_to(self.root):
                    raise ToolError(f"file reference @{raw_path} resolves outside the workspace")
                if not canonical.is_file():
                    raise ToolError(f"file reference @{raw_path} is not a regular file")
                if canonical in seen:
                    continue
                with canonical.open("rb") as stream:
                    data = stream.read(per_file_limit + 1)
            except OSError as exc:
                raise ToolError(f"cannot resolve file reference @{raw_path}: {exc.strerror}") from exc
            if b"\0" in data:
                raise ToolError(f"file reference @{raw_path} appears to be binary")
            if len(data) > per_file_limit or total + len(data) > total_limit:
                raise ToolError("referenced file context exceeds the configured size limit")
            if canonical in seen:
                continue
            seen.add(canonical)
            total += len(data)
            attached.append((canonical.relative_to(self.root).as_posix(),
                             data.decode("utf-8", errors="replace")))
        if not attached:
            return prompt
        context = "\n\n".join(
            f"[Referenced file: {path}]\n```\n{content}\n```"
            for path, content in attached
        )
        return f"{prompt}\n\nUser-selected file context:\n{context}"

    def _path(self, value: Any) -> Path:
        if not isinstance(value, str) or not value:
            raise ToolError("path must be a non-empty string")
        requested = Path(value)
        if requested.is_absolute() or ".." in requested.parts:
            raise ToolError("path must stay within the workspace")
        candidate = (self.root / requested).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise ToolError("path resolves outside the workspace")
        if any(part in EXCLUDED for part in requested.parts):
            raise ToolError("path is in an excluded directory")
        return candidate

    def _ignored(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            return True
        if self._ignore_patterns is None:
            try:
                self._ignore_patterns = tuple(
                    (self.root / ".gitignore").read_text(encoding="utf-8").splitlines()
                )
            except FileNotFoundError:
                self._ignore_patterns = ()
            except (OSError, UnicodeError) as exc:
                raise ToolError(f"cannot read workspace .gitignore: {exc}") from exc
        for pattern in self._ignore_patterns:
            pattern = pattern.strip()
            if pattern and not pattern.startswith("#") and not pattern.startswith("!"):
                pattern = pattern.lstrip("/")
                if fnmatch.fnmatch(relative, pattern) or any(
                    fnmatch.fnmatch(part, pattern.rstrip("/"))
                    for part in Path(relative).parts
                ):
                    return True
        return False

    def iter_files(self):
        scanned = 0
        for current, dirs, files in os.walk(self.root, topdown=True, followlinks=False):
            base = Path(current)
            dirs[:] = [
                name for name in dirs
                if name not in EXCLUDED
                and not (base / name).is_symlink()
                and not self._ignored(base / name)
            ]
            for name in files:
                path = base / name
                scanned += 1
                if scanned > 20_000:
                    return
                if not path.is_symlink() and not self._ignored(path):
                    yield path

    async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            raise ToolError("tool arguments must be an object")
        schemas = {
            "read_file": ({"path"}, {"path"}),
            "list_files": (set(), {"pattern"}),
            "search_files": ({"query"}, {"query"}),
            "write_file": ({"path", "content"}, {"path", "content"}),
            "run_command": ({"argv"}, {"argv"}),
        }
        if name not in schemas:
            raise ToolError(f"unknown tool: {name}")
        required, allowed = schemas[name]
        if not required.issubset(arguments) or not set(arguments).issubset(allowed):
            raise ToolError(f"malformed arguments for {name}")
        if name == "read_file":
            path = self._path(arguments.get("path"))
            if self._ignored(path):
                raise ToolError("file is ignored by .gitignore")
            try:
                with path.open("rb") as stream:
                    data = stream.read(self.output_limit + 1)
            except OSError as exc:
                raise ToolError(f"cannot read file: {exc.strerror}") from exc
            if len(data) > self.output_limit or b"\0" in data:
                raise ToolError("file is binary or exceeds the output limit")
            return ToolResult(data.decode("utf-8", errors="replace"))
        if name == "list_files":
            pattern = arguments.get("pattern", "*")
            if not isinstance(pattern, str):
                raise ToolError("pattern must be a string")
            paths = []
            for path in self.iter_files():
                rel = path.relative_to(self.root)
                if path.is_file() and fnmatch.fnmatch(rel.as_posix(), pattern):
                    paths.append(rel.as_posix())
                    if len(paths) >= 1000:
                        break
            return ToolResult("\n".join(paths))
        if name == "search_files":
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                raise ToolError("query must be a non-empty string")
            rows = []
            total_chars = 0
            for path in self.iter_files():
                rel = path.relative_to(self.root)
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_size > min(self.output_limit, 2_000_000):
                        continue
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError):
                    continue
                for number, line in enumerate(lines, 1):
                    if query not in line:
                        continue
                    row = f"{rel}:{number}: {line}"
                    remaining = self.output_limit - total_chars
                    if remaining <= 0:
                        break
                    row = row[:remaining]
                    rows.append(row)
                    total_chars += len(row)
                    if total_chars >= self.output_limit:
                        break
                if total_chars >= self.output_limit:
                    break
            return ToolResult("\n".join(rows))
        if name == "write_file":
            path, content = self._path(arguments.get("path")), arguments.get("content")
            if not isinstance(content, str):
                raise ToolError("content must be a string")
            if len(content.encode()) > self.output_limit:
                raise ToolError("write exceeds output limit")
            if not await self._approval(name, arguments):
                return ToolResult("Operation denied by user.", approved=False)
            if self._ignored(path):
                raise ToolError("refusing to modify a .gitignore-excluded file")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(f"Wrote {path.relative_to(self.root)}")
        if name == "run_command":
            argv = arguments.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
                raise ToolError("argv must be a non-empty list of strings")
            if not await self._approval(name, arguments):
                return ToolResult("Operation denied by user.", approved=False)
            if os.name != "posix":
                raise ToolError("run_command is supported only on POSIX platforms")
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=self.root, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                async def collect_output():
                    output = bytearray()
                    truncated = False
                    while chunk := await process.stdout.read(8192):
                        remaining = self.output_limit - len(output)
                        output.extend(chunk[:max(0, remaining)])
                        truncated = truncated or len(chunk) > remaining
                    await process.wait()
                    return bytes(output), truncated

                output, truncated = await asyncio.wait_for(
                    collect_output(), self.command_timeout
                )
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await process.wait()
                raise
            text = output[: self.output_limit].decode(errors="replace")
            if truncated:
                text += "\n[output truncated]"
            return ToolResult(f"exit code: {process.returncode}\n{text}")
        raise ToolError(f"unknown tool: {name}")

    async def _approval(self, name: str, arguments: dict[str, Any]) -> bool:
        if self.approve is None:
            return False
        return await self.approve(name, arguments)
