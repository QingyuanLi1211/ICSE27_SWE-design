"""以子进程方式调用 mini CLI，并落盘 prompt/stdout/stderr/trajectory。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RunnerSettings:
    command: list[str]
    timeout_seconds: int = 3600
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
    trajectory_path: Path
    command_path: Path
    parsed_trajectory: dict[str, Any] | None
    timed_out: bool = False
    saw_terminal_submission: bool = False
    completion_source: str = "process"
    terminated_after_terminal_submission: bool = False


class SubprocessMiniRunner:
    TERMINAL_SUBMISSION_GRACE_SECONDS = 3.0
    POLL_INTERVAL_SECONDS = 0.2

    def __init__(self, settings: RunnerSettings) -> None:
        self.settings = settings

    def run(
        self,
        task: str,
        *,
        workdir: Path,
        attempt_dir: Path,
        timeout_seconds: int | None = None,
    ) -> RunnerOutput:
        attempt_dir = attempt_dir.resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = (attempt_dir / "prompt.md").resolve()
        stdout_path = (attempt_dir / "runner_stdout.txt").resolve()
        stderr_path = (attempt_dir / "runner_stderr.txt").resolve()
        trajectory_path = (attempt_dir / "mini_run.traj.json").resolve()
        command_path = (attempt_dir / "runner_command.json").resolve()
        prompt_path.write_text(task, encoding="utf-8")

        env = dict(os.environ)
        env.update(self.settings.env)
        effective_timeout = timeout_seconds if timeout_seconds is not None else self.settings.timeout_seconds
        command = self._materialize_command(task=task, trajectory_path=trajectory_path)
        command_path.write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")

        stdout = ""
        stderr = ""
        returncode: int | None = None
        timed_out = False
        saw_terminal_submission = False
        completion_source = "process"
        terminated_after_terminal_submission = False
        process: subprocess.Popen[str] | None = None
        try:
            with (
                stdout_path.open("w", encoding="utf-8") as stdout_handle,
                stderr_path.open("w", encoding="utf-8") as stderr_handle,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=str(workdir),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    env=env,
                )
                start_time = time.monotonic()
                while True:
                    returncode = process.poll()
                    parsed_trajectory = _load_json_if_exists(trajectory_path)
                    if _has_terminal_submission(parsed_trajectory):
                        saw_terminal_submission = True
                        completion_source = "trajectory_submitted"
                        deadline = time.monotonic() + self.TERMINAL_SUBMISSION_GRACE_SECONDS
                        while time.monotonic() < deadline:
                            returncode = process.poll()
                            if returncode is not None:
                                break
                            time.sleep(self.POLL_INTERVAL_SECONDS)
                        if returncode is None:
                            self._terminate_process(process)
                            terminated_after_terminal_submission = True
                            returncode = process.poll()
                        break
                    if returncode is not None:
                        break
                    if time.monotonic() - start_time >= effective_timeout:
                        self._terminate_process(process)
                        timed_out = True
                        returncode = None
                        completion_source = "timeout"
                        break
                    time.sleep(self.POLL_INTERVAL_SECONDS)
                if returncode is None and process.poll() is None:
                    returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if process is not None:
                self._terminate_process(process)
            timed_out = True
            returncode = None

        if stdout_path.exists():
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        if stderr_path.exists():
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        parsed_trajectory = _load_json_if_exists(trajectory_path)
        return RunnerOutput(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            trajectory_path=trajectory_path,
            command_path=command_path,
            parsed_trajectory=parsed_trajectory,
            timed_out=timed_out,
            saw_terminal_submission=saw_terminal_submission or _has_terminal_submission(parsed_trajectory),
            completion_source=completion_source,
            terminated_after_terminal_submission=terminated_after_terminal_submission,
        )

    def _materialize_command(self, *, task: str, trajectory_path: Path) -> list[str]:
        return [
            part.replace("__TASK__", task).replace("__TRAJECTORY_FILE__", str(trajectory_path))
            for part in self.settings.command
        ]

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


def _load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _has_terminal_submission(parsed_trajectory: dict[str, Any] | None) -> bool:
    if not isinstance(parsed_trajectory, dict):
        return False
    info = parsed_trajectory.get("info", {})
    if not isinstance(info, dict):
        return False
    return info.get("exit_status") == "Submitted"
