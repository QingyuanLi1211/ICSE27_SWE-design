"""live_swe_agent build_worktree 钖勫皝瑁呫€?""

from __future__ import annotations

from pathlib import Path

from src.utils.repair_steps import build_worktree_for_record


def run_build_step(*, record, bundle_root: Path, work_root: Path, repair_status: dict, logger, runner):
    return build_worktree_for_record(
        record=record,
        bundle_root=bundle_root,
        work_root=work_root,
        repair_status=repair_status,
        logger=logger,
        runner=runner,
    )



