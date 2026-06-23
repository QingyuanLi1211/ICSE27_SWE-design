"""Subprocess-based Codex runner with retry-friendly prompt capture."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    last_message_path: Path
    parsed_json: dict[str, Any] | None
    parse_error: str | None
    timed_out: bool = False


class SubprocessCodexRunner:
    """Run a configurable Codex command against a prompt."""

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
        last_message_path = attempt_dir / "runner_last_message.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        command, stdin_payload = self._materialize_command(
            prompt,
            prompt_path,
            workdir,
            last_message_path,
        )
        env = None
        if self.settings.env:
            env = {**os.environ, **self.settings.env}
        effective_timeout = timeout_seconds if timeout_seconds is not None else self.settings.timeout_seconds

        try:
            completed = subprocess.run(
                command,
                cwd=str(workdir),
                input=stdin_payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                env=env,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + "\nRunner timed out."
            returncode = None
            timed_out = True

        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        parse_source = stdout
        if last_message_path.exists():
            last_message = last_message_path.read_text(encoding="utf-8").strip()
            if last_message:
                parse_source = last_message
        parsed_json, parse_error = extract_json_object(parse_source)
        return RunnerOutput(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            last_message_path=last_message_path,
            parsed_json=parsed_json,
            parse_error=parse_error,
            timed_out=timed_out,
        )

    def _materialize_command(
        self,
        prompt: str,
        prompt_path: Path,
        workdir: Path,
        last_message_path: Path,
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
        stdin_payload = None if uses_prompt_placeholder else prompt
        return command, stdin_payload


def build_default_codex_command(
    *,
    cli_path: str | None = None,
    model: str | None = None,
    reasoning_effort: str = "high",
) -> list[str]:
    resolved_cli = cli_path or ("codex.cmd" if os.name == "nt" else "codex")
    command = [
        resolved_cli,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--color",
        "never",
        "-C",
        "__WORKDIR__",
        "-o",
        "__LAST_MESSAGE_FILE__",
    ]
    if reasoning_effort:
        command.extend(["-c", f"reasoning_effort={reasoning_effort}"])
    if model:
        command.extend(["-m", model])
    command.append("-")
    return command


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
