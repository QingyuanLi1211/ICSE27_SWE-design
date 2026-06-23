"""Codex repair runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.empirical_agents.codex.codex_runner import RunnerSettings, SubprocessCodexRunner
from src.utils.repair_pipeline import AgentRunResult

CODEX_WORKTREE_BOUNDARY_PREFIX = """\
Hard boundary:
- Work only in the current working directory, which is the repository root.
- Absolutely do not inspect, read, list, search, grep, glob, or otherwise access any file or directory outside the current working directory.
- Do not use absolute paths that point outside the current working directory.
- Accessing files outside the worktree may be treated as cheating and will invalidate the run.
- Do not run broad filesystem scans outside the repository root such as `find /`, `find C:/`, or equivalent commands.

"""


@dataclass(slots=True)
class CodexConfig:
    cli_path: str = "codex.cmd"
    model: str = "gpt-5.4"
    api_key: str | None = None
    thinking_effort: str = "high"
    timeout_seconds: int = 1800
    agent_edit_mode: Literal["workspace-write", "danger-full-access"] = "danger-full-access"
    repair_environment: Literal["local", "docker"] = "local"
    docker_container_name: str | None = None
    docker_workdir: str = "/workspace/eval_repo"


def run_codex_fixing(
    *,
    prompt: str,
    candidate_dir: Path,
    trajectory_root: Path,
    logger,
    config: CodexConfig,
) -> AgentRunResult:
    env: dict[str, str] = {}
    if config.api_key:
        env["OPENAI_API_KEY"] = config.api_key

    docker_container_name = config.docker_container_name
    docker_workdir = config.docker_workdir
    effective_prompt = CODEX_WORKTREE_BOUNDARY_PREFIX + prompt
    if config.repair_environment == "docker":
        docker_container_name = docker_container_name or os.environ.get("MSWEA_REPAIR_DOCKER_CONTAINER")
        docker_workdir = docker_workdir or os.environ.get("MSWEA_REPAIR_DOCKER_WORKDIR", "/workspace/eval_repo")
        if not docker_container_name:
            raise RuntimeError("Docker repair requested but MSWEA_REPAIR_DOCKER_CONTAINER is not set.")
        effective_prompt = _docker_boundary_prefix(
            container_name=docker_container_name,
            docker_workdir=docker_workdir,
        ) + prompt

    runner = SubprocessCodexRunner(
        RunnerSettings(
            command=build_codex_command(
                cli_path=config.cli_path,
                model=config.model,
                thinking_effort=config.thinking_effort,
                agent_edit_mode=config.agent_edit_mode,
            ),
            timeout_seconds=config.timeout_seconds,
            env=env,
        )
    )
    output = runner.run(
        effective_prompt,
        workdir=candidate_dir,
        attempt_dir=trajectory_root,
        timeout_seconds=config.timeout_seconds,
        json_output=True,
    )
    completed = output.returncode == 0 and not output.timed_out
    logger.log(
        f"codex_run returncode={output.returncode} timed_out={output.timed_out} parse_error={output.parse_error}"
    )
    return AgentRunResult(
        completed=completed,
        raw_paths={
            "events_path": output.events_path,
            "stdout_path": output.stdout_path,
            "stderr_path": output.stderr_path,
            "last_message_path": output.last_message_path,
        },
        summary={
            "returncode": output.returncode,
            "timed_out": output.timed_out,
            "parse_error": output.parse_error,
            "effective_agent_edit_mode": config.agent_edit_mode,
            "effective_repair_environment": config.repair_environment,
            "effective_docker_container_name": docker_container_name,
            "effective_docker_workdir": docker_workdir,
        },
    )


def build_codex_command(
    *,
    cli_path: str,
    model: str,
    thinking_effort: str,
    agent_edit_mode: Literal["workspace-write", "danger-full-access"],
) -> list[str]:
    command = [cli_path]
    if agent_edit_mode == "danger-full-access":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.extend(["-a", "never", "-s", "workspace-write"])
    command.extend(
        [
            "exec",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            "__WORKDIR__",
            "-o",
            "__LAST_MESSAGE_FILE__",
        ]
    )
    if thinking_effort:
        command.extend(["-c", f"reasoning_effort={thinking_effort}"])
    if model:
        command.extend(["-m", model])
    command.append("-")
    return command


def _docker_boundary_prefix(*, container_name: str, docker_workdir: str) -> str:
    return f"""\
Hard boundary:
- The repository is inside Docker container `{container_name}` at `{docker_workdir}`.
- The current host working directory is only a harness placeholder. Do not read, edit, or test files there.
- Run every repository command through Docker, for example:
  docker exec -i -w {docker_workdir} {container_name} bash -lc "pwd && ls"
- Read, search, edit, and test only via `docker exec -i -w {docker_workdir} {container_name} bash -lc "..."`.
- Do not inspect, read, list, search, grep, glob, or otherwise access files outside `{docker_workdir}` inside the container.
- Do not use web search, web fetch, browser automation, remote repositories, package downloads, curl, wget, or any command that accesses the network.
- Leave final edits in the Docker worktree. The outer harness will extract the patch from the container.

"""


