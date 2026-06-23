"""Claude Code diff_agent_patch 钖勫皝瑁呫€?""

from __future__ import annotations

from pathlib import Path

from src.utils.repair_steps import diff_agent_patch_for_record


def run_diff_step(*, pristine_dir: Path | None, candidate_dir: Path | None, patch_path: Path, logger, runner) -> str:
    return diff_agent_patch_for_record(
        pristine_dir=pristine_dir,
        candidate_dir=candidate_dir,
        patch_path=patch_path,
        logger=logger,
        runner=runner,
    )


