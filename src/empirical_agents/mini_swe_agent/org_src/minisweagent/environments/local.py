from __future__ import annotations

import os
import platform
import subprocess
from typing import Any

from pydantic import BaseModel

from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge


class LocalEnvironmentConfig(BaseModel):
    cwd: str = ""
    env: dict[str, str] = {}
    timeout: int = 30
    interpreter: list[str] = []


class LocalEnvironment:
    def __init__(self, *, config_class: type = LocalEnvironmentConfig, **kwargs):
        """This class executes commands directly on the local machine."""
        self.config = config_class(**kwargs)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in the local environment and return the result as a dict."""
        command = action.get("command", "")
        cwd = cwd or self.config.cwd or os.getcwd()
        try:
            if self.config.interpreter:
                command_args: str | list[str] = [*self.config.interpreter, command]
                shell = False
            else:
                command_args = command
                shell = True
            result = subprocess.run(
                command_args,
                shell=shell,
                text=True,
                cwd=cwd,
                env=os.environ | self.config.env,
                timeout=timeout or self.config.timeout,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = {"output": _clean_command_output(result.stdout), "returncode": result.returncode, "exception_info": ""}
        except Exception as e:
            raw_output = getattr(e, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            output = {
                "output": _clean_command_output(raw_output),
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        self._check_finished(output)
        return output

    def _check_finished(self, output: dict):
        """Raises Submitted if the output indicates task completion."""
        if output["returncode"] != 0:
            return

        raw_output = output.get("output", "")
        # WSL bash on Windows may inject a warning line with embedded NUL bytes
        # before the sentinel. Normalize that noise away before checking finish.
        cleaned_output = raw_output.replace("\x00", "")
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
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), os.environ, kwargs)

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
    cleaned = raw_output.replace("\x00", "")
    lines = cleaned.splitlines(keepends=True)
    trimmed: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("wsl:"):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            continue
        trimmed.append(line)
        index += 1
    return "".join(trimmed)
