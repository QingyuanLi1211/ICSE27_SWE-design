"""Shared batch launcher for agent repair entrypoints.

The batch layer intentionally starts one Python subprocess per instance.  The
single-instance entrypoints keep owning the real repair logic; this file only
expands benchmark records, limits concurrency, and records child process logs.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from src.utils.agent_keys import agent_model_key
from src.utils.benchmark import BenchmarkRecord, load_records
from src.utils.output_layout import repair_status_path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = ROOT / "output_data_batch"
DEFAULT_BATCH_OUTPUT_ROOT = DEFAULT_OUTPUT_ROOT / "batch_logs"


@dataclass(frozen=True, slots=True)
class BatchRunnerConfig:
    agent_family: str
    agent_script: Path
    default_max_parallel: int = 4
    description: str = "Run agent repair in parallel subprocesses."
    record_filter: Callable[[list[BenchmarkRecord], argparse.Namespace], list[BenchmarkRecord]] | None = None
    batch_log_key_resolver: Callable[[argparse.Namespace, list[str]], str] | None = None
    circuit_breaker_returncodes: tuple[int, ...] = ()


@dataclass(slots=True)
class RunningJob:
    record: BenchmarkRecord
    command: list[str]
    process: subprocess.Popen[bytes]
    stdout_handle: TextIO
    stderr_handle: TextIO
    stdout_path: Path
    stderr_path: Path
    command_path: Path
    started_at: float


def run_batch(config: BatchRunnerConfig, argv: list[str] | None = None) -> int:
    args, passthrough_args = _parse_args(config, argv)
    if args.max_parallel < 1:
        raise ValueError("--max-parallel must be >= 1")
    api_key_error = _validate_api_key_args(passthrough_args)
    if api_key_error is not None:
        print(api_key_error, file=sys.stderr)
        return 2
    args.output_root = args.output_root.resolve()
    if args.batch_output_root is not None:
        args.batch_output_root = args.batch_output_root.resolve()
    args.batch_log_root = args.batch_log_root.resolve()
    batch_output_root = args.batch_output_root or args.batch_log_root

    output_key = _resolve_output_key(config, args, passthrough_args)
    records = load_records(args.jsonl_list)
    if config.record_filter is not None:
        records = config.record_filter(records, args)
    total_records = len(records)
    records, skipped_records = _filter_records_for_resume(
        records=records,
        resume_mode=args.resume,
        agent_name=output_key,
        output_root=args.output_root,
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_log_key = (
        config.batch_log_key_resolver(args, passthrough_args)
        if config.batch_log_key_resolver is not None
        else config.agent_family
    )
    run_log_root = batch_output_root / batch_log_key / timestamp
    run_log_root.mkdir(parents=True, exist_ok=True)

    commands = [
        _build_single_command(
            python_executable=args.python_executable,
            agent_script=config.agent_script,
            jsonl_list=args.jsonl_list,
            record=record,
            output_root=args.output_root,
            passthrough_args=passthrough_args,
        )
        for record in records
    ]

    if args.dry_run:
        for command in commands:
            print(_format_command(command))
        return 0

    print(
        f"Total records: {total_records}; "
        f"resume skipped: {len(skipped_records)}; "
        f"remaining: {len(records)}."
    )
    print(
        json.dumps(
            {
                "agent_family": config.agent_family,
                "agent_name": output_key,
                "batch_log_key": batch_log_key,
                "total": len(records),
                "skipped": len(skipped_records),
                "resume": args.resume,
                "max_parallel": args.max_parallel,
                "batch_output_root": str(run_log_root),
            },
            ensure_ascii=False,
        )
    )

    if skipped_records:
        print(
            json.dumps(
                {
                    "resume_skipped_instance_ids": [record.instance_id for record in skipped_records],
                },
                ensure_ascii=False,
            )
        )

    if not records:
        print("[]")
        return 0

    summaries = _run_commands(
        records=records,
        commands=commands,
        max_parallel=args.max_parallel,
        run_log_root=run_log_root,
        circuit_breaker_returncodes=config.circuit_breaker_returncodes,
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item["returncode"] == 0 for item in summaries) else 1


def _parse_args(
    config: BatchRunnerConfig,
    argv: list[str] | None,
) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=config.description)
    parser.add_argument("--jsonl-list", nargs="+", type=Path, required=True)
    parser.add_argument("--max-parallel", type=int, default=config.default_max_parallel)
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--batch-output-root", type=Path, default=None)
    parser.add_argument("--batch-log-root", type=Path, default=DEFAULT_BATCH_OUTPUT_ROOT)
    parser.add_argument("--resume", choices=["none", "normal", "strict"], default="strict")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_known_args(argv)


def _resolve_output_key(
    config: BatchRunnerConfig,
    args: argparse.Namespace,
    passthrough_args: list[str],
) -> str:
    explicit_output_key = _extract_flag_value(passthrough_args, "--output-key")
    if explicit_output_key:
        return explicit_output_key
    if config.batch_log_key_resolver is not None:
        return config.batch_log_key_resolver(args, passthrough_args)
    model = _extract_flag_value(passthrough_args, "--model")
    if model:
        return agent_model_key(config.agent_family, model)
    return config.agent_family


def _extract_flag_value(tokens: list[str], flag: str) -> str | None:
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _validate_api_key_args(tokens: list[str]) -> str | None:
    """Fail before launching per-instance jobs if the requested key is absent.

    Single-instance repair clears canonical outputs before invoking the model.
    A missing key would otherwise create empty failure artifacts for every
    instance in the batch, which is especially dangerous with resume runs.
    """
    if _extract_flag_value(tokens, "--api-key"):
        return None
    api_key_env = _extract_flag_value(tokens, "--api-key-env")
    if not api_key_env:
        return None
    if os.environ.get(api_key_env):
        return None
    return f"Missing API key environment variable before batch start: {api_key_env}"


def _filter_records_for_resume(
    *,
    records: list[BenchmarkRecord],
    resume_mode: str,
    agent_name: str,
    output_root: Path,
) -> tuple[list[BenchmarkRecord], list[BenchmarkRecord]]:
    if resume_mode == "none":
        return records, []

    runnable: list[BenchmarkRecord] = []
    skipped: list[BenchmarkRecord] = []
    for record in records:
        status_file = repair_status_path(agent_name, record.repo_key, record.instance_id, output_root)
        if _should_skip_record(status_file=status_file, resume_mode=resume_mode):
            skipped.append(record)
        else:
            runnable.append(record)
    return runnable, skipped


def _should_skip_record(*, status_file: Path, resume_mode: str) -> bool:
    if not status_file.exists():
        return False
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    if resume_mode == "normal":
        return payload.get("agent_patch_generated") is True
    if resume_mode == "strict":
        return (
            payload.get("agent_run_completed") is True
            and payload.get("agent_patch_generated") is True
        )
    return False


def _build_single_command(
    *,
    python_executable: str,
    agent_script: Path,
    jsonl_list: list[Path],
    record: BenchmarkRecord,
    output_root: Path,
    passthrough_args: list[str],
) -> list[str]:
    return [
        python_executable,
        str(agent_script),
        "--jsonl-list",
        *[str(path) for path in jsonl_list],
        "--instance-id",
        record.instance_id,
        "--output-root",
        str(output_root),
        *passthrough_args,
    ]


def _run_commands(
    *,
    records: list[BenchmarkRecord],
    commands: list[list[str]],
    max_parallel: int,
    run_log_root: Path,
    circuit_breaker_returncodes: tuple[int, ...],
) -> list[dict[str, object]]:
    pending = deque(zip(records, commands, strict=True))
    running: list[RunningJob] = []
    summaries: list[dict[str, object]] = []
    circuit_breaker_triggered = False

    try:
        while (pending or running) and not circuit_breaker_triggered:
            while pending and len(running) < max_parallel:
                record, command = pending.popleft()
                running.append(_start_job(record, command, run_log_root))

            finished: list[RunningJob] = []
            for job in running:
                returncode = job.process.poll()
                if returncode is None:
                    continue
                finished.append(job)
                summary = _finish_job(job, returncode)
                summaries.append(summary)
                if _is_circuit_breaker_returncode(returncode, circuit_breaker_returncodes):
                    circuit_breaker_triggered = True
                    print(
                        "[circuit-breaker] "
                        f"{job.record.instance_id} returncode={returncode}; "
                        "terminating running jobs and clearing pending queue."
                    )

            if finished:
                running = [job for job in running if job not in finished]
                if circuit_breaker_triggered:
                    pending.clear()
                    for job in running:
                        _terminate_process_tree(job.process)
                        returncode = job.process.poll()
                        summaries.append(_finish_job(job, returncode if returncode is not None else -1))
                    running.clear()
                continue

            time.sleep(1.0)
    except KeyboardInterrupt:
        for job in running:
            _terminate_process_tree(job.process)
            _close_job_handles(job)
        raise

    return summaries


def _is_circuit_breaker_returncode(returncode: int, circuit_breaker_returncodes: tuple[int, ...]) -> bool:
    return returncode in circuit_breaker_returncodes


def _start_job(record: BenchmarkRecord, command: list[str], run_log_root: Path) -> RunningJob:
    job_dir = run_log_root / record.repo_key / record.instance_id
    job_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    command_path = job_dir / "command.txt"
    command_path.write_text(_format_command(command), encoding="utf-8")

    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=(sys.platform != "win32"),
    )
    print(f"[start] {record.instance_id} pid={process.pid}")
    return RunningJob(
        record=record,
        command=command,
        process=process,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        command_path=command_path,
        started_at=time.time(),
    )


def _finish_job(job: RunningJob, returncode: int) -> dict[str, object]:
    _close_job_handles(job)
    elapsed_seconds = round(time.time() - job.started_at, 2)
    status = "ok" if returncode == 0 else "failed"
    print(f"[{status}] {job.record.instance_id} returncode={returncode} elapsed={elapsed_seconds}s")
    return {
        "instance_id": job.record.instance_id,
        "repo": job.record.repo,
        "returncode": returncode,
        "elapsed_seconds": elapsed_seconds,
        "stdout_path": str(job.stdout_path),
        "stderr_path": str(job.stderr_path),
        "command_path": str(job.command_path),
    }


def _close_job_handles(job: RunningJob) -> None:
    job.stdout_handle.close()
    job.stderr_handle.close()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _format_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


