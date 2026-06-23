"""鍏变韩 eval 涓诲叆鍙ｏ細鍙秷璐?JSONL銆乨ocker image 鍜?agent patch銆?""

"""

from __future__ import annotations

import argparse
import json
import tempfile
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.utils.benchmark import load_records, select_records
from src.utils.eval_core import evaluate_agent_only, evaluate_full, evaluate_infrastructure
from src.utils.logs import StepLogger
from src.utils.output_layout import (
    eval_infra_cache_path,
    eval_infra_cache_log_path,
    eval_result_path,
    repair_results_root,
    step_log_path,
)
from src.utils.process import rmtree_force, run_command
from src.utils.status import write_status


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "output_data_batch"


def _make_short_eval_root(agent_name: str, instance_id: str) -> Path:
    base = Path(tempfile.gettempdir()) / "sdb"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="e-", dir=base))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate saved agent patches against the benchmark.")
    parser.add_argument("--jsonl-list", nargs="+", type=Path, required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--mode", choices=["infrastructure-only", "agent-only", "full"], default="full")
    parser.add_argument("--agent-name")
    parser.add_argument("--agent-patch-path", type=Path)
    parser.add_argument("--agent-patch-root", type=Path)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def _step2_log_path(args: argparse.Namespace, repo_key: str, instance_id: str) -> Path:
    if args.mode == "infrastructure-only":
        return eval_infra_cache_log_path(repo_key, instance_id)
    return step_log_path(args.agent_name, repo_key, instance_id, "step2", args.output_root)


def _evaluate_one(
    *,
    args: argparse.Namespace,
    record,
    patch_path: Path | None,
    temp_root: Path,
    logger: StepLogger,
) -> dict[str, object]:
    if args.mode == "infrastructure-only":
        infra_status = evaluate_infrastructure(
            record=record,
            bundle_root=args.bundle_root,
            work_root=temp_root,
            logger=logger,
            runner=run_command,
        )
        infra_path = eval_infra_cache_path(record.repo_key, record.instance_id, args.output_root)
        write_status(infra_path, infra_status)
        logger.log(f"infrastructure_eval_status={infra_status}")
        return {
            "instance_id": record.instance_id,
            "mode": args.mode,
            "infra_cache_path": str(infra_path),
            "eval_infrastructure_valid": infra_status["eval_infrastructure_valid"],
        }

    infra_path = eval_infra_cache_path(record.repo_key, record.instance_id, args.output_root)
    result_path = eval_result_path(args.agent_name, record.repo_key, record.instance_id, args.output_root)

    if args.mode == "agent-only":
        if not _infra_cache_valid(infra_path):
            logger.log(f"skip_agent_eval=True reason=missing_or_invalid_infra_cache path={infra_path}")
            return {
                "instance_id": record.instance_id,
                "mode": args.mode,
                "agent_name": args.agent_name,
                "eval_result_path": str(result_path),
                "infra_cache_path": str(infra_path),
                "skipped": True,
                "skip_reason": "missing_or_invalid_infra_cache",
            }
        agent_status = evaluate_agent_only(
            record=record,
            bundle_root=args.bundle_root,
            agent_patch_path=patch_path,
            work_root=temp_root,
            logger=logger,
            runner=run_command,
        )
        write_status(result_path, agent_status)
        logger.log(f"agent_eval_status={agent_status}")
        return {
            "instance_id": record.instance_id,
            "mode": args.mode,
            "agent_name": args.agent_name,
            "eval_result_path": str(result_path),
            "infra_cache_path": str(infra_path),
            "agent_patch_passed": agent_status["agent_patch_passed"],
        }

    infra_status, agent_status = evaluate_full(
        record=record,
        bundle_root=args.bundle_root,
        agent_patch_path=patch_path,
        work_root=temp_root,
        logger=logger,
        runner=run_command,
    )
    write_status(infra_path, infra_status)
    logger.log(f"infrastructure_eval_status={infra_status}")
    if agent_status is None:
        return {
            "instance_id": record.instance_id,
            "mode": args.mode,
            "agent_name": args.agent_name,
            "eval_result_path": str(result_path),
            "infra_cache_path": str(infra_path),
            "eval_infrastructure_valid": infra_status["eval_infrastructure_valid"],
            "skipped": True,
            "skip_reason": "infrastructure_invalid",
        }

    write_status(result_path, agent_status)
    logger.log(f"agent_eval_status={agent_status}")
    return {
        "instance_id": record.instance_id,
        "mode": args.mode,
        "agent_name": args.agent_name,
        "eval_result_path": str(result_path),
        "infra_cache_path": str(infra_path),
        "eval_infrastructure_valid": infra_status["eval_infrastructure_valid"],
        "agent_patch_passed": agent_status["agent_patch_passed"],
    }


def _infra_cache_valid(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return payload.get("eval_infrastructure_valid") is True


def main() -> int:
    args = parse_args()
    args.bundle_root = args.bundle_root.resolve()
    args.output_root = args.output_root.resolve()
    if args.agent_patch_path is not None and args.agent_patch_root is not None:
        raise ValueError("Pass only one of --agent-patch-path or --agent-patch-root.")
    if args.agent_patch_path is not None and args.instance_id is None:
        raise ValueError("--agent-patch-path requires --instance-id.")
    if args.mode in {"agent-only", "full"} and not args.agent_name:
        raise ValueError("--agent-name is required for agent-only/full eval modes.")

    records = select_records(load_records(args.jsonl_list), args.instance_id)
    patch_root = None
    if args.agent_name:
        default_patch_root = repair_results_root(args.agent_name, args.output_root) / "agent_patch"
        patch_root = args.agent_patch_root or default_patch_root

    summaries: list[dict[str, object]] = []
    for record in records:
        logger = StepLogger(_step2_log_path(args, record.repo_key, record.instance_id))
        logger.log(f"instance_id={record.instance_id}")
        patch_path = None
        if args.mode in {"agent-only", "full"}:
            patch_path = args.agent_patch_path or (patch_root / record.repo_key / f"{record.instance_id}.diff")
            logger.log(f"agent_patch_path={patch_path}")

        temp_root = _make_short_eval_root(args.agent_name or "infrastructure", record.instance_id)
        try:
            summaries.append(_evaluate_one(args=args, record=record, patch_path=patch_path, temp_root=temp_root, logger=logger))
        finally:
            rmtree_force(temp_root)

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


