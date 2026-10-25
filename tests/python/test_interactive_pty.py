import base64
import os
import pty
import re
import select
import sys
import time
from pathlib import Path

from vmode.sessions import SessionStore


def test_normal_motions_and_response_copy_in_terminal(tmp_path: Path):
    state_home = tmp_path / "state"
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update(
            {
                "TERM": "xterm-256color",
                "VMODE_CONFIG": str(tmp_path / "missing.toml"),
                "XDG_STATE_HOME": str(state_home),
            }
        )
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "vmode",
                "--provider",
                "mock",
                "--workspace",
                str(tmp_path),
                "abc\ndef",
            ],
            env,
        )

    output = bytearray()

    def wait_for(marker: bytes, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                output.extend(os.read(fd, 8192))
            except OSError:
                return False
            if marker in output:
                return True
        return False

    def wait_for_response_count(count: int, timeout: float = 10.0) -> bool:
        store = SessionStore(tmp_path, state_home)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            session_id = store.latest_id()
            if session_id is not None:
                try:
                    record = store.load(session_id)
                except ValueError:
                    time.sleep(0.05)
                    continue
                if len(record["ui"].get("responses", [])) >= count:
                    return True
            time.sleep(0.05)
        return False

    status = None
    try:
        assert wait_for(b"INSERT")
        assert str(tmp_path).encode() in output
        output.clear()
        os.write(fd, b"\x1b")
        assert wait_for(b"NORMAL")

        output.clear()
        os.write(fd, b"hxklxj^$x\x13")
        assert wait_for(b"Turn complete"), bytes(output)

        output.clear()
        os.write(fd, b"\t/not-found\r")
        assert wait_for(b"No match for 'not-found'"), bytes(output)
        output.clear()
        os.write(fd, b"/Mock\rnN")
        time.sleep(0.1)
        output.clear()
        os.write(fd, b"vhy")
        assert wait_for(b"\x1b]52;c;")
        match = re.search(rb"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", output)
        assert match is not None
        visual_copy = base64.b64decode(match.group(1)).decode("utf-8")
        assert visual_copy == " M"

        output.clear()
        os.write(fd, b"y")
        assert wait_for(b"\x1b]52;c;")
        match = re.search(rb"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", output)
        assert match is not None
        first_register = base64.b64decode(match.group(1)).decode("utf-8")
        assert "Mock response: ab\nd" in first_register
        assert first_register.startswith(" M\n\n")

        output.clear()
        os.write(fd, b"\t")
        time.sleep(0.1)
        os.write(fd, b"i")
        assert wait_for(b"INSERT")
        os.write(fd, b" second task")
        assert wait_for(b"second task")
        os.write(fd, b"\x1b")
        assert wait_for(b"NORMAL")
        os.write(fd, b"\x13")
        assert wait_for_response_count(2), bytes(output)
        output.clear()
        os.write(fd, b"\t")
        time.sleep(0.1)
        os.write(fd, b"y")
        time.sleep(0.1)
        os.write(fd, b"[")
        assert wait_for(b"target: 1/2"), bytes(output)
        output.clear()
        os.write(fd, b"y")
        assert wait_for(b"Yanked response 1."), bytes(output)
        match = re.search(rb"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", output)
        assert match is not None
        copied = base64.b64decode(match.group(1)).decode("utf-8")
        assert "Mock response: ab\nd" in copied
        output.clear()
        os.write(fd, b"]")
        assert wait_for(b"target: 2/2"), bytes(output)
        output.clear()
        os.write(fd, b"y")
        assert wait_for(b"Yanked response 2."), bytes(output)
        match = re.search(rb"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", output)
        assert match is not None
        copied = base64.b64decode(match.group(1)).decode("utf-8")
        assert "Mock response: second task" in copied
        output.clear()
        os.write(fd, b"[")
        time.sleep(0.1)
        os.write(fd, b"y")
        assert wait_for(b"\x1b]52;c;")
        match = re.search(rb"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", output)
        assert match is not None
        accumulated = base64.b64decode(match.group(1)).decode("utf-8")
        assert "Mock response: second task" in accumulated
        assert "Mock response: ab\nd" in accumulated
        assert accumulated.count("Mock response:") == 2

        output.clear()
        os.write(fd, b"\x04")
        time.sleep(0.2)
        assert b"\x1b[?1049l" not in output

        output.clear()
        os.write(fd, b":q\r")
        assert wait_for(b"\x1b[?1049l")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                break
            time.sleep(0.05)
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_tui_remains_interactive_while_agent_task_runs(tmp_path: Path):
    state_home = tmp_path / "state"
    code = """
import asyncio
import sys
from vmode.providers import MockProvider

original_stream = MockProvider.stream

async def delayed_stream(self, messages, tools):
    await asyncio.sleep(2.0)
    async for event in original_stream(self, messages, tools):
        yield event

MockProvider.stream = delayed_stream
from vmode.cli import main
raise SystemExit(main([
    "--provider", "mock", "--workspace", sys.argv[1], "first task"
]))
"""
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update(
            {
                "TERM": "xterm-256color",
                "VMODE_CONFIG": str(tmp_path / "missing.toml"),
                "XDG_STATE_HOME": str(state_home),
            }
        )
        os.execve(sys.executable, [sys.executable, "-c", code, str(tmp_path)], env)

    output = bytearray()

    def wait_for(marker: bytes, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                output.extend(os.read(fd, 8192))
            except OSError:
                return False
            if marker in output:
                return True
        return False

    def wait_for_responses(count: int, timeout: float = 10.0) -> bool:
        store = SessionStore(tmp_path, state_home)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            session_id = store.latest_id()
            if session_id is not None:
                try:
                    record = store.load(session_id)
                except ValueError:
                    time.sleep(0.05)
                    continue
                if len(record["ui"].get("responses", [])) >= count:
                    return True
            time.sleep(0.05)
        return False

    status = None
    try:
        assert wait_for(b"INSERT")
        output.clear()
        os.write(fd, b"\x13")
        assert wait_for(b"Requesting mock/mock/deterministic"), bytes(output)

        output.clear()
        os.write(fd, b"\x1b:status\r")
        assert wait_for(b"Provider mock/mock/deterministic"), bytes(output)

        output.clear()
        os.write(fd, b"ifollow-up task\x13")
        assert wait_for(b"your draft was kept"), bytes(output)

        assert wait_for(b"Turn complete"), bytes(output)

        output.clear()
        os.write(fd, b"\x1b\x13")
        assert wait_for_responses(2), bytes(output)

        output.clear()
        os.write(fd, b":q\r")
        assert wait_for(b"\x1b[?1049l")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                break
            time.sleep(0.05)
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_insert_mode_command_and_file_completion_are_visible(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "src" / "a").write_text("valid referenced file\n", encoding="utf-8")
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update({"TERM": "xterm-256color", "VMODE_CONFIG": str(tmp_path / "missing.toml")})
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "vmode",
                "--provider",
                "mock",
                "--workspace",
                str(tmp_path),
            ],
            env,
        )

    output = bytearray()

    def wait_for(marker: bytes, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                output.extend(os.read(fd, 8192))
            except OSError:
                return False
            if marker in output:
                return True
        return False

    status = None
    try:
        assert wait_for(b"INSERT")
        output.clear()
        os.write(fd, b"\x1b")
        assert wait_for(b"NORMAL")
        output.clear()
        os.write(fd, b"/sta")
        assert wait_for(b"/status")

        output.clear()
        os.write(fd, b"\r\r")
        assert wait_for(b"Provider mock")

        output.clear()
        os.write(fd, b"\x1b")
        time.sleep(0.2)
        os.write(fd, b"i@src/a")
        assert wait_for(b"@src/app.py")

        output.clear()
        os.write(fd, b"\x1b\x1b")
        time.sleep(0.2)
        os.write(fd, b":w should-not-exist.txt\r")
        assert wait_for(b"No completed result to save")
        assert not (tmp_path / "should-not-exist.txt").exists()
        output.clear()
        os.write(fd, b"\x13")
        assert wait_for(b"Turn complete"), bytes(output[-2500:])
        output.clear()
        os.write(fd, b":q\r")
        assert wait_for(b"\x1b[?1049l")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                break
            time.sleep(0.05)
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_result_save_command_writes_selected_result(tmp_path: Path):
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update({"TERM": "xterm-256color", "VMODE_CONFIG": str(tmp_path / "missing.toml")})
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "vmode",
                "--provider",
                "mock",
                "--workspace",
                str(tmp_path),
                "create a hello world cli",
            ],
            env,
        )

    output = bytearray()

    def wait_for(marker: bytes, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                output.extend(os.read(fd, 8192))
            except OSError:
                return False
            if marker in output:
                return True
        return False

    status = None
    try:
        assert wait_for(b"INSERT")
        output.clear()
        os.write(fd, b"\x1b\x13")
        assert wait_for(b"Turn complete")
        assert b"You" in output
        assert b"Velocity-Mode" in output

        output.clear()
        os.write(fd, b"\t:w hello.txt\r")
        assert wait_for(b"Saved result 1 to hello.txt")
        assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == (
            "Mock response: create a hello world cli"
        )

        output.clear()
        os.write(fd, b":q\r")
        assert wait_for(b"\x1b[?1049l")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                break
            time.sleep(0.05)
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_partial_response_remains_available_after_iteration_limit(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("test file\n", encoding="utf-8")
    state_home = tmp_path / "state"
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update(
            {
                "TERM": "xterm-256color",
                "VMODE_CONFIG": str(tmp_path / "missing.toml"),
                "XDG_STATE_HOME": str(state_home),
            }
        )
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "vmode",
                "--provider",
                "mock",
                "--mock-scenario",
                "read-file",
                "--max-iterations",
                "1",
                "--workspace",
                str(workspace),
                "Inspect @README.md",
            ],
            env,
        )

    output = bytearray()

    def wait_for(marker: bytes, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                output.extend(os.read(fd, 8192))
            except OSError:
                return False
            if marker in output:
                return True
        return False

    status = None
    try:
        assert wait_for(b"INSERT")
        output.clear()
        os.write(fd, b"\x1b")
        assert wait_for(b"NORMAL")
        os.write(fd, b"\x13")
        assert wait_for(b"Agent iteration limit reached"), bytes(output)

        output.clear()
        os.write(fd, b"\x1b:w partial.txt\r")
        assert wait_for(b"Saved result 1 to partial.txt"), bytes(output)
        assert (workspace / "partial.txt").read_text(encoding="utf-8") == (
            "I’ll inspect the requested file."
        )

        output.clear()
        os.write(fd, b":q\r")
        assert wait_for(b"\x1b[?1049l")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                break
            time.sleep(0.05)
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_session_is_saved_and_can_be_resumed(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_home = tmp_path / "state"
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update(
            {
                "TERM": "xterm-256color",
                "VMODE_CONFIG": str(tmp_path / "missing.toml"),
                "XDG_STATE_HOME": str(state_home),
            }
        )
        os.execve(
            sys.executable,
            [sys.executable, "-m", "vmode", "--provider", "mock", "--workspace", str(workspace)],
            env,
        )

    output = bytearray()

    def wait_for(fd: int, marker: bytes, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                output.extend(os.read(fd, 8192))
            except OSError:
                return False
            if marker in output:
                return True
        return False

    status = None
    try:
        assert wait_for(fd, b"INSERT")
        output.clear()
        os.write(fd, b"resume this conversation\x13")
        assert wait_for(fd, b"Turn complete")
        session_id = SessionStore(workspace, state_home).latest_id()
        assert session_id is not None
        output.clear()
        os.write(fd, b"\x04")
        assert wait_for(fd, b"\x1b[?1049l")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                break
            time.sleep(0.05)
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update(
            {
                "TERM": "xterm-256color",
                "VMODE_CONFIG": str(tmp_path / "missing.toml"),
                "XDG_STATE_HOME": str(state_home),
            }
        )
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "vmode",
                "--provider",
                "mock",
                "--workspace",
                str(workspace),
                "--resume",
                session_id,
            ],
            env,
        )
    output.clear()
    status = None
    try:
        assert wait_for(fd, b"resume this conversation")
        assert wait_for(fd, b"Resuming session")
        output.clear()
        os.write(fd, b"\x04")
        assert wait_for(fd, b"\x1b[?1049l")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child, child_status = os.waitpid(pid, os.WNOHANG)
            if child:
                status = child_status
                break
            time.sleep(0.05)
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
