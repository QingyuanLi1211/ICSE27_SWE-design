from __future__ import annotations

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
    """Execute mini-swe-agent shell actions inside a prepared Docker container."""

    def __init__(self, *, config_class: type = DockerEnvironmentConfig, **kwargs):
        self.config = config_class(**kwargs)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
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
            output = {
                "output": _clean_command_output(result.stdout),
                "returncode": result.returncode,
                "exception_info": "",
            }
        except Exception as exc:  # noqa: BLE001
            raw_output = getattr(exc, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            output = {
                "output": _clean_command_output(raw_output),
                "returncode": -1,
                "exception_info": f"An error occurred while executing the docker command: {exc}",
                "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }
        self._check_finished(output)
        return output

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
