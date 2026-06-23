from __future__ import annotations

import re
import subprocess
from typing import Any

from pydantic import BaseModel

from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge


class DockerEnvironmentConfig(BaseModel):
    container_name: str
    cwd: str = "/workspace/eval_repo"
    env: dict[str, str] = {}
    timeout: int = 180
    interpreter: list[str] = ["bash", "-lc"]


class DockerEnvironment:
    """Execute live-swe-agent shell actions inside a prepared Docker container."""

    def __init__(self, *, config_class: type = DockerEnvironmentConfig, **kwargs):
        self.config = config_class(**kwargs)
        self._last_command = ""
        self._consecutive_command_count = 0
        self._observed_output_chars = 0

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        normalized_command = _normalize_shell_command(command)
        rejected = _reject_low_quality_global_query(normalized_command)
        if rejected is not None:
            return rejected

        rejected = self._reject_repeated_command(normalized_command)
        if rejected is not None:
            return rejected

        workdir = cwd or self.config.cwd
        exec_args = ["docker", "exec", "-i", "-w", workdir]
        for key, value in self.config.env.items():
            exec_args.extend(["-e", f"{key}={value}"])
        exec_args.append(self.config.container_name)
        if self.config.interpreter:
            exec_args.extend([*self.config.interpreter, command])
        else:
            exec_args.extend(["bash", "-lc", command])

        try:
            result = subprocess.run(
                exec_args,
                text=True,
                timeout=timeout or self.config.timeout,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            cleaned_output = self._cap_observation_output(_clean_command_output(result.stdout))
            output = {
                "output": cleaned_output,
                "returncode": result.returncode,
                "exception_info": "",
            }
        except Exception as exc:  # noqa: BLE001
            raw_output = getattr(exc, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            cleaned_output = self._cap_observation_output(_clean_command_output(raw_output))
            output = {
                "output": cleaned_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the docker command: {exc}",
                "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }
        self._check_finished(output)
        return output

    def _reject_repeated_command(self, command: str) -> dict[str, Any] | None:
        if command == self._last_command:
            self._consecutive_command_count += 1
        else:
            self._last_command = command
            self._consecutive_command_count = 1

        if self._consecutive_command_count <= 3:
            return None
        return {
            "output": (
                "Command rejected by benchmark harness before execution.\n"
                "Reason: the exact same command was issued more than 3 times consecutively.\n"
                "Do not repeatedly read the same location. Edit the worktree now, inspect a different "
                "checkpoint, or explain completion with the final submission command."
            ),
            "returncode": 2,
            "exception_info": "",
            "extra": {"blocked_by_harness": True, "reason": "repeated_command"},
        }

    def _cap_observation_output(self, output: str) -> str:
        self._observed_output_chars += len(output)
        if self._observed_output_chars <= 100_000 and len(output) <= 30_000:
            return output
        if len(output) <= 30_000:
            return output

        elided = len(output) - 30_000
        return (
            "[benchmark harness note: command output was truncated because accumulated "
            "tool output for this run exceeded 100000 characters. Showing the final "
            f"30000 characters; {elided} characters elided.]\n"
            + output[-30_000:]
        )

    def _check_finished(self, output: dict):
        if output["returncode"] != 0:
            return

        cleaned_output = output.get("output", "").replace("\x00", "")
        lines = cleaned_output.lstrip().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT":
                submission = "".join(lines[index + 1 :])
                raise Submitted(
                    {
                        "role": "exit",
                        "content": submission,
                        "extra": {"exit_status": "Submitted", "submission": submission},
                    }
                )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(
            {
                "container_name": self.config.container_name,
                "cwd": self.config.cwd,
                "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
            },
            kwargs,
        )

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "environment": self.config.model_dump(mode="json"),
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }


def _clean_command_output(raw_output: str) -> str:
    return raw_output.replace("\x00", "")


def _reject_low_quality_global_query(command: str) -> dict[str, Any] | None:
    """Reject broad repo-root discovery commands before they reach docker exec.

    Gemini in particular tends to issue broad recursive searches that can hang
    the docker shell or flood the context. Returning a normal tool observation
    lets the agent recover and issue a narrower command instead.
    """

    normalized = _normalize_shell_command(command)
    reason = _global_query_rejection_reason(normalized)
    if reason is None:
        return None
    return {
        "output": (
            "Command rejected by benchmark harness before execution.\n"
            f"Reason: {reason}\n"
            "Use a narrowly scoped command instead. Prefer targeted file reads "
            "or `rg` with a concrete search term, a likely source directory, "
            "and exclusions for caches/build/vendor directories."
        ),
        "returncode": 2,
        "exception_info": "",
        "extra": {"blocked_by_harness": True, "reason": reason},
    }


def _normalize_shell_command(command: str) -> str:
    command = command.replace("\\\n", " ")
    command = re.sub(r"\s+", " ", command)
    return command.strip()


def _global_query_rejection_reason(command: str) -> str | None:
    if _is_recursive_grep(command) and not _recursive_grep_is_bounded(command):
        return "`grep -r/-R` must include --exclude-dir and pipe output through `head -c <N>`"

    # Broad recursive grep from repository root, including piped forms like
    # `grep -r "term" . | grep ...`.
    if re.search(r"(^|[;&|]\s*)grep\s+[^;&|]*-(?:[A-Za-z]*[rR][A-Za-z]*)\b[^;&|]*\s\.(?:\s|$|[;&|])", command):
        return "`grep -r/-R ... .` is a broad repository-root recursive search"

    # `find .`, `find ./...`, and piped `find . | xargs ...` are commonly too
    # broad for this benchmark repair setting.
    if re.search(r"(^|[;&|]\s*)find\s+\.(?:\s|$|/|[;&|])", command):
        return "`find .` is a broad repository-root traversal"

    if re.search(r"(^|[;&|]\s*)ls\s+[^;&|]*-[-A-Za-z]*R[-A-Za-z]*\b", command):
        return "`ls -R` is a broad recursive directory listing"

    forbidden_paths = (
        ".venv",
        "node_modules",
        "build",
        "dist",
        "target",
        ".git",
        ".mypy_cache",
        "__pycache__",
    )
    for path in forbidden_paths:
        if re.search(rf"(^|[\s/]){re.escape(path)}($|[\s/])", command):
            return f"searching generated/cache/vendor path `{path}` is not allowed"

    return None


def _is_recursive_grep(command: str) -> bool:
    return bool(re.search(r"(^|[;&|]\s*)grep\s+[^;&|]*-(?:[A-Za-z]*[rR][A-Za-z]*)\b", command))


def _recursive_grep_is_bounded(command: str) -> bool:
    has_exclude = "--exclude-dir" in command
    has_head_c = bool(re.search(r"\|\s*head\s+-c\s+\S+", command))
    return has_exclude and has_head_c
