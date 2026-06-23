"""以子进程方式调用 Codex CLI，并落盘 prompt/stdout/stderr/事件流。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from typing import Any


@dataclass(slots=True)
class RunnerSettings:
    command: list[str]
    timeout_seconds: int = 1800
    env: dict[str, str] = field(default_factory=dict)
    transport_retries: int = 2
    transport_retry_backoff_seconds: float = 5.0


@dataclass(slots=True)
class RunnerOutput:
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    last_message_path: Path
    events_path: Path | None
    parsed_json: dict[str, Any] | None
    parse_error: str | None
    timed_out: bool = False


class SubprocessCodexRunner:
    """按给定参数启动 Codex CLI。"""

    def __init__(self, settings: RunnerSettings) -> None:
        self.settings = settings

    def run(
        self,
        prompt: str,
        *,
        workdir: Path,
        attempt_dir: Path,
        timeout_seconds: int | None = None,
        json_output: bool = False,
    ) -> RunnerOutput:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = attempt_dir / "prompt.md"
        stdout_path = attempt_dir / "runner_stdout.txt"
        stderr_path = attempt_dir / "runner_stderr.txt"
        last_message_path = attempt_dir / "runner_last_message.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        env = None
        if self.settings.env:
            env = {**os.environ, **self.settings.env}
        effective_timeout = timeout_seconds if timeout_seconds is not None else self.settings.timeout_seconds
        max_transport_attempts = max(1, self.settings.transport_retries + 1)
        last_output: RunnerOutput | None = None

        for transport_attempt in range(1, max_transport_attempts + 1):
            events_path = attempt_dir / "runner_events.jsonl" if json_output else None
            self._reset_attempt_artifacts(
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                last_message_path=last_message_path,
                events_path=events_path,
            )
            command, stdin_payload = self._materialize_command(
                prompt,
                prompt_path,
                workdir,
                last_message_path,
                json_output=json_output,
            )

            if json_output:
                stdout, stderr, returncode, timed_out = self._run_streaming(
                    command=command,
                    stdin_payload=stdin_payload,
                    workdir=workdir,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    events_path=events_path,
                    timeout_seconds=effective_timeout,
                    env=env,
                )
            else:
                stdout, stderr, returncode, timed_out = self._run_captured(
                    command=command,
                    stdin_payload=stdin_payload,
                    workdir=workdir,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout_seconds=effective_timeout,
                    env=env,
                )

            parse_source = stdout
            if last_message_path.exists():
                last_message = last_message_path.read_text(encoding="utf-8").strip()
                if last_message:
                    parse_source = last_message
            parsed_json, parse_error = extract_json_object(parse_source)
            parse_error = self._refine_parse_error(parse_error=parse_error, stdout=stdout, stderr=stderr)

            if json_output and self._should_retry_without_json(
                parsed_json=parsed_json,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
            ):
                events_path = None
                self._reset_attempt_artifacts(
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    last_message_path=last_message_path,
                    events_path=None,
                )
                command, stdin_payload = self._materialize_command(
                    prompt,
                    prompt_path,
                    workdir,
                    last_message_path,
                    json_output=False,
                )
                stdout, stderr, returncode, timed_out = self._run_captured(
                    command=command,
                    stdin_payload=stdin_payload,
                    workdir=workdir,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout_seconds=effective_timeout,
                    env=env,
                )
                parse_source = stdout
                if last_message_path.exists():
                    last_message = last_message_path.read_text(encoding="utf-8").strip()
                    if last_message:
                        parse_source = last_message
                parsed_json, parse_error = extract_json_object(parse_source)
                parse_error = self._refine_parse_error(parse_error=parse_error, stdout=stdout, stderr=stderr)

            last_output = RunnerOutput(
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                prompt_path=prompt_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                last_message_path=last_message_path,
                events_path=events_path,
                parsed_json=parsed_json,
                parse_error=parse_error,
                timed_out=timed_out,
            )
            if not self._should_retry_transport(last_output):
                return last_output
            if transport_attempt >= max_transport_attempts:
                return last_output
            self._stabilize_between_transport_retries(transport_attempt=transport_attempt)

        assert last_output is not None
        return last_output

    def _reset_attempt_artifacts(
        self,
        *,
        stdout_path: Path,
        stderr_path: Path,
        last_message_path: Path,
        events_path: Path | None,
    ) -> None:
        for path in (stdout_path, stderr_path, last_message_path, events_path):
            if path is None:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _should_retry_without_json(
        self,
        *,
        parsed_json: dict[str, Any] | None,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> bool:
        lowered = f"{stdout}\n{stderr}".lower()
        websocket_failure = "failed to connect to websocket" in lowered or "responses_websocket" in lowered
        stream_disconnect = (
            "stream disconnected before completion" in lowered
            or "turn.failed" in lowered
            or "error sending request for url" in lowered
        )
        if parsed_json is None:
            return websocket_failure or stream_disconnect or (timed_out and "websocket" in lowered)
        return self._looks_like_stream_event_envelope(parsed_json) or websocket_failure or stream_disconnect

    def _should_retry_transport(self, output: RunnerOutput) -> bool:
        lowered = f"{output.stdout}\n{output.stderr}".lower()
        websocket_failure = "failed to connect to websocket" in lowered or "responses_websocket" in lowered
        stream_disconnect = (
            "stream disconnected before completion" in lowered
            or "stream disconnected - retrying sampling request" in lowered
            or "error sending request for url" in lowered
        )
        transport_issue = websocket_failure or stream_disconnect
        if output.parse_error and "usage limit" in output.parse_error.lower():
            return False
        if output.parsed_json is not None and not self._looks_like_stream_event_envelope(output.parsed_json):
            return False
        if output.timed_out:
            return transport_issue
        if output.parse_error == "runner stdout is empty":
            return transport_issue
        if output.parsed_json is None:
            return transport_issue and (output.returncode is None or output.returncode != 0 or not output.stdout.strip())
        return transport_issue

    def _refine_parse_error(
        self,
        *,
        parse_error: str | None,
        stdout: str,
        stderr: str,
    ) -> str | None:
        if parse_error is None:
            return None
        usage_limit_line = self._usage_limit_line(stdout=stdout, stderr=stderr)
        if usage_limit_line is not None:
            return usage_limit_line
        return parse_error

    @staticmethod
    def _usage_limit_line(*, stdout: str, stderr: str) -> str | None:
        for source in (stderr, stdout):
            for line in source.splitlines():
                if "usage limit" in line.lower():
                    return line.strip()
        return None

    @staticmethod
    def _looks_like_stream_event_envelope(parsed_json: dict[str, Any]) -> bool:
        return "type" in parsed_json and "status" not in parsed_json

    def _materialize_command(
        self,
        prompt: str,
        prompt_path: Path,
        workdir: Path,
        last_message_path: Path,
        *,
        json_output: bool,
    ) -> tuple[list[str], str | None]:
        command = []
        uses_prompt_placeholder = False
        for part in self.settings.command:
            replaced = (
                part.replace("__PROMPT_FILE__", str(prompt_path))
                .replace("__WORKDIR__", str(workdir))
                .replace("__LAST_MESSAGE_FILE__", str(last_message_path))
                .replace("__PROMPT__", prompt)
            )
            if "__PROMPT_FILE__" in part or "__PROMPT__" in part:
                uses_prompt_placeholder = True
            command.append(replaced)
        if json_output and "--json" not in command:
            insert_at = len(command) - 1 if command else 0
            command.insert(max(insert_at, 0), "--json")
        stdin_payload = None if uses_prompt_placeholder else prompt
        return command, stdin_payload

    def _run_captured(
        self,
        *,
        command: list[str],
        stdin_payload: str | None,
        workdir: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
        env: dict[str, str] | None,
    ) -> tuple[str, str, int | None, bool]:
        return self._run_streaming(
            command=command,
            stdin_payload=stdin_payload,
            workdir=workdir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            events_path=None,
            timeout_seconds=timeout_seconds,
            env=env,
        )

    def _run_streaming(
        self,
        *,
        command: list[str],
        stdin_payload: str | None,
        workdir: Path,
        stdout_path: Path,
        stderr_path: Path,
        events_path: Path | None,
        timeout_seconds: int,
        env: dict[str, str] | None,
    ) -> tuple[str, str, int | None, bool]:
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
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
                stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            assert process.stderr is not None

            stdout_thread = Thread(
                target=self._copy_stream,
                args=(process.stdout, stdout_handle, stdout_chunks, events_handle),
                daemon=True,
            )
            stderr_thread = Thread(
                target=self._copy_stream,
                args=(process.stderr, stderr_handle, stderr_chunks, None),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            try:
                if stdin_payload is not None and process.stdin is not None:
                    try:
                        process.stdin.write(stdin_payload)
                    except BrokenPipeError:
                        pass
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                returncode = process.wait(timeout=timeout_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                returncode = None
                timed_out = True
            finally:
                stdout_thread.join(timeout=15)
                stderr_thread.join(timeout=15)

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if timed_out:
            timeout_marker = "\nRunner timed out."
            if not stderr.endswith(timeout_marker):
                stderr += timeout_marker
                stderr_path.write_text(stderr, encoding="utf-8")
        self._stabilize_after_process_exit(timed_out=timed_out)
        return stdout, stderr, returncode, timed_out

    def _copy_stream(self, stream, sink_handle, sink_chunks: list[str], tee_handle) -> None:
        try:
            for chunk in iter(stream.readline, ""):
                sink_chunks.append(chunk)
                sink_handle.write(chunk)
                sink_handle.flush()
                if tee_handle is not None:
                    tee_handle.write(chunk)
                    tee_handle.flush()
        finally:
            stream.close()

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
            process.kill()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _stabilize_after_process_exit(self, *, timed_out: bool) -> None:
        if os.name != "nt":
            return
        time.sleep(2.0 if timed_out else 0.5)

    def _stabilize_between_transport_retries(self, *, transport_attempt: int) -> None:
        time.sleep(max(0.0, self.settings.transport_retry_backoff_seconds) * transport_attempt)


def extract_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    stripped = text.strip()
    if not stripped:
        return None, "runner stdout is empty"
    direct = _try_json_load(stripped)
    if direct is not None:
        return direct, None

    for chunk in _code_fence_candidates(stripped):
        loaded = _try_json_load(chunk)
        if loaded is not None:
            return loaded, None

    decoder = json.JSONDecoder()
    best: dict[str, Any] | None = None
    best_span = -1
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            candidate, consumed = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and consumed > best_span:
            best = candidate
            best_span = consumed
    if best is not None:
        return best, None
    return None, "runner stdout does not contain a parseable JSON object"


def _try_json_load(text: str) -> dict[str, Any] | None:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _code_fence_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    fence = "```"
    start = 0
    while True:
        first = text.find(fence, start)
        if first == -1:
            break
        second = text.find(fence, first + len(fence))
        if second == -1:
            break
        body = text[first + len(fence) : second].strip()
        if body.startswith("json"):
            body = body[4:].strip()
        candidates.append(body)
        start = second + len(fence)
    return candidates

