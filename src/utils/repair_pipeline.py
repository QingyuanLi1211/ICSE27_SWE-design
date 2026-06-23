"""Shared repair orchestration for worktree prep, agent run, and patch extraction."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .benchmark import BenchmarkRecord
from .logs import StepLogger
from .output_layout import repair_patch_path, repair_status_path, step_log_path, trajectory_dir
from .process import rmtree_force
from .prompting import build_design_issue_prompt
from .repair_steps import (
    DiffOutcome,
    DockerRepairWorkspace,
    build_docker_workspace_for_record,
    build_worktree_for_record,
    cleanup_docker_workspace,
    diff_agent_patch_for_record,
    diff_docker_agent_patch_for_record,
)
from .status import new_repair_status, write_status
from .trajectory import normalize_trajectory


@dataclass(slots=True)
class AgentRunResult:
    completed: bool
    raw_paths: dict[str, Path | None] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def run_repair_for_record(
    *,
    agent_name: str,
    record: BenchmarkRecord,
    bundle_root: Path,
    output_root: Path,
    runner,
    execute_agent: Callable[[BenchmarkRecord, str, Path, Path, StepLogger], AgentRunResult],
    repair_environment: str = "local",
    prompt_builder: Callable[[BenchmarkRecord], str] | None = None,
) -> dict[str, Any]:
    patch_path = repair_patch_path(agent_name, record.repo_key, record.instance_id, output_root)
    status_path = repair_status_path(agent_name, record.repo_key, record.instance_id, output_root)
    trajectory_root = trajectory_dir(agent_name, record.repo_key, record.instance_id, output_root)
    step1_log_path = step_log_path(agent_name, record.repo_key, record.instance_id, "step1", output_root)
    lock_path = step1_log_path.with_suffix(".lock")

    _acquire_instance_lock(lock_path)
    temp_root = _make_short_temp_root()
    preserve_temp_root = False
    preserved_temp_root: str | None = None
    try:
        _clear_canonical_outputs(
            trajectory_root=trajectory_root,
            patch_path=patch_path,
            status_path=status_path,
            step1_log_path=step1_log_path,
        )
        trajectory_root.mkdir(parents=True, exist_ok=True)
        logger = StepLogger(step1_log_path)
        repair_status = new_repair_status()
        raw_paths: dict[str, Path | None] = {}
        agent_summary: dict[str, Any] = {}

        if prompt_builder is not None:
            prompt = prompt_builder(record)
        else:
            prompt = build_design_issue_prompt(
                repo=record.repo,
                problem_statement=record.problem_statement,
                filename=record.filename,
            )
        prompt_path = trajectory_root / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        logger.log(f"instance_id={record.instance_id}")
        logger.log(f"source_jsonl={record.source_jsonl}")
        logger.log(f"prompt_path={prompt_path}")

        patch_text = ""
        pristine_dir: Path | None = None
        candidate_dir: Path | None = None
        docker_workspace: DockerRepairWorkspace | None = None
        diff_outcome = DiffOutcome(patch_text="", worktree_changed=False, patch_generated=False)

        if repair_environment == "docker":
            docker_workspace = build_docker_workspace_for_record(
                record=record,
                bundle_root=bundle_root,
                repair_status=repair_status,
                logger=logger,
                runner=runner,
            )
            candidate_dir = temp_root / "host_cwd"
            candidate_dir.mkdir(parents=True, exist_ok=True)
        else:
            pristine_dir, candidate_dir = build_worktree_for_record(
                record=record,
                bundle_root=bundle_root,
                work_root=temp_root / "worktree",
                repair_status=repair_status,
                logger=logger,
                runner=runner,
            )

        if repair_status["workspace_writable"] is True and candidate_dir is not None:
            try:
                previous_container = os.environ.get("MSWEA_REPAIR_DOCKER_CONTAINER")
                previous_workdir = os.environ.get("MSWEA_REPAIR_DOCKER_WORKDIR")
                if docker_workspace is not None:
                    os.environ["MSWEA_REPAIR_DOCKER_CONTAINER"] = docker_workspace.container_name
                    os.environ["MSWEA_REPAIR_DOCKER_WORKDIR"] = docker_workspace.candidate_dir
                agent_result = execute_agent(record, prompt, candidate_dir, trajectory_root, logger)
                repair_status["agent_run_completed"] = agent_result.completed
                raw_paths = agent_result.raw_paths
                agent_summary = agent_result.summary
                if agent_result.summary:
                    logger.log(f"agent_summary={agent_result.summary}")
            except Exception as exc:  # noqa: BLE001
                repair_status["agent_run_completed"] = False
                logger.log(f"agent_run_completed=False error={exc}")
            finally:
                if docker_workspace is not None:
                    if previous_container is None:
                        os.environ.pop("MSWEA_REPAIR_DOCKER_CONTAINER", None)
                    else:
                        os.environ["MSWEA_REPAIR_DOCKER_CONTAINER"] = previous_container
                    if previous_workdir is None:
                        os.environ.pop("MSWEA_REPAIR_DOCKER_WORKDIR", None)
                    else:
                        os.environ["MSWEA_REPAIR_DOCKER_WORKDIR"] = previous_workdir
        else:
            logger.log("skip_agent_run because workspace_writable is not True.")

        if docker_workspace is not None:
            diff_outcome = diff_docker_agent_patch_for_record(
                record=record,
                workspace=docker_workspace,
                patch_path=patch_path,
                logger=logger,
                runner=runner,
            )
        else:
            diff_outcome = diff_agent_patch_for_record(
                record=record,
                pristine_dir=pristine_dir,
                candidate_dir=candidate_dir,
                patch_path=patch_path,
                logger=logger,
                runner=runner,
            )
        patch_text = diff_outcome.patch_text

        if (
            repair_status["image_available"] is True
            and repair_status["workspace_prepared"] is True
            and repair_status["workspace_writable"] is True
        ):
            repair_status["agent_modified_worktree"] = diff_outcome.worktree_changed
            repair_status["agent_patch_generated"] = diff_outcome.patch_generated

        if diff_outcome.worktree_changed and not diff_outcome.patch_generated:
            preserve_temp_root = True
            preserved_temp_root = str(temp_root)
            logger.log(
                "preserve_temp_root=True "
                f"root={temp_root} "
                "reason=worktree_changed_without_patch "
                f"diff_error={diff_outcome.error}"
            )

        write_status(status_path, repair_status)
        logger.log(f"repair_status={repair_status}")

        if preserve_temp_root:
            logger.log("normalized_traj.jsonl skipped because patch extraction failed after worktree changes.")
        else:
            normalize_trajectory(
                agent_name=agent_name,
                record=record,
                raw_paths=raw_paths,
                worktree_root=candidate_dir,
                prompt_path=prompt_path,
                output_path=trajectory_root / "normalized_traj.jsonl",
                repair_status=repair_status,
                patch_text=patch_text,
                patch_path=patch_path,
            )
            logger.log("normalized_traj.jsonl generated.")

        result = {
            "instance_id": record.instance_id,
            "agent_name": agent_name,
            "patch_path": str(patch_path),
            "status_path": str(status_path),
            "trajectory_root": str(trajectory_root),
            "agent_patch_generated": repair_status["agent_patch_generated"],
        }
        if agent_summary:
            result["agent_summary"] = agent_summary
        if preserved_temp_root is not None:
            result["preserved_temp_root"] = preserved_temp_root
        return result
    finally:
        if preserve_temp_root:
            pass
        else:
            rmtree_force(temp_root)
        cleanup_docker_workspace(locals().get("docker_workspace"), runner=runner)
        _release_instance_lock(lock_path)


def _make_short_temp_root() -> Path:
    base = Path(tempfile.gettempdir()) / "sdb"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="r-", dir=base))


def _clear_canonical_outputs(
    *,
    trajectory_root: Path,
    patch_path: Path,
    status_path: Path,
    step1_log_path: Path,
) -> None:
    if trajectory_root.exists():
        rmtree_force(trajectory_root)
    for path in (patch_path, status_path, step1_log_path):
        if path.exists():
            path.unlink()


def _acquire_instance_lock(lock_path: Path, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock_path.mkdir(parents=True, exist_ok=False)
            (lock_path / "pid.txt").write_text(str(os.getpid()), encoding="utf-8")
            return
        except FileExistsError:
            owner_pid = _read_lock_owner_pid(lock_path)
            if owner_pid is not None and not _pid_is_running(owner_pid):
                rmtree_force(lock_path)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Instance lock is busy: {lock_path}")
            time.sleep(0.2)


def _release_instance_lock(lock_path: Path) -> None:
    rmtree_force(lock_path)


def _read_lock_owner_pid(lock_path: Path) -> int | None:
    pid_path = lock_path / "pid.txt"
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
