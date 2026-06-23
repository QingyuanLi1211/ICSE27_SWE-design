"""mini_swe_agent repair entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.empirical_agents.mini_swe_agent.run_agent_fixing.main import MiniConfig, run_mini_fixing
from src.utils.agent_keys import agent_model_key
from src.utils.benchmark import load_records, select_records
from src.utils.process import run_command
from src.utils.repair_pipeline import run_repair_for_record


ROOT = Path(__file__).resolve().parents[3]
AGENT_FAMILY = "mini_swe_agent"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full mini_swe_agent repair pipeline.")
    parser.add_argument("--jsonl-list", nargs="+", type=Path, required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "output_data")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key")
    parser.add_argument("--thinking-effort", default="high")
    parser.add_argument("--agent-timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--agent-edit-mode",
        choices=["workspace-write", "danger-full-access"],
        default="danger-full-access",
    )
    parser.add_argument("--conda-env", default="minisweagent")
    parser.add_argument("--mini-python", type=Path)
    parser.add_argument("--api-base")
    parser.add_argument("--api-key-env")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--output-key")
    parser.add_argument(
        "--repair-environment",
        choices=["local", "docker"],
        default="local",
        help="Run agent shell tools locally or inside the benchmark Docker image.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_root = args.output_root.resolve()
    args.bundle_root = args.bundle_root.resolve()
    config = MiniConfig(
        model=args.model,
        api_key=args.api_key,
        api_key_env=args.api_key_env,
        thinking_effort=args.thinking_effort,
        timeout_seconds=args.agent_timeout_seconds,
        agent_edit_mode=args.agent_edit_mode,
        conda_env=args.conda_env,
        mini_python=args.mini_python,
        api_base=args.api_base,
        max_tokens=args.max_tokens,
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
            execute_agent=lambda current_record, prompt, candidate_dir, trajectory_root, logger, _config=config: run_mini_fixing(
                prompt=prompt,
                candidate_dir=candidate_dir,
                trajectory_root=trajectory_root,
                logger=logger,
                config=_config,
            ),
            repair_environment=args.repair_environment,
        )
        for record in records
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


