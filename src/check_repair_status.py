#!/usr/bin/env python3
"""Summarize repair step-1 status from logs and patch_status files.

Example:
  python src/check_repair_status.py --agent mini_swe_agent_glm51 --projects buck sentry closure-compiler
  python src/check_repair_status.py
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


DEFAULT_PROJECT_JSONL = {
    "media": "benchmark/jsonl_files/androidx_media_6.jsonl",
    "doris": "benchmark/jsonl_files/apache_doris_2.jsonl",
    "Ax": "benchmark/jsonl_files/facebook_ax_3.jsonl",
    "checkmk": "benchmark/jsonl_files/checkmk_checkmk_12.jsonl",
    "elasticsearch": "benchmark/jsonl_files/elastic_elasticsearch_15.jsonl",
    "buck": "benchmark/jsonl_files/facebook_buck_22.jsonl",
    "sentry": "benchmark/jsonl_files/getsentry_sentry_32.jsonl",
    "closure-compiler": "benchmark/jsonl_files/google_closure-compiler_41.jsonl",
    "closure-templates": "benchmark/jsonl_files/google_closure-templates_24.jsonl",
    "nomulus": "benchmark/jsonl_files/google_nomulus_22.jsonl",
    "pants": "benchmark/jsonl_files/pantsbuild_pants_9.jsonl",
    "pytorch": "benchmark/jsonl_files/pytorch_pytorch_15.jsonl",
    "ray": "benchmark/jsonl_files/ray-project_ray_7.jsonl",
    "zulip": "benchmark/jsonl_files/zulip_zulip_5.jsonl",
}

STATUS_KEYS = (
    "image_available",
    "workspace_prepared",
    "workspace_writable",
    "agent_run_completed",
    "agent_modified_worktree",
    "agent_patch_generated",
)


def normalize_project(name: str) -> str:
    return name.lower()


def project_from_jsonl_path(path: Path) -> str:
    stem = re.sub(r"_\d+$", "", path.stem)
    if "_" not in stem:
        return stem
    return stem.split("_", 1)[1]


def discover_project_jsonl(benchmark_dir: Path) -> dict[str, Path]:
    project_jsonl: dict[str, Path] = {}
    for project, raw_path in DEFAULT_PROJECT_JSONL.items():
        path = Path(raw_path)
        if path.exists():
            project_jsonl[normalize_project(project)] = path

    if benchmark_dir.exists():
        for path in sorted(benchmark_dir.glob("*.jsonl")):
            project_jsonl[normalize_project(project_from_jsonl_path(path))] = path
    return project_jsonl


def discover_agents(output_root: Path) -> list[str]:
    if not output_root.exists():
        return []
    agents: list[str] = []
    for path in sorted(output_root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_dir():
            continue
        if any((path / child).exists() for child in ("logs", "repair_results", "eval_results")):
            agents.append(path.name)
    return agents


def discover_projects(output_root: Path, agents: list[str]) -> list[str]:
    projects: set[str] = set()
    for agent in agents:
        agent_root = output_root / agent
        for path in (
            agent_root / "logs",
            agent_root / "eval_results",
            agent_root / "repair_results" / "patch_status",
        ):
            if not path.exists():
                continue
            projects.update(child.name for child in path.iterdir() if child.is_dir())
    return sorted(projects, key=lambda item: item.lower())


def load_instance_ids(jsonl_path: Path) -> list[str]:
    ids: list[str] = []
    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            ids.append(json.loads(line)["instance_id"])
    return ids


def parse_last_dict(log_path: Path, marker: str) -> dict | None:
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    matches = re.findall(rf"{re.escape(marker)}=(\{{[^\n]*\}})", text)
    if not matches:
        return None
    try:
        return ast.literal_eval(matches[-1])
    except Exception:
        return None


def parse_log_status(log_path: Path) -> dict | None:
    status = parse_last_dict(log_path, "repair_status")
    if status is not None:
        return status
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    matches = re.findall(r"patch status:\s*(\{[^\n]*\})", text)
    if not matches:
        matches = re.findall(r"\{[^\n]*'agent_patch_generated'[^\n]*\}", text)
    if not matches:
        return None
    try:
        return ast.literal_eval(matches[-1])
    except Exception:
        try:
            return json.loads(matches[-1].replace("'", '"'))
        except Exception:
            return None


def false_keys(payload: dict | None) -> list[str]:
    if not isinstance(payload, dict):
        return ["missing_status"]
    return [key for key in STATUS_KEYS if payload.get(key) is not True]


def reason_from_log(log_path: Path) -> str:
    summary = parse_last_dict(log_path, "agent_summary")
    if not isinstance(summary, dict):
        return "unknown"
    reasons: list[str] = []
    if summary.get("timed_out"):
        reasons.append("timeout")
    returncode = summary.get("returncode")
    if returncode not in (0, None):
        reasons.append(f"returncode={returncode}")
    exit_status = summary.get("exit_status")
    if exit_status:
        reasons.append(f"exit_status={exit_status}")
    completion_source = summary.get("completion_source")
    if completion_source:
        reasons.append(f"completion_source={completion_source}")
    return ", ".join(reasons) if reasons else "unknown"


def status_for_project(output_root: Path, agent: str, project: str, jsonl_path: Path) -> dict:
    expected = load_instance_ids(jsonl_path)
    logs_dir = output_root / agent / "logs" / project
    status_dir = output_root / agent / "repair_results" / "patch_status" / project

    result = {"tt": [], "running": [], "pending": [], "non_tt": []}
    for instance_id in expected:
        log_path = logs_dir / instance_id / "step1.log"
        lock_dir = logs_dir / instance_id / "step1.lock"
        patch_status_path = status_dir / f"{instance_id}.json"

        if lock_dir.exists():
            result["running"].append(instance_id)
            continue
        if not log_path.exists() and not patch_status_path.exists():
            result["pending"].append(instance_id)
            continue

        log_status = parse_log_status(log_path)
        patch_status = None
        if patch_status_path.exists():
            try:
                patch_status = json.loads(patch_status_path.read_text(encoding="utf-8", errors="ignore"))
            except json.JSONDecodeError:
                patch_status = None

        log_false = false_keys(log_status)
        patch_false = false_keys(patch_status)
        if not log_false and not patch_false:
            result["tt"].append(instance_id)
        else:
            result["non_tt"].append(
                {
                    "instance_id": instance_id,
                    "log_false": log_false,
                    "patch_status_false": patch_false,
                    "reason": reason_from_log(log_path),
                }
            )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default="output_data_batch")
    parser.add_argument("--agent", help="Single agent to inspect; kept for backward compatibility")
    parser.add_argument("--agents", nargs="*", help="Agents to inspect; defaults to all agents under output-root")
    parser.add_argument("--projects", nargs="*", help="Projects to inspect; defaults to projects found under selected agents")
    parser.add_argument("--benchmark-dir", default="benchmark/jsonl_files")
    parser.add_argument("--jsonl", action="append", default=[], help="Override as project=path")
    args = parser.parse_args()

    output_root = Path(args.output_root)

    project_jsonl = discover_project_jsonl(Path(args.benchmark_dir))
    for item in args.jsonl:
        project, path = item.split("=", 1)
        project_jsonl[normalize_project(project)] = Path(path)

    agents: list[str] = []
    if args.agent:
        agents.append(args.agent)
    if args.agents:
        agents.extend(args.agents)
    if not agents:
        agents = discover_agents(output_root)
    agents = list(dict.fromkeys(agents))
    if not agents:
        raise SystemExit(f"No agents found under {output_root}")

    projects = args.projects or discover_projects(output_root, agents)
    if not projects:
        raise SystemExit(f"No projects found for {', '.join(agents)} under {output_root}")

    for agent_index, agent in enumerate(agents):
        if len(agents) > 1:
            if agent_index:
                print()
            print(f"AGENT {agent}")
        for project in projects:
            jsonl_path = project_jsonl.get(normalize_project(project))
            if jsonl_path is None:
                raise SystemExit(
                    f"Unknown project {project}; add a matching JSONL under {args.benchmark_dir} "
                    f"or pass --jsonl {project}=path"
                )
            if not jsonl_path.exists():
                raise SystemExit(f"JSONL for project {project} does not exist: {jsonl_path}")
            result = status_for_project(output_root, agent, project, jsonl_path)
            print(f"PROJECT {project}")
            print(f"  T/T: {len(result['tt'])}")
            running = result["running"]
            print(f"  running: {len(running)}" + (f" -> {', '.join(running)}" if running else ""))
            print(f"  pending: {len(result['pending'])}")
            non_tt = result["non_tt"]
            print(f"  non_T/T: {len(non_tt)}")
            for item in non_tt:
                print(
                    "    "
                    f"{item['instance_id']}: "
                    f"log_false={item['log_false']}; "
                    f"patch_status_false={item['patch_status_false']}; "
                    f"reason={item['reason']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

