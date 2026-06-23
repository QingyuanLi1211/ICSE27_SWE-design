"""Batch repair launcher for Claude Code."""

from __future__ import annotations

import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.empirical_agents.common.batch_runner import BatchRunnerConfig, run_batch
from src.empirical_agents.claude_code.run_agent_fixing.main import CLAUDE_CODE_RATE_LIMIT_EXIT_CODE


def main() -> int:
    return run_batch(
        BatchRunnerConfig(
            agent_family="claude_code",
            agent_script=Path(__file__).with_name("claude_code.py"),
            default_max_parallel=3,
            description="Run Claude Code repair instances in parallel subprocesses.",
            circuit_breaker_returncodes=(CLAUDE_CODE_RATE_LIMIT_EXIT_CODE,),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())


