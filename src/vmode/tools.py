from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

EXCLUDED = {".git", ".hg", ".svn", "build", "dist", "target", "node_modules", ".venv", "__pycache__"}
MAX_DISCOVERED_FILES = 1_000
MAX_SEARCH_MATCHES = 500
MAX_SEARCH_FILE_BYTES = 2_000_000


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
        _function(
            "search_files",
            "Search project files with a smart-case regular expression; set literal to true for exact text",
            {"query": {"type": "string"}, "literal": {"type": "boolean"}},
            required=["query"],
        ),
        _function("write_file", "Write UTF-8 content to a project file; requires approval", {"path": {"type": "string"}, "content": {"type": "string"}}),
        _function(
            "edit_file",
            "Replace one exact text occurrence in an existing UTF-8 project file; requires approval",
            {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
        ),
        _function("git_status", "Inspect the current Git working tree status", {}),
        _function(
            "git_diff",
            "Inspect working-tree or staged Git changes; untracked files are listed without contents",
            {
                "staged": {"type": "boolean"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            required=[],
        ),
        _function(
            "git_stage",
            "Stage explicitly selected VMode-created task changes; requires approval",
            {"paths": {"type": "array", "items": {"type": "string"}}},
        ),
        _function(
            "git_commit",
            "Commit only VMode-verified staged changes; requires approval",
            {"message": {"type": "string"}},
            required=[],
        ),
        _function("run_command", "Run an approved command using argv in the project directory", {"argv": {"type": "array", "items": {"type": "string"}}}),
    ]


def _function(name: str, description: str, properties: dict,
              required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}}}


class ToolRegistry:
    def __init__(self, workspace: Path, approve: Approval | None = None,
                 output_limit: int = 100_000, command_timeout: float = 60.0,
                 search_timeout: float = 10.0, rg_executable: str = "rg"):
        self.root = workspace.resolve()
        self.approve = approve
        self.output_limit = output_limit
        self.command_timeout = command_timeout
        self.search_timeout = search_timeout
        self.rg_executable = rg_executable
        self._ignore_patterns: tuple[str, ...] | None = None
        self._git_baseline: dict[str, dict[str, Any]] | None = None
        self._git_task_contents: dict[str, str] = {}
        self._git_verified_paths: set[str] = set()

    async def begin_task(self) -> None:
        self._git_baseline = None
        self._git_task_contents.clear()
        self._git_verified_paths.clear()
        try:
            status = await self._git_status()
        except ToolError:
            return
        self._git_baseline = {entry["path"]: entry for entry in status["entries"]}

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
            dirs[:] = sorted(
                name for name in dirs
                if name not in EXCLUDED
                and not (base / name).is_symlink()
                and not self._ignored(base / name)
            )
            for name in sorted(files):
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
            "search_files": ({"query"}, {"query", "literal"}),
            "write_file": ({"path", "content"}, {"path", "content"}),
            "edit_file": (
                {"path", "old_text", "new_text"},
                {"path", "old_text", "new_text"},
            ),
            "git_status": (set(), set()),
            "git_diff": (set(), {"staged", "paths"}),
            "git_stage": ({"paths"}, {"paths"}),
            "git_commit": (set(), {"message"}),
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
            return ToolResult(await self._list_files(pattern))
        if name == "search_files":
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                raise ToolError("query must be a non-empty string")
            literal = arguments.get("literal", False)
            if type(literal) is not bool:
                raise ToolError("literal must be a boolean")
            return ToolResult(await self._search_files(query, literal))
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
            await self._record_git_task_path(path, content)
            return ToolResult(f"Wrote {path.relative_to(self.root)}")
        if name == "edit_file":
            path = self._path(arguments.get("path"))
            old_text, new_text = arguments.get("old_text"), arguments.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                raise ToolError("old_text must be a non-empty string")
            if not isinstance(new_text, str):
                raise ToolError("new_text must be a string")
            if self._ignored(path):
                raise ToolError("refusing to modify a .gitignore-excluded file")
            try:
                with path.open("rb") as stream:
                    data = stream.read(self.output_limit + 1)
            except FileNotFoundError as exc:
                raise ToolError("edit_file requires an existing file") from exc
            except OSError as exc:
                raise ToolError(f"cannot read file for edit: {exc.strerror}") from exc
            if len(data) > self.output_limit or b"\0" in data:
                raise ToolError("file is binary or exceeds the output limit")
            try:
                original = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolError("file is not valid UTF-8") from exc
            occurrences = original.count(old_text)
            if occurrences != 1:
                raise ToolError(
                    f"old_text must occur exactly once in the file; found {occurrences}"
                )
            updated = original.replace(old_text, new_text, 1)
            if len(updated.encode()) > self.output_limit:
                raise ToolError("edit exceeds output limit")
            if not await self._approval(name, arguments):
                return ToolResult("Operation denied by user.", approved=False)
            try:
                current = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ToolError(f"cannot verify file before edit: {exc.strerror}") from exc
            if current != original:
                raise ToolError("file changed while awaiting approval; edit was not applied")
            path.write_text(updated, encoding="utf-8")
            await self._record_git_task_path(path, updated)
            return ToolResult(f"Edited {path.relative_to(self.root)}")
        if name == "git_status":
            return ToolResult(self._json_result(await self._git_status()))
        if name == "git_diff":
            staged = arguments.get("staged", False)
            if type(staged) is not bool:
                raise ToolError("staged must be a boolean")
            paths = self._git_paths(arguments.get("paths", []))
            return ToolResult(await self._git_diff(staged, paths))
        if name == "git_stage":
            paths = self._git_paths(arguments.get("paths"))
            return await self._git_stage(paths, arguments)
        if name == "git_commit":
            message = arguments.get("message")
            if message is not None and (not isinstance(message, str) or not message.strip()):
                raise ToolError("message must be a non-empty string when provided")
            return await self._git_commit(message, arguments)
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

    async def _list_files(self, pattern: str) -> str:
        completed = await self._run_rg(["--files", "--null", *self._rg_scope_flags()])
        stdout, stderr, returncode, output_truncated = completed
        if returncode not in {0, 1}:
            raise ToolError(self._rg_error("file discovery", returncode, stderr))
        paths = []
        truncated = output_truncated
        values = stdout.decode(errors="replace").split("\0")
        if output_truncated and values[-1]:
            values.pop()
        for value in values:
            if not value:
                continue
            if fnmatch.fnmatch(value, pattern):
                paths.append(value)
                if len(paths) >= MAX_DISCOVERED_FILES:
                    truncated = True
                    break
        return self._structured_result("paths", paths, "rg", truncated)

    async def _search_files(self, query: str, literal: bool) -> str:
        if "\n" in query or "\r" in query:
            raise ToolError("search query must be a single line")
        args = [
            "--json",
            "--smart-case",
            "--hidden",
            "--max-filesize",
            str(MAX_SEARCH_FILE_BYTES),
            *self._rg_scope_flags(),
        ]
        if literal:
            args.append("--fixed-strings")
        args.extend(("--", query))
        completed = await self._run_rg(args)
        stdout, stderr, returncode, output_truncated = completed
        if returncode == 1:
            return self._structured_result("matches", [], "rg", output_truncated)
        if returncode != 0:
            message = stderr.decode(errors="replace").strip()
            if "No files were searched" in message:
                return self._structured_result("matches", [], "rg", output_truncated)
            if "regex" in message.lower():
                raise ToolError(f"invalid search regex: {message}")
            raise ToolError(self._rg_error("search", returncode, stderr))
        matches = []
        truncated = output_truncated
        lines = stdout.decode(errors="replace").splitlines()
        if output_truncated and stdout and not stdout.endswith(b"\n"):
            lines.pop()
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ToolError("ripgrep returned malformed search output") from exc
            if event.get("type") != "match":
                continue
            data = event.get("data", {})
            path = data.get("path", {}).get("text")
            line_number = data.get("line_number")
            text = data.get("lines", {}).get("text")
            if not isinstance(path, str) or type(line_number) is not int or not isinstance(text, str):
                raise ToolError("ripgrep returned malformed search match")
            matches.append(
                {"path": path, "line": line_number, "text": text.rstrip("\r\n")}
            )
            if len(matches) >= MAX_SEARCH_MATCHES:
                truncated = True
                break
        return self._structured_result("matches", matches, "rg", truncated)

    def _rg_scope_flags(self) -> list[str]:
        flags = ["--hidden"]
        for directory in sorted(EXCLUDED):
            flags.extend(
                (
                    "--glob",
                    f"!{directory}/**",
                    "--glob",
                    f"!**/{directory}/**",
                )
            )
        return flags

    async def _run_rg(
        self, arguments: list[str]
    ) -> tuple[bytes, bytes, int, bool]:
        try:
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "RIPGREP_CONFIG_PATH": os.devnull,
            }
            environment.update(
                {
                    key: value
                    for key in ("LANG", "LC_ALL")
                    if (value := os.environ.get(key))
                }
            )
            process = await asyncio.create_subprocess_exec(
                self.rg_executable,
                *arguments,
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError:
            raise ToolError(
                "ripgrep (rg) is required for workspace discovery and search; "
                "install it and ensure it is on PATH"
            ) from None
        except OSError as exc:
            raise ToolError(f"cannot start ripgrep: {exc.strerror}") from exc

        async def read_limited(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
            output = bytearray()
            truncated = False
            while chunk := await stream.read(8192):
                remaining = self.output_limit - len(output)
                output.extend(chunk[:max(0, remaining)])
                truncated = truncated or len(chunk) > remaining
            return bytes(output), truncated

        try:
            (stdout, stdout_truncated), (stderr, stderr_truncated), _ = (
                await asyncio.wait_for(
                    asyncio.gather(
                        read_limited(process.stdout),
                        read_limited(process.stderr),
                        process.wait(),
                    ),
                    self.search_timeout,
                )
            )
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.terminate()
            await process.wait()
            raise ToolError("ripgrep search timed out")
        return stdout, stderr, process.returncode, stdout_truncated or stderr_truncated

    def _structured_result(
        self,
        field: str,
        values: list[Any],
        backend: str,
        truncated: bool,
    ) -> str:
        while True:
            payload: dict[str, Any] = {
                field: values,
                "truncated": truncated,
                "backend": backend,
            }
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(content) <= self.output_limit or not values:
                return content
            values.pop()
            truncated = True

    @staticmethod
    def _rg_error(operation: str, returncode: int, stderr: bytes) -> str:
        detail = stderr.decode(errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        return f"ripgrep {operation} failed with exit code {returncode}{suffix}"

    def _git_paths(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(path, str) for path in value):
            raise ToolError("paths must be a list of strings")
        paths: list[str] = []
        for value_path in value:
            if not value_path or value_path == "." or value_path.startswith(":") or any(
                character in value_path for character in "*?[]"
            ):
                raise ToolError("paths must be explicit workspace-relative file paths")
            path = self._path(value_path)
            relative = path.relative_to(self.root).as_posix()
            if relative not in paths:
                paths.append(relative)
        return paths

    async def _run_git(self, arguments: list[str]) -> tuple[bytes, bytes, int, bool]:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *arguments,
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ToolError("git is required for Git tools and must be on PATH") from None
        except OSError as exc:
            raise ToolError(f"cannot start git: {exc.strerror}") from exc

        async def read_limited(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
            output = bytearray()
            truncated = False
            while chunk := await stream.read(8192):
                remaining = self.output_limit - len(output)
                output.extend(chunk[:max(0, remaining)])
                truncated = truncated or len(chunk) > remaining
            return bytes(output), truncated

        try:
            (stdout, stdout_truncated), (stderr, stderr_truncated), _ = (
                await asyncio.wait_for(
                    asyncio.gather(
                        read_limited(process.stdout),
                        read_limited(process.stderr),
                        process.wait(),
                    ),
                    self.command_timeout,
                )
            )
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.terminate()
            await process.wait()
            raise ToolError("git operation timed out")
        return stdout, stderr, process.returncode, stdout_truncated or stderr_truncated

    async def _ensure_git_repository(self) -> None:
        stdout, _, returncode, truncated = await self._run_git(
            ["rev-parse", "--is-inside-work-tree"]
        )
        if returncode != 0 or truncated or stdout.strip() != b"true":
            raise ToolError("workspace is not inside a Git working tree")

    async def _git_status(self) -> dict[str, Any]:
        await self._ensure_git_repository()
        stdout, stderr, returncode, truncated = await self._run_git(
            [
                "-c",
                "status.relativePaths=true",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ]
        )
        if returncode != 0 or truncated:
            raise ToolError(self._git_error("status", returncode, stderr, truncated))
        records = stdout.split(b"\0")
        entries: list[dict[str, Any]] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 3 or record[2:3] != b" ":
                raise ToolError("git returned malformed porcelain status")
            code = record[:2].decode("ascii", errors="replace")
            path = record[3:].decode("utf-8", errors="surrogateescape")
            entry: dict[str, Any] = {
                "path": path,
                "index": code[0],
                "worktree": code[1],
                "staged": code[0] not in {" ", "?"},
                "unstaged": code[1] != " ",
                "untracked": code == "??",
                "conflicted": "U" in code or code in {"AA", "DD"},
                "renamed": "R" in code,
                "copied": "C" in code,
            }
            if "R" in code or "C" in code:
                if index >= len(records) or not records[index]:
                    raise ToolError("git returned incomplete rename status")
                entry["original_path"] = records[index].decode(
                    "utf-8", errors="surrogateescape"
                )
                index += 1
            entries.append(entry)
        branch_stdout, _, branch_code, _ = await self._run_git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"]
        )
        branch = (
            branch_stdout.decode("utf-8", errors="replace").strip()
            if branch_code == 0
            else None
        )
        return {"branch": branch, "entries": entries, "truncated": False}

    async def _git_diff(self, staged: bool, paths: list[str]) -> str:
        status = await self._git_status()
        args = ["diff", "--no-ext-diff"]
        if staged:
            args.append("--cached")
        if paths:
            args.extend(("--", *paths))
        stdout, stderr, returncode, truncated = await self._run_git(args)
        if returncode != 0:
            raise ToolError(self._git_error("diff", returncode, stderr, truncated))
        untracked = [
            entry["path"]
            for entry in status["entries"]
            if entry["untracked"] and (not paths or entry["path"] in paths)
        ]
        text = stdout.decode("utf-8", errors="replace")
        marker = "\n[diff truncated]" if truncated else ""
        payload = {
            "staged": staged,
            "paths": paths,
            "diff": text + marker,
            "untracked": untracked,
            "truncated": truncated,
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(content) > self.output_limit:
            payload["diff"] = text[: max(0, self.output_limit // 2)] + "\n[diff truncated]"
            payload["truncated"] = True
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return content

    async def _record_git_task_path(self, path: Path, content: str) -> None:
        if self._git_baseline is None:
            return
        relative = path.relative_to(self.root).as_posix()
        if relative not in self._git_baseline:
            self._git_task_contents[relative] = content

    async def _git_stage(
        self, paths: list[str], arguments: dict[str, Any]
    ) -> ToolResult:
        if not paths:
            raise ToolError("git_stage requires at least one explicit path")
        if self._git_baseline is None:
            raise ToolError("git_stage requires a task Git baseline")
        status = await self._git_status()
        current = {entry["path"]: entry for entry in status["entries"]}
        self._validate_git_stage_paths(paths, current)
        if not await self._approval("git_stage", arguments):
            return ToolResult("Operation denied by user.", approved=False)
        status = await self._git_status()
        current = {entry["path"]: entry for entry in status["entries"]}
        self._validate_git_stage_paths(paths, current)
        _, stderr, returncode, truncated = await self._run_git(["add", "--", *paths])
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").lower()
            if "ignored" in detail:
                raise ToolError("git refused to stage an ignored path")
            raise ToolError(self._git_error("stage", returncode, stderr, truncated))
        staged = await self._git_status()
        staged_paths = {
            entry["path"] for entry in staged["entries"] if entry["staged"]
        }
        if not set(paths).issubset(staged_paths):
            raise ToolError("git did not stage every approved path")
        self._git_verified_paths.update(paths)
        return ToolResult(
            self._json_result(
                {
                    "staged_paths": sorted(self._git_verified_paths),
                    "branch": staged["branch"],
                    "entries": staged["entries"],
                }
            )
        )

    def _validate_git_stage_paths(
        self, paths: list[str], current: dict[str, dict[str, Any]]
    ) -> None:
        for path in paths:
            if path in self._git_baseline:
                raise ToolError(f"{path} had baseline user changes and will not be staged")
            if path not in self._git_task_contents:
                raise ToolError(f"{path} was not modified by VMode in this task")
            entry = current.get(path)
            if entry is None:
                raise ToolError(f"{path} has no current Git change to stage")
            if entry["conflicted"]:
                raise ToolError(f"{path} has unresolved merge conflicts")
            candidate = self.root / path
            if candidate.exists() and candidate.read_text(encoding="utf-8") != self._git_task_contents[path]:
                raise ToolError(f"{path} changed after VMode wrote it and will not be staged")

    async def _git_commit(
        self, message: str | None, arguments: dict[str, Any]
    ) -> ToolResult:
        status = await self._git_status()
        staged_paths = {entry["path"] for entry in status["entries"] if entry["staged"]}
        if not staged_paths:
            raise ToolError("git_commit requires non-empty staged changes")
        if staged_paths != self._git_verified_paths:
            raise ToolError("staged changes differ from VMode-verified staging plan")
        if any(entry["conflicted"] for entry in status["entries"]):
            raise ToolError("git_commit refuses unresolved merge conflicts")
        diff, _, returncode, truncated = await self._run_git(
            ["diff", "--cached", "--no-ext-diff"]
        )
        if returncode != 0 or truncated or not diff:
            raise ToolError("git_commit requires a bounded non-empty staged diff")
        trailer = "Co-authored-by: Velocity-Mode <velocity-mode@users.noreply.github.com>"
        lines = (message or "chore: update staged files").strip().splitlines()
        lines = [line for line in lines if line.lower() != trailer.lower()]
        commit_message = "\n".join(lines).rstrip() + "\n\n" + trailer
        approval_args = {
            **arguments,
            "message": commit_message,
            "paths": sorted(staged_paths),
        }
        fingerprint = hashlib.sha256(diff).digest()
        if not await self._approval("git_commit", approval_args):
            return ToolResult("Operation denied by user.", approved=False)
        current_diff, _, current_code, current_truncated = await self._run_git(
            ["diff", "--cached", "--no-ext-diff"]
        )
        current_status = await self._git_status()
        current_paths = {
            entry["path"] for entry in current_status["entries"] if entry["staged"]
        }
        if (
            current_code != 0
            or current_truncated
            or hashlib.sha256(current_diff).digest() != fingerprint
            or current_paths != staged_paths
        ):
            raise ToolError("staged changes changed while awaiting approval")
        _, stderr, returncode, truncated = await self._run_git(
            ["commit", "-m", commit_message]
        )
        if returncode != 0:
            raise ToolError(self._git_error("commit", returncode, stderr, truncated))
        head, _, head_code, _ = await self._run_git(["rev-parse", "HEAD"])
        if head_code != 0:
            raise ToolError("commit succeeded but Git could not report HEAD")
        self._git_verified_paths.clear()
        return ToolResult(
            self._json_result(
                {
                    "commit": head.decode("ascii", errors="replace").strip(),
                    "branch": (final_status := await self._git_status())["branch"],
                    "entries": final_status["entries"],
                }
            )
        )

    def _json_result(self, payload: dict[str, Any]) -> str:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(content) <= self.output_limit:
            return content
        if isinstance(payload.get("entries"), list):
            payload = {**payload, "entries": list(payload["entries"]), "truncated": True}
            while payload["entries"]:
                payload["entries"].pop()
                content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if len(content) <= self.output_limit:
                    return content
        return content[: self.output_limit] + "\n[output truncated]"

    @staticmethod
    def _git_error(operation: str, returncode: int, stderr: bytes, truncated: bool) -> str:
        detail = stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        if truncated:
            suffix += " [output truncated]"
        return f"git {operation} failed with exit code {returncode}{suffix}"

    async def _approval(self, name: str, arguments: dict[str, Any]) -> bool:
        if self.approve is None:
            return False
        return await self.approve(name, arguments)
