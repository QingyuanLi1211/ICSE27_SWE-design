"""Codex repair entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.empirical_agents.codex.run_agent_fixing.main import CodexConfig, run_codex_fixing
from src.utils.agent_keys import agent_model_key
from src.utils.benchmark import load_records, select_records
from src.utils.process import run_command
from src.utils.repair_pipeline import run_repair_for_record


ROOT = Path(__file__).resolve().parents[3]
AGENT_FAMILY = "codex"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full Codex repair pipeline.")
    parser.add_argument("--jsonl-list", nargs="+", type=Path, required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "output_data")
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--api-key")
    parser.add_argument("--thinking-effort", default="high")
    parser.add_argument("--agent-timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--agent-edit-mode",
        choices=["workspace-write", "danger-full-access"],
        default="danger-full-access",
    )
    parser.add_argument("--codex-cli-path", default="codex.cmd")
    parser.add_argument("--output-key")
    parser.add_argument(
        "--repair-environment",
        choices=["local", "docker"],
        default="local",
        help="Run repair against the local worktree or a prepared instance Docker container.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_root = args.output_root.resolve()
    args.bundle_root = args.bundle_root.resolve()
    config = CodexConfig(
        cli_path=args.codex_cli_path,
        model=args.model,
        api_key=args.api_key,
        thinking_effort=args.thinking_effort,
        timeout_seconds=args.agent_timeout_seconds,
        agent_edit_mode=args.agent_edit_mode,
        repair_environment=args.repair_environment,
    )
    output_key = args.output_key or agent_model_key(AGENT_FAMILY, args.model)
    records = select_records(load_records(args.jsonl_list), args.instance_id)
    summaries = [
        run_repair_for_record(
            agent_name=output_key,
            record=record,
            bundle_root=args.bundle_root,
            output_root=args.output_root,
            runner=run_command,
            repair_environment=args.repair_environment,
            execute_agent=lambda current_record, prompt, candidate_dir, trajectory_root, logger, _config=config: run_codex_fixing(
                prompt=prompt,
                candidate_dir=candidate_dir,
                trajectory_root=trajectory_root,
                logger=logger,
                config=_config,
            ),
        )
        for record in records
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


