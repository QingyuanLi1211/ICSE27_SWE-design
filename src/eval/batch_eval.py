"""Batch launcher for infrastructure and agent patch evaluation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.utils.benchmark import BenchmarkRecord, load_records
from src.utils.output_layout import eval_infra_cache_path, eval_result_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "output_data_batch"
DEFAULT_BATCH_OUTPUT_ROOT = DEFAULT_OUTPUT_ROOT / "batch_logs"

EVAL_MODES = ("infrastructure-only", "agent-only", "full")


@dataclass(slots=True)
class EvalJob:
    record: BenchmarkRecord
    mode: str
    agent_name: str | None


@dataclass(slots=True)
class RunningEval:
    job: EvalJob
    command: list[str]
    process: subprocess.Popen[bytes]
    stdout_handle: TextIO
    stderr_handle: TextIO
    stdout_path: Path
    stderr_path: Path
    command_path: Path
    started_at: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate benchmark infrastructure and saved agent patches in parallel.")
    parser.add_argument("--jsonl-list", nargs="+", type=Path, required=True)
    parser.add_argument("--mode", choices=EVAL_MODES, default="full")
    parser.add_argument("--projects", nargs="+")
    parser.add_argument("--agents", nargs="+")
    parser.add_argument("--agent-name")
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--agent-patch-root", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--batch-output-root", type=Path, default=None)
    parser.add_argument("--batch-log-root", type=Path, default=DEFAULT_BATCH_OUTPUT_ROOT)
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument("--resume", choices=["none", "strict"], default="none")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_parallel < 1:
        raise ValueError("--max-parallel must be >= 1")

    args.output_root = args.output_root.resolve()
    args.bundle_root = args.bundle_root.resolve()
    args.batch_log_root = args.batch_log_root.resolve()
    if args.batch_output_root is not None:
        args.batch_output_root = args.batch_output_root.resolve()
    if args.agent_patch_root is not None:
        args.agent_patch_root = args.agent_patch_root.resolve()

    records = _filter_records_by_project(load_records(args.jsonl_list), args.projects)
    agents = _resolve_agents(args)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_output_root = args.batch_output_root or args.batch_log_root
    run_log_root = batch_output_root / _batch_run_name(args.mode, agents) / timestamp
    run_log_root.mkdir(parents=True, exist_ok=True)

    if args.mode == "full":
        return _run_full_mode(args=args, records=records, agents=agents, run_log_root=run_log_root)

    jobs = _build_jobs(records=records, mode=args.mode, agents=agents)
    total_jobs = len(jobs)
    jobs, skipped = _filter_jobs_for_resume(jobs=jobs, output_root=args.output_root, resume_mode=args.resume)
    commands = [_build_eval_command(python_executable=args.python_executable, job=job, args=args) for job in jobs]
    _print_plan_summary(
        mode=args.mode,
        projects=[record.repo_key for record in records],
        agents=agents,
        total_jobs=total_jobs,
        skipped=len(skipped),
        to_eval=len(jobs),
        resume=args.resume,
        max_parallel=args.max_parallel,
        batch_output_root=run_log_root,
        dry_run=args.dry_run,
    )
    if skipped:
        print(json.dumps({"resume_skipped": [_job_key(job) for job in skipped]}, ensure_ascii=False))

    if args.dry_run:
        if not commands:
            print("No jobs to evaluate.")
        for command in commands:
            print(subprocess.list2cmdline(command))
        return 0

    if not jobs:
        print("No jobs to evaluate.")
        return 0

    summaries = _run_eval_commands(
        commands=commands,
        jobs=jobs,
        max_parallel=args.max_parallel,
        run_log_root=run_log_root,
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item["returncode"] == 0 for item in summaries) else 1


def _print_plan_summary(
    *,
    mode: str,
    projects: list[str],
    agents: list[str],
    total_jobs: int,
    skipped: int,
    to_eval: int,
    resume: str,
    max_parallel: int,
    batch_output_root: Path,
    dry_run: bool,
    extra: dict[str, object] | None = None,
) -> None:
    unique_projects = list(dict.fromkeys(projects))
    summary: dict[str, object] = {
        "dry_run": dry_run,
        "mode": mode,
        "projects": unique_projects,
        "agents": agents,
        "resume": resume,
        "total_jobs": total_jobs,
        "skipped_existing": skipped,
        "to_evaluate": to_eval,
        "max_parallel": max_parallel,
        "batch_output_root": str(batch_output_root),
    }
    if extra:
        summary.update(extra)
    print(json.dumps(summary, ensure_ascii=False))


def _resolve_agents(args: argparse.Namespace) -> list[str]:
    if args.mode == "infrastructure-only":
        return []
    agents: list[str] = []
    if args.agents:
        agents.extend(args.agents)
    if args.agent_name:
        agents.append(args.agent_name)
    agents = list(dict.fromkeys(agents))
    if not agents:
        raise ValueError("--agents or --agent-name is required for agent-only/full eval modes.")
    return agents


def _filter_records_by_project(records: list[BenchmarkRecord], projects: list[str] | None) -> list[BenchmarkRecord]:
    if not projects:
        return records
    filtered: list[BenchmarkRecord] = []
    for project in projects:
        filtered.extend(record for record in records if record.repo_key == project)
    matched = {record.repo_key for record in filtered}
    missing = [project for project in projects if project not in matched]
    if missing:
        raise ValueError(f"--projects contains project(s) not found in the supplied JSONL files: {missing}")
    return filtered


def _build_jobs(*, records: list[BenchmarkRecord], mode: str, agents: list[str]) -> list[EvalJob]:
    if mode == "infrastructure-only":
        return [EvalJob(record=record, mode=mode, agent_name=None) for record in records]
    jobs: list[EvalJob] = []
    for project_records in _records_grouped_by_project(records):
        for agent in agents:
            for record in project_records:
                jobs.append(EvalJob(record=record, mode=mode, agent_name=agent))
    return jobs


def _records_grouped_by_project(records: list[BenchmarkRecord]) -> list[list[BenchmarkRecord]]:
    grouped: dict[str, list[BenchmarkRecord]] = {}
    order: list[str] = []
    for record in records:
        if record.repo_key not in grouped:
            grouped[record.repo_key] = []
            order.append(record.repo_key)
        grouped[record.repo_key].append(record)
    return [grouped[project] for project in order]


def _run_full_mode(
    *,
    args: argparse.Namespace,
    records: list[BenchmarkRecord],
    agents: list[str],
    run_log_root: Path,
) -> int:
    infra_jobs = _build_jobs(records=records, mode="infrastructure-only", agents=[])
    agent_jobs = _build_jobs(records=records, mode="agent-only", agents=agents)
    infra_jobs_to_run, infra_skipped = _filter_jobs_for_resume(
        jobs=infra_jobs,
        output_root=args.output_root,
        resume_mode=args.resume,
    )
    agent_jobs_for_dry_run, skipped_for_dry_run = _filter_jobs_for_resume(
        jobs=agent_jobs,
        output_root=args.output_root,
        resume_mode=args.resume,
    )
    infra_commands = [
        _build_eval_command(python_executable=args.python_executable, job=job, args=args)
        for job in infra_jobs_to_run
    ]
    agent_commands = [
        _build_eval_command(python_executable=args.python_executable, job=job, args=args)
        for job in agent_jobs_for_dry_run
    ]
    _print_plan_summary(
        mode=args.mode,
        projects=[record.repo_key for record in records],
        agents=agents,
        total_jobs=len(infra_jobs) + len(agent_jobs),
        skipped=len(infra_skipped) + len(skipped_for_dry_run),
        to_eval=len(infra_jobs_to_run) + len(agent_jobs_for_dry_run),
        resume=args.resume,
        max_parallel=args.max_parallel,
        batch_output_root=run_log_root,
        dry_run=args.dry_run,
        extra={
            "infra_total": len(infra_jobs),
            "infra_skipped_existing": len(infra_skipped),
            "infra_to_evaluate": len(infra_jobs_to_run),
            "agent_total": len(agent_jobs),
            "agent_skipped_existing_before_infra": len(skipped_for_dry_run),
            "agent_to_evaluate_before_infra": len(agent_jobs_for_dry_run),
            "full_mode_note": "infrastructure is refreshed or resumed once per instance before any agent eval starts",
        },
    )

    if args.dry_run:
        if not infra_commands and not agent_commands:
            print("No jobs to evaluate.")
        for command in infra_commands:
            print(subprocess.list2cmdline(command))
        for command in agent_commands:
            print(subprocess.list2cmdline(command))
        return 0

    infra_summaries = _run_eval_commands(
        commands=infra_commands,
        jobs=infra_jobs_to_run,
        max_parallel=args.max_parallel,
        run_log_root=run_log_root / "phase1_infrastructure",
    )
    valid_records = [record for record in records if _infra_cache_valid_for_record(record=record, output_root=args.output_root)]
    invalid_records = [record.instance_id for record in records if record not in valid_records]
    if invalid_records:
        print(json.dumps({"infrastructure_invalid_instances": invalid_records}, ensure_ascii=False))

    agent_jobs = [job for job in agent_jobs if job.record in valid_records]
    agent_jobs, skipped = _filter_jobs_for_resume(
        jobs=agent_jobs,
        output_root=args.output_root,
        resume_mode=args.resume,
    )
    print(
        json.dumps(
            {
                "phase": "agent",
                "valid_infrastructure_instances": len(valid_records),
                "agent_skipped_existing_after_infra": len(skipped),
                "agent_to_evaluate_after_infra": len(agent_jobs),
            },
            ensure_ascii=False,
        )
    )
    if skipped:
        print(json.dumps({"resume_skipped": [_job_key(job) for job in skipped]}, ensure_ascii=False))
    agent_commands = [_build_eval_command(python_executable=args.python_executable, job=job, args=args) for job in agent_jobs]
    agent_summaries = _run_eval_commands(
        commands=agent_commands,
        jobs=agent_jobs,
        max_parallel=args.max_parallel,
        run_log_root=run_log_root / "phase2_agent",
    )
    summaries = {"infrastructure": infra_summaries, "agent": agent_summaries}
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    all_summaries = infra_summaries + agent_summaries
    return 0 if all(item["returncode"] == 0 for item in all_summaries) else 1


def _build_eval_command(*, python_executable: str, job: EvalJob, args: argparse.Namespace) -> list[str]:
    command = [
        python_executable,
        str(ROOT / "src" / "eval" / "eval.py"),
        "--jsonl-list",
        *[str(path) for path in args.jsonl_list],
        "--instance-id",
        job.record.instance_id,
        "--mode",
        job.mode,
        "--bundle-root",
        str(args.bundle_root),
        "--output-root",
        str(args.output_root),
    ]
    if job.agent_name is not None:
        command.extend(["--agent-name", job.agent_name])
    if args.agent_patch_root is not None:
        command.extend(["--agent-patch-root", str(args.agent_patch_root)])
    return command


def _filter_jobs_for_resume(
    *,
    jobs: list[EvalJob],
    output_root: Path,
    resume_mode: str,
) -> tuple[list[EvalJob], list[EvalJob]]:
    if resume_mode == "none":
        return jobs, []

    runnable: list[EvalJob] = []
    skipped: list[EvalJob] = []
    for job in jobs:
        if _should_skip_job(job=job, output_root=output_root, resume_mode=resume_mode):
            skipped.append(job)
        else:
            runnable.append(job)
    return runnable, skipped


def _should_skip_job(*, job: EvalJob, output_root: Path, resume_mode: str) -> bool:
    if resume_mode == "none":
        return False
    if job.mode == "infrastructure-only":
        status_file = eval_infra_cache_path(job.record.repo_key, job.record.instance_id, output_root)
        payload = _read_json(status_file)
        return payload.get("eval_infrastructure_valid") is True

    if job.agent_name is None:
        return False
    infra_status_file = eval_infra_cache_path(job.record.repo_key, job.record.instance_id, output_root)
    infra_payload = _read_json(infra_status_file)
    if infra_payload.get("eval_infrastructure_valid") is not True:
        return False

    status_file = eval_result_path(job.agent_name, job.record.repo_key, job.record.instance_id, output_root)
    payload = _read_json(status_file)
    if not payload:
        return False
    return isinstance(payload.get("agent_patch_passed"), bool)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _infra_cache_valid_for_record(*, record: BenchmarkRecord, output_root: Path) -> bool:
    payload = _read_json(eval_infra_cache_path(record.repo_key, record.instance_id, output_root))
    return payload.get("eval_infrastructure_valid") is True


def _run_eval_commands(
    *,
    commands: list[list[str]],
    jobs: list[EvalJob],
    max_parallel: int,
    run_log_root: Path,
) -> list[dict[str, object]]:
    pending = deque(zip(jobs, commands, strict=True))
    running: list[RunningEval] = []
    summaries: list[dict[str, object]] = []
    try:
        while pending or running:
            while pending and len(running) < max_parallel:
                job, command = pending.popleft()
                running.append(_start_eval(job=job, command=command, run_log_root=run_log_root))

            finished: list[RunningEval] = []
            for running_job in running:
                returncode = running_job.process.poll()
                if returncode is None:
                    continue
                finished.append(running_job)
                summaries.append(_finish_eval(job=running_job, returncode=returncode))

            if finished:
                running = [running_job for running_job in running if running_job not in finished]
                continue
            time.sleep(1.0)
    except KeyboardInterrupt:
        for running_job in running:
            _terminate_process(running_job.process)
            _close_handles(running_job)
        raise
    return summaries


def _start_eval(*, job: EvalJob, command: list[str], run_log_root: Path) -> RunningEval:
    job_dir = run_log_root / (job.agent_name or "infrastructure") / job.record.repo_key / job.record.instance_id
    job_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    command_path = job_dir / "command.txt"
    command_path.write_text(subprocess.list2cmdline(command), encoding="utf-8")
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=(sys.platform != "win32"),
    )
    print(f"[start] {_job_key(job)} pid={process.pid}")
    return RunningEval(
        job=job,
        command=command,
        process=process,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        command_path=command_path,
        started_at=time.time(),
    )


def _finish_eval(*, job: RunningEval, returncode: int) -> dict[str, object]:
    _close_handles(job)
    elapsed_seconds = round(time.time() - job.started_at, 2)
    status = "ok" if returncode == 0 else "failed"
    print(f"[{status}] {_job_key(job.job)} returncode={returncode} elapsed={elapsed_seconds}s")
    return {
        "mode": job.job.mode,
        "agent_name": job.job.agent_name,
        "instance_id": job.job.record.instance_id,
        "repo": job.job.record.repo,
        "returncode": returncode,
        "elapsed_seconds": elapsed_seconds,
        "stdout_path": str(job.stdout_path),
        "stderr_path": str(job.stderr_path),
        "command_path": str(job.command_path),
    }


def _close_handles(job: RunningEval) -> None:
    job.stdout_handle.close()
    job.stderr_handle.close()


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, text=True, check=False)
    else:
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _batch_run_name(mode: str, agents: list[str]) -> str:
    if mode == "infrastructure-only":
        return "eval_infrastructure"
    if len(agents) == 1:
        return f"eval_{agents[0]}"
    return "eval_multi_agent"


def _job_key(job: EvalJob) -> str:
    if job.agent_name is None:
        return f"infrastructure:{job.record.instance_id}"
    return f"{job.agent_name}:{job.record.instance_id}"


if __name__ == "__main__":
    raise SystemExit(main())

