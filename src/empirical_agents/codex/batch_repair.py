"""Batch repair launcher for Codex."""

from __future__ import annotations

import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.empirical_agents.common.batch_runner import BatchRunnerConfig, run_batch


def main() -> int:
    return run_batch(
        BatchRunnerConfig(
            agent_family="codex",
            agent_script=Path(__file__).with_name("codex.py"),
            default_max_parallel=4,
            description="Run Codex repair instances in parallel subprocesses.",
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())


