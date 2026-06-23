"""mini_swe_agent repair step wrapper."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.empirical_agents.mini_swe_agent.mini_runner import RunnerSettings, SubprocessMiniRunner
from src.utils.repair_pipeline import AgentRunResult


ROOT = Path(__file__).resolve().parents[4]
DEEPSEEK_API_BASE = "https://api.deepseek.com"
MINIMAX_API_BASE = "https://api.minimaxi.com/anthropic"
MIMO_API_BASE = "https://api.xiaomimimo.com/v1"
KIMI_API_BASE = "https://api.kimi.com/coding"
GLM_API_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"
# ARK Coding is intentionally deprecated. Do not route new mini-swe-agent models
# through https://ark.cn-beijing.volces.com/api/coding/v3.


DEEPSEEK_MODELS = {"deepseek-v4-pro"}
MINMAX_MODELS = {"minimax-m2.7"}
MIMO_MODELS = {"mimo-v2.5-pro", "mimo-2.5-pro"}
KIMI_MODELS = {"kimi-k2.6"}
GLM_MODELS = {"glm-5.1"}


def _anthropic_thinking_model_config() -> list[str]:
    """Enable vendor thinking for Anthropic-compatible coding endpoints.

    Keep reasoning_effort separately for benchmark consistency; unsupported
    params are dropped by the base mini-swe-agent config.
    """
    return [
        'model.model_kwargs.allowed_openai_params=["thinking","reasoning_effort"]',
        "model.model_kwargs.thinking.type=enabled",
    ]


def _openai_thinking_model_config() -> list[str]:
    """Enable vendor thinking for OpenAI-compatible endpoints."""
    return [
        'model.model_kwargs.allowed_openai_params=["reasoning_effort"]',
        "model.model_kwargs.extra_body.thinking.type=enabled",
    ]


def _kimi_thinking_model_config() -> list[str]:
    """Enable Kimi Code thinking mode."""
    return [
        'model.model_kwargs.allowed_openai_params=["thinking","reasoning_effort","default_thinking"]',
        "model.model_kwargs.default_thinking=true",
        "model.model_kwargs.thinking.type=enabled",
    ]


@dataclass(slots=True)
class MiniConfig:
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    thinking_effort: str = "high"
    timeout_seconds: int = 600
    agent_edit_mode: Literal["workspace-write", "danger-full-access"] = "danger-full-access"
    conda_env: str = "minisweagent"
    mini_python: Path | None = None
    api_base: str | None = None
    max_tokens: int = 4096
    repair_environment: Literal["local", "docker"] = "local"
    docker_container_name: str | None = None
    docker_workdir: str = "/workspace/eval_repo"
    swebench_config: Path = (
        ROOT
        / "src"
        / "empirical_agents"
        / "mini_swe_agent"
        / "org_src"
        / "minisweagent"
        / "config"
        / "benchmarks"
        / "swebench.yaml"
    )
    override_config: Path = (
        ROOT
        / "src"
        / "empirical_agents"
        / "mini_swe_agent"
        / "run_agent_fixing"
        / "blind_repair.yaml"
    )


def run_mini_fixing(
    *,
    prompt: str,
    candidate_dir: Path,
    trajectory_root: Path,
    logger,
    config: MiniConfig,
) -> AgentRunResult:
    resolved = resolve_mini_model_config(config)
    prompt = _augment_prompt_for_model(
        prompt=prompt,
        display_model=config.model,
        repair_environment=config.repair_environment,
        docker_workdir=config.docker_workdir,
    )
    api_key = (config.api_key or os.environ.get(str(resolved["api_key_env"]), "")).strip()
    if not api_key:
        raise RuntimeError(
            f"Missing mini_swe_agent API key. Checked --api-key and {resolved['api_key_env']}."
        )

    docker_container_name = config.docker_container_name
    docker_workdir = config.docker_workdir
    if config.repair_environment == "docker":
        docker_container_name = docker_container_name or os.environ.get("MSWEA_REPAIR_DOCKER_CONTAINER")
        docker_workdir = os.environ.get("MSWEA_REPAIR_DOCKER_WORKDIR", docker_workdir)
        if not docker_container_name:
            raise RuntimeError("Missing docker repair container name for mini_swe_agent docker environment.")

    mini_python = resolve_mini_python(config.conda_env, config.mini_python)
    runner = SubprocessMiniRunner(
        RunnerSettings(
            command=build_mini_command(
                mini_python=mini_python,
                swebench_config=config.swebench_config,
                override_config=config.override_config,
                model=str(resolved["model_name"]),
                api_base=str(resolved["api_base"]),
                max_tokens=config.max_tokens,
                thinking_effort=str(resolved["reasoning_effort"]),
                extra_model_config=list(resolved["extra_model_config"]),
                repair_environment=config.repair_environment,
                docker_container_name=docker_container_name,
                docker_workdir=docker_workdir,
            ),
            timeout_seconds=config.timeout_seconds,
            env=_build_model_env(
                api_key_env=str(resolved["api_key_env"]),
                api_key=api_key,
                model_name=str(resolved["model_name"]),
                org_src_root=ROOT / "src" / "empirical_agents" / "mini_swe_agent" / "org_src",
            ),
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
        "mini_run "
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
            "effective_extra_model_config": resolved["extra_model_config"],
            "effective_repair_environment": config.repair_environment,
            "effective_docker_container_name": docker_container_name,
            "effective_docker_workdir": docker_workdir,
        },
    )


def _augment_prompt_for_model(
    *,
    prompt: str,
    display_model: str,
    repair_environment: str,
    docker_workdir: str,
) -> str:
    """Add model-specific guardrails without changing the benchmark record.

    DeepSeek has repeatedly edited /workspace/repo inside Docker and submitted
    without changing /workspace/eval_repo. Keep this guard model-scoped so other
    agents remain comparable with their existing prompt.
    """
    lowered = display_model.strip().lower()
    if "/" in lowered:
        lowered = lowered.split("/")[-1]
    if lowered not in DEEPSEEK_MODELS or repair_environment != "docker":
        return prompt

    workspace = docker_workdir or "/workspace/eval_repo"
    guard = f"""
This run is evaluated only from `{workspace}`.
Hard requirements:
- Treat `{workspace}` as the only repository and the only valid working tree.
- If you ever find yourself outside `{workspace}`, immediately `cd {workspace}` before continuing.
- All source-code edits must happen under `{workspace}`. Any change outside this directory is invalid and counts as a failed repair.
"""
    return f"{guard}\n\n{prompt}"


def build_mini_command(
    *,
    mini_python: Path,
    swebench_config: Path,
    override_config: Path,
    model: str,
    api_base: str,
    max_tokens: int,
    thinking_effort: str,
    extra_model_config: list[str],
    repair_environment: str = "local",
    docker_container_name: str | None = None,
    docker_workdir: str = "/workspace/eval_repo",
) -> list[str]:
    command = [
        str(mini_python),
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
        str(swebench_config),
        "-c",
        str(override_config),
        "-c",
        f"model.model_kwargs.api_base={api_base}",
        "-c",
        f"model.model_kwargs.max_tokens={max_tokens}",
        "-c",
        "model.cost_tracking=ignore_errors",
    ]
    if thinking_effort:
        command.extend(["-c", f"model.model_kwargs.reasoning_effort={thinking_effort}"])
    for item in extra_model_config:
        command.extend(["-c", item])
    if repair_environment == "docker":
        if not docker_container_name:
            raise ValueError("docker repair environment requires docker_container_name.")
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
    return command


def resolve_mini_python(conda_env: str, explicit_python: Path | None) -> Path:
    if explicit_python is not None:
        resolved = explicit_python.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"mini python does not exist: {resolved}")
        return resolved

    candidates = [
        Path.home() / ".conda" / "envs" / conda_env / "python.exe",
        ROOT / ".conda" / "envs" / conda_env / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Could not resolve python.exe for conda env `{conda_env}`. Pass it explicitly with --mini-python."
    )


def _build_pythonpath(org_src_root: Path) -> str:
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        return f"{org_src_root}{os.pathsep}{existing}"
    return str(org_src_root)


def _build_model_env(*, api_key_env: str, api_key: str, model_name: str, org_src_root: Path) -> dict[str, str]:
    env = {
        api_key_env: api_key,
        "MSWEA_CONFIGURED": "true",
        "MSWEA_SILENT_STARTUP": "1",
        "PYTHONUTF8": "1",
        "PYTHONPATH": _build_pythonpath(org_src_root),
    }
    provider = model_name.split("/", 1)[0].lower() if "/" in model_name else ""
    if provider == "openai" and api_key_env == "OPENAI_API_KEY":
        env.setdefault("OPENAI_API_KEY", api_key)
    elif provider == "anthropic" and api_key_env == "ANTHROPIC_API_KEY":
        env.setdefault("ANTHROPIC_API_KEY", api_key)
    return env


def resolve_mini_model_config(config: MiniConfig) -> dict[str, object]:
    display_model = config.model.strip()
    if not display_model:
        raise ValueError("mini-swe-agent requires --model; no default model is configured.")
    lowered = display_model.lower()
    if "/" in lowered:
        lowered = lowered.split("/")[-1]

    if lowered in MINMAX_MODELS:
        return {
            "model_name": f"anthropic/{display_model.split('/')[-1]}",
            "api_base": config.api_base or MINIMAX_API_BASE,
            "api_key_env": config.api_key_env or "MINIMAX_API_KEY",
            "reasoning_effort": config.thinking_effort,
            "extra_model_config": _anthropic_thinking_model_config(),
        }

    if lowered in MIMO_MODELS:
        return {
            "model_name": "openai/mimo-v2.5-pro",
            "api_base": config.api_base or MIMO_API_BASE,
            "api_key_env": config.api_key_env or "MIMO_API_KEY",
            "reasoning_effort": config.thinking_effort,
            "extra_model_config": _openai_thinking_model_config(),
        }

    if lowered in KIMI_MODELS:
        return {
            "model_name": "anthropic/kimi-for-coding",
            "api_base": config.api_base or KIMI_API_BASE,
            "api_key_env": config.api_key_env or "KIMI_API_KEY",
            "reasoning_effort": config.thinking_effort,
            "extra_model_config": _kimi_thinking_model_config(),
        }

    if lowered in GLM_MODELS:
        return {
            "model_name": "openai/GLM-5.1",
            "api_base": config.api_base or GLM_API_BASE,
            "api_key_env": config.api_key_env or "GLM_API_KEY",
            "reasoning_effort": config.thinking_effort,
            "extra_model_config": _openai_thinking_model_config(),
        }

    if lowered in DEEPSEEK_MODELS:
        return {
            "model_name": f"openai/{display_model.split('/')[-1]}",
            "api_base": config.api_base or DEEPSEEK_API_BASE,
            "api_key_env": config.api_key_env or "DEEPSEEK_API_KEY",
            "reasoning_effort": config.thinking_effort,
            "extra_model_config": _openai_thinking_model_config(),
        }

    raise ValueError(
        f"Unsupported mini-swe-agent model `{config.model}`. Supported aliases: "
        f"{sorted(MINMAX_MODELS | MIMO_MODELS | KIMI_MODELS | GLM_MODELS | DEEPSEEK_MODELS)}. "
        "Pass an explicit supported alias instead of relying on a default."
    )

