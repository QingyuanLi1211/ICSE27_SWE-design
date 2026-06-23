"""以子进程方式调用 Claude Code CLI，并落盘 prompt/stdout/stderr/事件流。

Claude Code 的非交互模式使用 `claude -p`。本 runner 只负责启动 CLI
和保存原生输出，不从 stdout/stderr/trajectory 中提取 patch，也不会把
agent 输出的文本 patch 回放到 worktree。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread


@dataclass(slots=True)
class RunnerSettings:
    command: list[str]
    timeout_seconds: int = 1800
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RunnerOutput:
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    events_path: Path
    command_path: Path
    timed_out: bool = False
    saw_terminal_success: bool = False
    completion_source: str = "process"
    terminated_after_terminal_success: bool = False
    rate_limited: bool = False
    rate_limit_reason: str | None = None


class SubprocessClaudeCodeRunner:
    """按给定参数启动 Claude Code CLI。"""

    TERMINAL_SUCCESS_GRACE_SECONDS = 3.0
    POLL_INTERVAL_SECONDS = 0.2

    def __init__(self, settings: RunnerSettings) -> None:
        self.settings = settings

    def run(
        self,
        prompt: str,
        *,
        workdir: Path,
        attempt_dir: Path,
        timeout_seconds: int | None = None,
    ) -> RunnerOutput:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = attempt_dir / "prompt.md"
        stdout_path = attempt_dir / "runner_stdout.txt"
        stderr_path = attempt_dir / "runner_stderr.txt"
        events_path = attempt_dir / "runner_events.jsonl"
        command_path = attempt_dir / "runner_command.txt"
        system_prompt_path = (attempt_dir / "system_append.md").resolve()
        prompt_path.write_text(prompt, encoding="utf-8")
        system_prompt_path.write_text(CLAUDE_CODE_APPEND_SYSTEM_PROMPT, encoding="utf-8")

        command = self._materialize_command(
            prompt=prompt,
            workdir=workdir,
            system_prompt_path=system_prompt_path,
        )
        command_path.write_text("\n".join(command), encoding="utf-8")
        env = {**os.environ, **self.settings.env}
        effective_timeout = timeout_seconds if timeout_seconds is not None else self.settings.timeout_seconds

        (
            stdout,
            stderr,
            returncode,
            timed_out,
            saw_terminal_success,
            completion_source,
            terminated_after_terminal_success,
            rate_limited,
            rate_limit_reason,
        ) = self._run_streaming(
            command=command,
            workdir=workdir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            events_path=events_path,
            timeout_seconds=effective_timeout,
            env=env,
        )
        return RunnerOutput(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            events_path=events_path,
            command_path=command_path,
            timed_out=timed_out,
            saw_terminal_success=saw_terminal_success,
            completion_source=completion_source,
            terminated_after_terminal_success=terminated_after_terminal_success,
            rate_limited=rate_limited,
            rate_limit_reason=rate_limit_reason,
        )

    def _materialize_command(
        self,
        *,
        prompt: str,
        workdir: Path,
        system_prompt_path: Path,
    ) -> list[str]:
        command: list[str] = []
        for part in self.settings.command:
            command.append(
                part.replace("__PROMPT__", prompt)
                .replace("__WORKDIR__", str(workdir))
                .replace("__APPEND_SYSTEM_PROMPT_FILE__", str(system_prompt_path))
            )
        return command

    def _run_streaming(
        self,
        *,
        command: list[str],
        workdir: Path,
        stdout_path: Path,
        stderr_path: Path,
        events_path: Path,
        timeout_seconds: int,
        env: dict[str, str],
    ) -> tuple[str, str, int | None, bool, bool, str, bool, bool, str | None]:
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        terminal_success_event = Event()
        rate_limit_event = Event()
        rate_limit_reason: list[str] = []
        with (
            stdout_path.open("w", encoding="utf-8", buffering=1) as stdout_handle,
            stderr_path.open("w", encoding="utf-8", buffering=1) as stderr_handle,
            (
                events_path.open("w", encoding="utf-8", buffering=1)
                if events_path is not None
                else nullcontext(None)
            ) as events_handle,
        ):
            process = subprocess.Popen(
                command,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                start_new_session=(os.name != "nt"),
            )
            assert process.stdout is not None
            assert process.stderr is not None

            stdout_thread = Thread(
                target=self._copy_stream,
                args=(
                    process.stdout,
                    stdout_handle,
                    stdout_chunks,
                    events_handle,
                    terminal_success_event,
                    rate_limit_event,
                    rate_limit_reason,
                ),
                daemon=True,
            )
            stderr_thread = Thread(
                target=self._copy_stream,
                args=(process.stderr, stderr_handle, stderr_chunks, None, None, rate_limit_event, rate_limit_reason),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            start_time = time.monotonic()
            timed_out = False
            saw_terminal_success = False
            completion_source = "process"
            terminated_after_terminal_success = False
            rate_limited = False
            returncode: int | None = None
            try:
                while True:
                    returncode = process.poll()
                    if rate_limit_event.is_set():
                        rate_limited = True
                        completion_source = "rate_limit"
                        if returncode is None:
                            self._terminate_process(process)
                            returncode = process.poll()
                        break
                    if terminal_success_event.is_set():
                        saw_terminal_success = True
                        completion_source = "event_success"
                        deadline = time.monotonic() + self.TERMINAL_SUCCESS_GRACE_SECONDS
                        while time.monotonic() < deadline:
                            returncode = process.poll()
                            if returncode is not None:
                                break
                            time.sleep(self.POLL_INTERVAL_SECONDS)
                        if returncode is None:
                            self._terminate_process(process)
                            terminated_after_terminal_success = True
                            returncode = process.poll()
                        break
                    if returncode is not None:
                        break
                    if time.monotonic() - start_time >= timeout_seconds:
                        self._terminate_process(process)
                        returncode = None
                        timed_out = True
                        completion_source = "timeout"
                        break
                    time.sleep(self.POLL_INTERVAL_SECONDS)
            finally:
                stdout_thread.join(timeout=15)
                stderr_thread.join(timeout=15)
                if rate_limit_event.is_set():
                    rate_limited = True
                    completion_source = "rate_limit"
                if terminal_success_event.is_set():
                    saw_terminal_success = True
                    if completion_source == "process":
                        completion_source = "event_success"

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if timed_out:
            stderr += "\nRunner timed out."
            stderr_path.write_text(stderr, encoding="utf-8")
        self._stabilize_after_process_exit(timed_out=timed_out)
        return (
            stdout,
            stderr,
            returncode,
            timed_out,
            saw_terminal_success,
            completion_source,
            terminated_after_terminal_success,
            rate_limited,
            rate_limit_reason[0] if rate_limit_reason else None,
        )

    def _copy_stream(
        self,
        stream,
        sink_handle,
        sink_chunks: list[str],
        tee_handle,
        terminal_success_event: Event | None,
        rate_limit_event: Event | None,
        rate_limit_reason: list[str] | None,
    ) -> None:
        try:
            for chunk in iter(stream.readline, ""):
                sink_chunks.append(chunk)
                sink_handle.write(chunk)
                sink_handle.flush()
                if tee_handle is not None:
                    tee_handle.write(chunk)
                    tee_handle.flush()
                if terminal_success_event is not None and self._is_terminal_success_event(chunk):
                    terminal_success_event.set()
                if rate_limit_event is not None:
                    reason = self._rate_limit_reason(chunk)
                    if reason is not None:
                        if rate_limit_reason is not None and not rate_limit_reason:
                            rate_limit_reason.append(reason)
                        rate_limit_event.set()
        finally:
            stream.close()

    def _is_terminal_success_event(self, chunk: str) -> bool:
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            return False
        return (
            payload.get("type") == "result"
            and payload.get("subtype") == "success"
            and payload.get("is_error") is False
        )

    def _rate_limit_reason(self, chunk: str) -> str | None:
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            lowered = chunk.lower()
            if "you've hit your limit" in lowered or "you have hit your limit" in lowered:
                return chunk.strip()
            if "429" in lowered and ("rate limit" in lowered or "rate_limit" in lowered):
                return chunk.strip()
            return None

        if payload.get("type") == "rate_limit_event":
            info = payload.get("rate_limit_info") or {}
            status = info.get("status")
            if status == "rejected":
                return f"rate_limit_event status=rejected resetsAt={info.get('resetsAt')}"
        if payload.get("api_error_status") == 429:
            return str(payload.get("result") or payload.get("error") or "api_error_status=429")
        if payload.get("error") == "rate_limit":
            return str(payload.get("result") or "error=rate_limit")
        return None

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _stabilize_after_process_exit(self, *, timed_out: bool) -> None:
        if os.name == "nt":
            time.sleep(2.0 if timed_out else 0.5)


CLAUDE_CODE_APPEND_SYSTEM_PROMPT = """\
You are running inside a benchmark harness.

Hard boundaries:
- Work only in the current working directory, which is the repository root.
- Absolutely do not inspect, read, list, search, grep, glob, or otherwise access any file or directory outside the current working directory.
- Do not read parent directories, benchmark directories, output_data directories, hidden test assets, or future repository state.
- Do not use absolute paths that point outside the current working directory.
- Accessing files outside the worktree may be treated as cheating and will invalidate the run.
- Do not run broad filesystem scans outside the repository root such as `find /`, `find C:/`, or equivalent commands.
- Do not use web search, web fetch, browser automation, remote repositories, package downloads, curl, wget, or any command that accesses the network.
- Modify production files directly in the working tree.
- Do not output git patches, diff text, apply_patch blocks, or prose-only edit instructions as a substitute for modifying files.
- If files are not writable or edits fail, stop the repair without producing a textual patch.
- When finished, leave the edits in the working tree and end normally. The outer harness will generate the diff.
"""
