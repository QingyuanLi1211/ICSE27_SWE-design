"""Claude Code repair runner."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.empirical_agents.claude_code.claude_code_runner import RunnerSettings, SubprocessClaudeCodeRunner
from src.utils.repair_pipeline import AgentRunResult

CLAUDE_CODE_RATE_LIMIT_EXIT_CODE = 75


@dataclass(slots=True)
class ClaudeCodeConfig:
    cli_path: Path | None = None
    conda_env: str = "claudecode"
    model: str = "claude-opus-4-7"
    api_key: str | None = None
    thinking_effort: str = "high"
    timeout_seconds: int = 1800
    agent_edit_mode: Literal["workspace-write", "danger-full-access"] = "danger-full-access"
    max_turns: int = 120
    tools: str = "Bash,Read,Edit,MultiEdit,Write,Glob,Grep,LS"
    repair_environment: Literal["local", "docker"] = "local"
    docker_container_name: str | None = None
    docker_workdir: str = "/workspace/eval_repo"


def run_claude_code_fixing(
    *,
    prompt: str,
    candidate_dir: Path,
    trajectory_root: Path,
    logger,
    config: ClaudeCodeConfig,
) -> AgentRunResult:
    env: dict[str, str] = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
    }
    if config.api_key:
        env["ANTHROPIC_API_KEY"] = config.api_key

    docker_container_name = config.docker_container_name
    docker_workdir = config.docker_workdir
    effective_prompt = prompt
    effective_tools = config.tools
    if config.repair_environment == "docker":
        docker_container_name = docker_container_name or os.environ.get("MSWEA_REPAIR_DOCKER_CONTAINER")
        docker_workdir = docker_workdir or os.environ.get("MSWEA_REPAIR_DOCKER_WORKDIR", "/workspace/eval_repo")
        if not docker_container_name:
            raise RuntimeError("Docker repair requested but MSWEA_REPAIR_DOCKER_CONTAINER is not set.")
        effective_prompt = _docker_boundary_prefix(
            container_name=docker_container_name,
            docker_workdir=docker_workdir,
        ) + prompt

    runner = SubprocessClaudeCodeRunner(
        RunnerSettings(
            command=build_claude_code_command(
                cli_path=str(resolve_claude_cli(config.conda_env, config.cli_path)),
                model=config.model,
                thinking_effort=config.thinking_effort,
                agent_edit_mode=config.agent_edit_mode,
                max_turns=config.max_turns,
                tools=effective_tools,
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
    )
    completed = (
        not output.rate_limited
        and (output.saw_terminal_success or (output.returncode == 0 and not output.timed_out))
    )
    logger.log(
        "claude_code_run "
        f"returncode={output.returncode} "
        f"timed_out={output.timed_out} "
        f"rate_limited={output.rate_limited} "
        f"rate_limit_reason={output.rate_limit_reason!r} "
        f"saw_terminal_success={output.saw_terminal_success} "
        f"completion_source={output.completion_source} "
        f"terminated_after_terminal_success={output.terminated_after_terminal_success}"
    )
    return AgentRunResult(
        completed=completed,
        raw_paths={
            "events_path": output.events_path,
            "stdout_path": output.stdout_path,
            "stderr_path": output.stderr_path,
            "command_path": output.command_path,
        },
        summary={
            "returncode": output.returncode,
            "timed_out": output.timed_out,
            "rate_limited": output.rate_limited,
            "rate_limit_reason": output.rate_limit_reason,
            "saw_terminal_success": output.saw_terminal_success,
            "completion_source": output.completion_source,
            "terminated_after_terminal_success": output.terminated_after_terminal_success,
            "effective_agent_edit_mode": config.agent_edit_mode,
            "effective_model_name": config.model,
            "effective_thinking_effort": config.thinking_effort,
            "effective_max_turns": config.max_turns,
            "effective_tools": effective_tools,
            "effective_repair_environment": config.repair_environment,
            "effective_docker_container_name": docker_container_name,
            "effective_docker_workdir": docker_workdir,
        },
    )


def build_claude_code_command(
    *,
    cli_path: str,
    model: str,
    thinking_effort: str,
    agent_edit_mode: Literal["workspace-write", "danger-full-access"],
    max_turns: int,
    tools: str,
) -> list[str]:
    command = [
        cli_path,
        "-p",
        "__PROMPT__",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--append-system-prompt-file",
        "__APPEND_SYSTEM_PROMPT_FILE__",
        "--tools",
        tools,
        "--disallowedTools",
        "WebFetch",
        "WebSearch",
        "--max-turns",
        str(max_turns),
    ]
    if agent_edit_mode == "danger-full-access":
        command.append("--dangerously-skip-permissions")
    else:
        command.extend(["--permission-mode", "acceptEdits"])
    if thinking_effort:
        command.extend(["--effort", thinking_effort])
    if model:
        command.extend(["--model", model])
    return command


def _docker_boundary_prefix(*, container_name: str, docker_workdir: str) -> str:
    return f"""\
Docker repair mode:
- The repository is inside Docker container `{container_name}` at `{docker_workdir}`.
- The current host working directory is only a harness placeholder. Do not read, edit, or test files there.
- Use Bash only, and run every repository command through Docker, for example:
  docker exec -i -w {docker_workdir} {container_name} bash -lc "pwd && ls"
- Read, search, edit, and test only via `docker exec -i -w {docker_workdir} {container_name} bash -lc "..."`.
- Do not inspect, read, list, search, grep, glob, or otherwise access files outside `{docker_workdir}` inside the container.
- Do not use web search, web fetch, browser automation, remote repositories, package downloads, curl, wget, or any command that accesses the network.
- Leave final edits in the Docker worktree. The outer harness will extract the patch from the container.

"""


def resolve_claude_cli(conda_env: str, explicit_cli_path: Path | None) -> Path | str:
    if explicit_cli_path is not None:
        resolved = explicit_cli_path.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Claude Code CLI does not exist: {resolved}")
        usable = _resolve_usable_claude_cli(resolved)
        if usable is None:
            raise FileNotFoundError(f"Claude Code CLI exists but is not usable: {resolved}")
        return usable

    candidates = [
        Path.home() / ".conda" / "envs" / conda_env / "claude.cmd",
        Path.home() / ".conda" / "envs" / conda_env / "Scripts" / "claude.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.exe",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude",
    ]
    for candidate in _iter_cli_candidates(candidates):
        usable = _resolve_usable_claude_cli(candidate)
        if usable is not None:
            return usable

    for name in ("claude.exe", "claude.cmd", "claude"):
        path_candidate = shutil.which(name)
        if not path_candidate:
            continue
        usable = _resolve_usable_claude_cli(Path(path_candidate))
        if usable is not None:
            return usable

    for candidate in _iter_where_candidates():
        usable = _resolve_usable_claude_cli(candidate)
        if usable is not None:
            return usable
    return "claude"


def _resolve_usable_claude_cli(path: Path) -> Path | None:
    if not path.exists():
        return None
    if path.suffix.lower() != ".cmd":
        return path.resolve()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"%dp0%\\([^"]+claude\.exe)"', content, flags=re.IGNORECASE)
    if match is None:
        return path.resolve()
    target = (path.parent / match.group(1)).resolve()
    if target.exists():
        return target
    return None


def _iter_cli_candidates(candidates: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _iter_where_candidates() -> list[Path]:
    try:
        proc = subprocess.run(
            ["where.exe", "claude"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return []

    candidates: list[Path] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        candidates.append(Path(line))
    return _iter_cli_candidates(candidates)


