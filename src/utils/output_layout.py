"""统一定义 output_data 下的目录布局。"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = REPO_ROOT / "output_data"


def agent_output_root(agent_name: str, output_root: Path | None = None) -> Path:
    root = output_root or OUTPUT_ROOT
    return root / agent_name


def repair_results_root(agent_name: str, output_root: Path | None = None) -> Path:
    return agent_output_root(agent_name, output_root) / "repair_results"


def repair_patch_path(agent_name: str, repo_key: str, instance_id: str, output_root: Path | None = None) -> Path:
    return repair_results_root(agent_name, output_root) / "agent_patch" / repo_key / f"{instance_id}.diff"


def repair_status_path(agent_name: str, repo_key: str, instance_id: str, output_root: Path | None = None) -> Path:
    return repair_results_root(agent_name, output_root) / "patch_status" / repo_key / f"{instance_id}.json"


def trajectory_dir(agent_name: str, repo_key: str, instance_id: str, output_root: Path | None = None) -> Path:
    return agent_output_root(agent_name, output_root) / "trajectory" / repo_key / instance_id


def logs_dir(agent_name: str, repo_key: str, instance_id: str, output_root: Path | None = None) -> Path:
    return agent_output_root(agent_name, output_root) / "logs" / repo_key / instance_id


def step_log_path(
    agent_name: str,
    repo_key: str,
    instance_id: str,
    step_name: str,
    output_root: Path | None = None,
) -> Path:
    return logs_dir(agent_name, repo_key, instance_id, output_root) / f"{step_name}.log"


def eval_result_path(agent_name: str, repo_key: str, instance_id: str, output_root: Path | None = None) -> Path:
    return agent_output_root(agent_name, output_root) / "eval_results" / repo_key / f"{instance_id}.json"


def eval_infra_cache_path(repo_key: str, instance_id: str, output_root: Path | None = None) -> Path:
    # Infrastructure validity is shared across agents and output batches.
    # Keep it at the repository root instead of under a specific output_root.
    return REPO_ROOT / "eval_infra_cache" / repo_key / f"{instance_id}.json"


def eval_infra_cache_log_path(repo_key: str, instance_id: str) -> Path:
    return REPO_ROOT / "eval_infra_cache" / "eval_infra_cache_logs" / repo_key / instance_id / "step2.log"
