#!/usr/bin/env python3
"""Find eval logs with external-dependency or harness-interrupt signatures.

By default this scans every agent/project currently present under
output_data_batch instead of relying on a hard-coded project list.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

DEFAULT_OUTPUT_ROOT = Path("output_data_batch")
DEFAULT_INFRA_ROOT = Path("eval_infra_cache")
TS_RE = re.compile(r"^\[(.*?)\]")

NETWORK_PATTERNS = [
    ("all_mirrors_down", r"All mirrors are down"),
    ("could_not_transfer_artifact", r"Could not transfer artifact"),
    ("transfer_failed", r"Transfer failed for"),
    ("remote_host_handshake", r"Remote host terminated the handshake"),
    ("ssl_handshake", r"SSLHandshakeException|SSL peer shut down incorrectly"),
    ("read_timed_out", r"Read timed out"),
    ("connection_reset", r"Connection reset"),
    ("connection_refused", r"Connection refused"),
    ("unknown_host", r"UnknownHostException|Unknown host"),
    ("dns_temporary_failure", r"Temporary failure in name resolution|Name or service not known"),
    ("dependency_resolution", r"Could not resolve all files|Could not download|No cached version"),
    ("eof_exception", r"\bEOFException\b"),
    ("http_5xx", r"status code 500|502 Bad Gateway|503 Service Unavailable"),
    ("node_dns", r"\bENOTFOUND\b|\bEAI_AGAIN\b"),
    ("network_unreachable", r"Network is unreachable"),
    ("failed_to_connect_to", r"Failed to connect to\b"),
    ("build_did_not_start", r"Couldn't start the build\. Unable to run tests\."),
]

HARD_INTERRUPT_PATTERNS = [
    ("returncode_137", r"returncode=137|exit code 137"),
    ("keyboard_interrupt", r"KeyboardInterrupt"),
    ("no_such_container", r"No such container"),
    ("docker_500", r"request returned 500"),
    ("container_error", r"The container|container is not running|No such image|OCI runtime"),
    ("no_such_process", r"No such process"),
    ("docker_daemon", r"Cannot connect to the Docker daemon"),
    ("disk_full", r"No space left on device|disk full"),
    ("oom_or_killed", r"Cannot allocate memory|OutOfMemoryError|signal: killed|Killed"),
]

REAL_FAILURE_PATTERNS = [
    r"AssertionError",
    r"Assertion failed",
    r"Tests with failures:",
    r"Failed tests:",
    r"Tests run: .*Failures: [1-9]",
    r"There are test failures",
    r"test completed, \d+ failed",
    r"There were failing tests",
    r"BUILD FAILED",
    r"COMPILATION ERROR",
    r"Compilation failure",
    r"cannot be converted",
    r"cannot find symbol",
    r"FAILED \(failures=",
    r"FAILED \(errors=",
    r"FAIL:",
    r"ERROR:",
    r"Traceback \(most recent call last\)",
    r"org\.opentest4j\.AssertionFailedError",
    r"java\.lang\.AssertionError",
    r"ComparisonFailure",
    r"MojoFailureException",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--infra-root", default=str(DEFAULT_INFRA_ROOT))
    parser.add_argument("--agents", nargs="*", help="Agents to scan; defaults to all agents with eval_results")
    parser.add_argument("--projects", nargs="*", help="Projects to scan; defaults to all projects under selected agents")
    parser.add_argument("--false-only", action="store_true", help="Only print external-pattern rows where agent_patch_passed is false")
    return parser.parse_args()


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def discover_agents(output_root: Path) -> list[str]:
    if not output_root.exists():
        return []
    agents = []
    for path in sorted(output_root.iterdir(), key=lambda p: p.name.lower()):
        if (path / "eval_results").is_dir():
            agents.append(path.name)
    return agents


def discover_projects(output_root: Path, agents: list[str]) -> list[str]:
    projects: set[str] = set()
    for agent in agents:
        eval_root = output_root / agent / "eval_results"
        if eval_root.exists():
            projects.update(child.name for child in eval_root.iterdir() if child.is_dir())
    return sorted(projects, key=lambda item: item.lower())


def load_infra_validity(infra_root: Path) -> dict[tuple[str, str], bool]:
    validity: dict[tuple[str, str], bool] = {}
    if not infra_root.exists():
        return validity
    for project_dir in sorted(infra_root.iterdir(), key=lambda p: p.name.lower()):
        if not project_dir.is_dir() or project_dir.name in {"eval_infra_cache_logs", "maven_cache"}:
            continue
        for json_path in project_dir.glob("*.json"):
            payload = load_json(json_path)
            validity[(project_dir.name, json_path.stem)] = bool(
                payload and payload.get("eval_infrastructure_valid") is True
            )
    return validity


def parse_duration(log_path: Path) -> float | None:
    if not log_path.exists():
        return None
    first = last = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = TS_RE.match(line)
        if not match:
            continue
        try:
            timestamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        except Exception:
            continue
        if first is None:
            first = timestamp
        last = timestamp
    return (last - first).total_seconds() if first and last else None


def read_log(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def line_hits(text: str, patterns: list[tuple[str, str]], limit: int = 3) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for line in text.splitlines():
        for label, pattern in patterns:
            if re.search(pattern, line, flags=re.IGNORECASE):
                hits.append((label, line.strip()[:240]))
                break
        if len(hits) >= limit:
            break
    return hits


def real_failures_after_last_external(text: str) -> str:
    last = -1
    for _, pattern in NETWORK_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            last = max(last, match.start())
    tail = text[last:] if last >= 0 else text[-5000:]
    labels = []
    for pattern in REAL_FAILURE_PATTERNS:
        if re.search(pattern, tail, flags=re.IGNORECASE):
            labels.append(pattern)
    return ";".join(labels[:5])


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root)
    infra_root = Path(args.infra_root)
    agents = args.agents or discover_agents(output_root)
    projects = args.projects or discover_projects(output_root, agents)
    infra_validity = load_infra_validity(infra_root)

    invalid_infra = {
        key for key, is_valid in infra_validity.items() if not is_valid
    }
    rows = []
    hard = []
    summary = []

    for project in projects:
        for agent in agents:
            result_dir = output_root / agent / "eval_results" / project
            valid_done = 0
            network_suspicious = 0
            network_false_suspicious = 0
            hard_suspicious = 0
            if not result_dir.exists():
                summary.append((project, agent, 0, 0, 0, 0))
                continue
            for result_path in sorted(result_dir.glob("*.json")):
                instance_id = result_path.stem
                if (project, instance_id) in invalid_infra:
                    continue
                payload = load_json(result_path)
                if not payload or not isinstance(payload.get("agent_patch_passed"), bool):
                    hard.append((project, agent, instance_id, "bad_json_or_missing_boolean", "", ""))
                    continue

                valid_done += 1
                passed = payload["agent_patch_passed"]
                log_path = output_root / agent / "logs" / project / instance_id / "step2.log"
                text = read_log(log_path)
                duration = parse_duration(log_path)
                network_hits = line_hits(text, NETWORK_PATTERNS) if text else [("missing_log", "")]
                hard_hits = line_hits(text, HARD_INTERRUPT_PATTERNS) if text else [("missing_log", "")]

                real_after = real_failures_after_last_external(text) if network_hits and text else ""
                if network_hits and network_hits[0][0] != "missing_log":
                    network_suspicious += 1
                    if passed is False:
                        network_false_suspicious += 1
                    if not args.false_only or passed is False:
                        rows.append(
                            (
                                project,
                                agent,
                                instance_id,
                                passed,
                                round(duration, 1) if duration else "",
                                network_hits[0][0],
                                network_hits[0][1],
                                real_after,
                            )
                        )
                if hard_hits:
                    hard_suspicious += 1
                    hard.append(
                        (
                            project,
                            agent,
                            instance_id,
                            hard_hits[0][0],
                            round(duration, 1) if duration else "",
                            hard_hits[0][1],
                        )
                    )
            summary.append((project, agent, valid_done, network_suspicious, network_false_suspicious, hard_suspicious))

    print("NETWORK_OR_EXTERNAL_DEPENDENCY_SUSPICIOUS")
    print("project\tagent\tinstance\tagent_patch_passed\tagent_sec\tpattern\tsample\treal_failure_after_last_external")
    for row in rows:
        print("\t".join(map(str, row)))

    print("\nHARD_INTERRUPT_OR_CONTAINER_SUSPICIOUS")
    print("project\tagent\tinstance\tpattern\tagent_sec\tsample")
    for row in hard:
        print("\t".join(map(str, row)))

    print("\nSUMMARY_COUNTS")
    print("project\tagent\tvalid_infra_agent_results\tnetwork_suspicious\tnetwork_false_suspicious\thard_suspicious")
    for project, agent, valid_done, network_suspicious, network_false_suspicious, hard_suspicious in summary:
        if valid_done or network_suspicious or hard_suspicious:
            print(f"{project}\t{agent}\t{valid_done}\t{network_suspicious}\t{network_false_suspicious}\t{hard_suspicious}")

    print("\nINFRA_INVALID_OR_BAD")
    if invalid_infra:
        for project, instance_id in sorted(invalid_infra):
            print(f"{project}\t{instance_id}")
    else:
        print("none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
