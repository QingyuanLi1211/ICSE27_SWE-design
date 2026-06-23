"""Claude Code repair 鎬诲叆鍙ｃ€?
璇ュ叆鍙ｆ寜缁熶竴椤哄簭鎵ц build worktree -> run agent fixing -> diff agent patch銆?eval 涓嶅湪杩欓噷鎵ц锛屽悗缁粺涓€璋冪敤 src/eval/eval.py銆?"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.empirical_agents.claude_code.run_agent_fixing.main import (
    CLAUDE_CODE_RATE_LIMIT_EXIT_CODE,
    ClaudeCodeConfig,
    run_claude_code_fixing,
)
from src.utils.agent_keys import agent_model_key
from src.utils.benchmark import load_records, select_records
from src.utils.process import run_command
from src.utils.repair_pipeline import run_repair_for_record


ROOT = Path(__file__).resolve().parents[3]
AGENT_FAMILY = "claude_code"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full Claude Code repair pipeline.")
    parser.add_argument("--jsonl-list", nargs="+", type=Path, required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "output_data")
    parser.add_argument("--model", default="claude-opus-4-7")
    parser.add_argument("--api-key")
    parser.add_argument("--thinking-effort", default="high")
    parser.add_argument("--agent-timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--agent-edit-mode",
        choices=["workspace-write", "danger-full-access"],
        default="danger-full-access",
    )
    parser.add_argument("--conda-env", default="claudecode")
    parser.add_argument("--claude-cli-path", type=Path)
    parser.add_argument("--max-turns", type=int, default=120)
    parser.add_argument("--tools", default="Bash,Read,Edit,MultiEdit,Write,Glob,Grep,LS")
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
    config = ClaudeCodeConfig(
        cli_path=args.claude_cli_path,
        conda_env=args.conda_env,
        model=args.model,
        api_key=args.api_key,
        thinking_effort=args.thinking_effort,
        timeout_seconds=args.agent_timeout_seconds,
        agent_edit_mode=args.agent_edit_mode,
        max_turns=args.max_turns,
        tools=args.tools,
        repair_environment=args.repair_environment,
    )
    output_key = args.output_key or agent_model_key(AGENT_FAMILY, args.model)
    records = select_records(load_records(args.jsonl_list), args.instance_id)
    summaries = []
    for record in records:
        summary = run_repair_for_record(
            agent_name=output_key,
            record=record,
            bundle_root=args.bundle_root,
            output_root=args.output_root,
            runner=run_command,
            repair_environment=args.repair_environment,
            execute_agent=lambda current_record, prompt, candidate_dir, trajectory_root, logger, _config=config: run_claude_code_fixing(
                prompt=prompt,
                candidate_dir=candidate_dir,
                trajectory_root=trajectory_root,
                logger=logger,
                config=_config,
            ),
        )
        summaries.append(summary)
        if (summary.get("agent_summary") or {}).get("rate_limited") is True:
            break
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    if any((item.get("agent_summary") or {}).get("rate_limited") is True for item in summaries):
        return CLAUDE_CODE_RATE_LIMIT_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


