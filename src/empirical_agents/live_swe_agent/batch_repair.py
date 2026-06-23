"""Batch repair launcher for Live-SWE-agent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.empirical_agents.common.batch_runner import BatchRunnerConfig, run_batch
from src.utils.agent_keys import agent_model_key


def _batch_log_key(args: argparse.Namespace, passthrough_args: list[str]) -> str:
    model = "gemini-3.1-pro-preview"
    for index, token in enumerate(passthrough_args):
        if token == "--model" and index + 1 < len(passthrough_args):
            model = passthrough_args[index + 1]
    return agent_model_key("live_swe_agent", model)


def main() -> int:
    return run_batch(
        BatchRunnerConfig(
            agent_family="live_swe_agent",
            agent_script=Path(__file__).with_name("live_swe_agent.py"),
            default_max_parallel=4,
            description="Run Live-SWE-agent repair instances in parallel subprocesses.",
            batch_log_key_resolver=_batch_log_key,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())


