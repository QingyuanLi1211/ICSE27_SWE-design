"""Live-SWE-agent repair runner.

This keeps the shared mini-swe-agent subprocess runner, but swaps in the Live
prompt/config and model routing rules used by this benchmark harness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from src.empirical_agents.live_swe_agent.live_runner import RunnerSettings, SubprocessMiniRunner
from src.utils.repair_pipeline import AgentRunResult


ROOT = Path(__file__).resolve().parents[4]
ARK_CODING_API_BASE = "https://ark.cn-beijing.volces.com/api/coding/v3"
MINIMAX_API_BASE = "https://api.minimaxi.com/anthropic"
GEMINI_API_BASE = "https://bitexingai.com/v1"
ARK_MODELS = {"glm-5.1"}


@dataclass(slots=True)
class LiveConfig:
    model: str = "gpt-5.4"
    api_key: str | None = None
    api_key_env: str | None = None
    thinking_effort: str = "high"
    timeout_seconds: int = 1800
    agent_edit_mode: Literal["workspace-write", "danger-full-access"] = "danger-full-access"
    conda_env: str = "livesweagent"
    live_python: Path | None = None
    api_base: str | None = None
    max_tokens: int = 4096
    repair_environment: Literal["local", "docker"] = "local"
    docker_container_name: str | None = None
    docker_workdir: str = "/workspace/eval_repo"
    live_config: Path = (
        ROOT
        / "src"
        / "empirical_agents"
        / "live_swe_agent"
        / "run_agent_fixing"
        / "live_blind_repair.yaml"
    )


def run_live_fixing(
    *,
    prompt: str,
    candidate_dir: Path,
    trajectory_root: Path,
    logger,
    config: LiveConfig,
) -> AgentRunResult:
    resolved = resolve_live_model_config(config)
    api_key = config.api_key or os.environ.get(str(resolved["api_key_env"]), "")
    if not api_key:
        raise RuntimeError(
            f"Missing live_swe_agent API key. Checked --api-key and {resolved['api_key_env']}."
        )

    docker_container_name = config.docker_container_name
    docker_workdir = config.docker_workdir
    if config.repair_environment == "docker":
        docker_container_name = docker_container_name or os.environ.get("MSWEA_REPAIR_DOCKER_CONTAINER")
        docker_workdir = docker_workdir or os.environ.get("MSWEA_REPAIR_DOCKER_WORKDIR", "/workspace/eval_repo")
        if not docker_container_name:
            raise RuntimeError("Docker repair requested but MSWEA_REPAIR_DOCKER_CONTAINER is not set.")

    live_python = resolve_live_python(config.conda_env, config.live_python)
    runner = SubprocessMiniRunner(
        RunnerSettings(
            command=build_live_command(
                live_python=live_python,
                live_config=config.live_config,
                model=str(resolved["model_name"]),
                api_base=resolved["api_base"],
                max_tokens=config.max_tokens,
                thinking_effort=str(resolved["reasoning_effort"]),
                extra_model_config=list(resolved["extra_model_config"]),
                model_class=resolved.get("model_class"),
                repair_environment=config.repair_environment,
                docker_container_name=docker_container_name,
                docker_workdir=docker_workdir,
            ),
            timeout_seconds=config.timeout_seconds,
            env={
                str(resolved["api_key_env"]): api_key,
                "MSWEA_CONFIGURED": "true",
                "MSWEA_SILENT_STARTUP": "1",
                "PYTHONUTF8": "1",
                "PYTHONPATH": _build_pythonpath(
                    ROOT / "src" / "empirical_agents" / "live_swe_agent" / "org_src"
                ),
            },
        )
    )
    output = runner.run(
        prompt,
        workdir=candidate_dir,
        attempt_dir=trajectory_root,
        timeout_seconds=config.timeout_seconds,
    )
    parsed = output.parsed_trajectory or {}
    info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
    completed = output.saw_terminal_submission or (output.returncode == 0 and not output.timed_out)
    logger.log(
        "live_run "
        f"returncode={output.returncode} "
        f"timed_out={output.timed_out} "
        f"exit_status={info.get('exit_status')} "
        f"saw_terminal_submission={output.saw_terminal_submission} "
        f"completion_source={output.completion_source} "
        f"terminated_after_terminal_submission={output.terminated_after_terminal_submission}"
    )
    return AgentRunResult(
        completed=completed,
        raw_paths={
            "trajectory_path": output.trajectory_path,
            "stdout_path": output.stdout_path,
            "stderr_path": output.stderr_path,
            "command_path": output.command_path,
        },
        summary={
            "returncode": output.returncode,
            "timed_out": output.timed_out,
            "exit_status": info.get("exit_status"),
            "submission": info.get("submission"),
            "saw_terminal_submission": output.saw_terminal_submission,
            "completion_source": output.completion_source,
            "terminated_after_terminal_submission": output.terminated_after_terminal_submission,
            "effective_agent_edit_mode": config.agent_edit_mode,
            "effective_model_name": resolved["model_name"],
            "effective_api_base": resolved["api_base"],
            "effective_api_key_env": resolved["api_key_env"],
            "effective_reasoning_effort": resolved["reasoning_effort"],
            "effective_model_class": resolved.get("model_class"),
            "effective_repair_environment": config.repair_environment,
            "effective_docker_container_name": docker_container_name,
            "effective_docker_workdir": docker_workdir,
        },
    )


def build_live_command(
    *,
    live_python: Path,
    live_config: Path,
    model: str,
    api_base: str | None,
    max_tokens: int,
    thinking_effort: str,
    extra_model_config: list[str],
    model_class: str | None,
    repair_environment: str = "local",
    docker_container_name: str | None = None,
    docker_workdir: str = "/workspace/eval_repo",
) -> list[str]:
    command = [
        str(live_python),
        "-m",
        "minisweagent.run.mini",
        "-y",
        "--exit-immediately",
        "-m",
        model,
        "-t",
        "__TASK__",
        "-o",
        "__TRAJECTORY_FILE__",
        "-c",
        str(live_config),
        "-c",
        f"model.model_kwargs.max_tokens={max_tokens}",
        "-c",
        "model.cost_tracking=ignore_errors",
    ]
    if model_class:
        command.extend(["-c", f"model.model_class={model_class}"])
    if api_base:
        command.extend(["-c", f"model.model_kwargs.api_base={api_base}"])
    if thinking_effort:
        command.extend(["-c", f"model.model_kwargs.reasoning_effort={thinking_effort}"])
    if repair_environment == "docker":
        if not docker_container_name:
            raise RuntimeError("Docker repair requested but no container name was provided.")
        command.extend(
            [
                "-c",
                "environment.environment_class=docker",
                "-c",
                f"environment.container_name={docker_container_name}",
                "-c",
                f"environment.cwd={docker_workdir}",
            ]
        )
    for item in extra_model_config:
        command.extend(["-c", item])
    return command


def resolve_live_python(conda_env: str, explicit_python: Path | None) -> Path:
    if explicit_python is not None:
        resolved = explicit_python.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"live python does not exist: {resolved}")
        return resolved

    candidates = [
        Path.home() / ".conda" / "envs" / conda_env / "python.exe",
        ROOT / ".conda" / "envs" / conda_env / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Could not resolve python.exe for conda env `{conda_env}`. Pass it explicitly with --live-python."
    )


def resolve_live_model_config(config: LiveConfig) -> dict[str, object]:
    display_model = config.model.strip()
    model_tail = display_model.split("/")[-1]
    provider = display_model.split("/", 1)[0].lower() if "/" in display_model else ""
    lowered_tail = model_tail.lower()

    if lowered_tail.startswith("gemini-"):
        effective_api_base = config.api_base or GEMINI_API_BASE
        if _is_openai_compatible_gemini_base(effective_api_base):
            return {
                "model_name": f"openai/{model_tail}",
                "api_base": effective_api_base,
                "api_key_env": config.api_key_env or "GEMINI_API_KEY",
                "reasoning_effort": config.thinking_effort,
                # LiteLLM blocks non-standard OpenAI params for openai/*
                # models unless explicitly allowlisted. Gemini's OpenAI
                # compatibility layer maps reasoning_effort to thinkingLevel.
                "extra_model_config": [
                    "model.model_kwargs.allowed_openai_params.0=reasoning_effort",
                    f"model.model_kwargs.api_key_env={config.api_key_env or 'GEMINI_API_KEY'}",
                ],
                "model_class": None,
            }
        native_api_base = _normalize_gemini_api_base(effective_api_base)
        if native_api_base.startswith("http://127.0.0.1:8080/") or native_api_base.startswith("http://localhost:8080/"):
            return {
                "model_name": display_model if provider == "gemini" else f"gemini/{model_tail}",
                "api_base": native_api_base,
                "api_key_env": config.api_key_env or "GEMINI_API_KEY",
                "reasoning_effort": config.thinking_effort,
                "extra_model_config": [],
                "model_class": "minisweagent.models.sub2api_gemini_native_model.Sub2apiGeminiNativeModel",
            }
        return {
            "model_name": display_model if provider == "gemini" else f"gemini/{model_tail}",
            "api_base": native_api_base,
            "api_key_env": config.api_key_env or "GEMINI_API_KEY",
            "reasoning_effort": config.thinking_effort,
            "extra_model_config": [],
            "model_class": None,
        }

    if lowered_tail in ARK_MODELS:
        return {
            "model_name": f"openai/{model_tail}",
            "api_base": config.api_base or ARK_CODING_API_BASE,
            "api_key_env": config.api_key_env or "OPENAI_API_KEY",
            "reasoning_effort": config.thinking_effort,
            "extra_model_config": ["model.model_kwargs.extra_body.thinking.type=enabled"],
            "model_class": None,
        }

    if "minimax" in lowered_tail:
        return {
            "model_name": display_model if "/" in display_model else f"anthropic/{display_model}",
            "api_base": config.api_base or MINIMAX_API_BASE,
            "api_key_env": config.api_key_env or "ANTHROPIC_API_KEY",
            "reasoning_effort": "",
            "extra_model_config": [],
            "model_class": None,
        }

    if "/" in display_model:
        return {
            "model_name": display_model,
            "api_base": config.api_base,
            "api_key_env": config.api_key_env or _default_api_key_env(provider),
            "reasoning_effort": "" if provider == "anthropic" else config.thinking_effort,
            "extra_model_config": [],
            "model_class": None,
        }

    return {
        "model_name": f"openai/{display_model}",
        "api_base": config.api_base,
        "api_key_env": config.api_key_env or "OPENAI_API_KEY",
        "reasoning_effort": config.thinking_effort,
        "extra_model_config": [],
        "model_class": None,
    }


def _default_api_key_env(provider: str) -> str:
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if provider == "gemini":
        return "GEMINI_API_KEY"
    return "OPENAI_API_KEY"


def _build_pythonpath(org_src_root: Path) -> str:
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        return f"{org_src_root}{os.pathsep}{existing}"
    return str(org_src_root)


def _normalize_gemini_api_base(api_base: str | None) -> str:
    raw = (api_base or GEMINI_API_BASE).rstrip("/")
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if path.endswith("/v1beta/models"):
        path = path[: -len("/v1beta/models")] + "/v1beta"
    elif path.endswith("/models"):
        path = path[: -len("/models")]
    elif not path.endswith("/v1beta"):
        path = f"{path}/v1beta" if path else "/v1beta"
    normalized = urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    return normalized.rstrip("/")


def _is_openai_compatible_gemini_base(api_base: str | None) -> bool:
    if not api_base:
        return False
    lowered = api_base.rstrip("/").lower()
    return "/v1beta" not in lowered


