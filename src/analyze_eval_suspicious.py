#!/usr/bin/env python3
"""Summarize eval-result coverage and suspicious invalidation candidates.

This is a broad audit companion to analyze_eval_external_failures.py. It scans
the live output_data_batch tree by default, verifies infra validity, checks
patch-apply state, and highlights missing logs/status, hard harness failures,
external-pattern false results, and timing anomalies.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

DEFAULT_OUTPUT_ROOT = Path("output_data_batch")
DEFAULT_INFRA_ROOT = Path("eval_infra_cache")
TS_RE = re.compile(r"^\[(.*?)\]")

HARD_PATTERNS = [
    ("returncode_137", r"returncode=137|exit code 137"),
    ("keyboard_interrupt", r"KeyboardInterrupt"),
    ("no_such_container", r"No such container"),
    ("docker_500", r"request returned 500"),
    ("container_error", r"The container|container is not running|No such image|OCI runtime"),
    ("docker_daemon", r"Cannot connect to the Docker daemon"),
    ("disk_full", r"No space left on device|disk full"),
    ("oom_or_killed", r"Cannot allocate memory|OutOfMemoryError|signal: killed|Killed"),
]

EXTERNAL_PATTERNS = [
    ("all_mirrors_down", r"All mirrors are down"),
    ("could_not_transfer_artifact", r"Could not transfer artifact"),
    ("transfer_failed", r"Transfer failed for"),
    ("failed_to_connect_to", r"Failed to connect to\b"),
    ("connection_refused", r"Connection refused"),
    ("connection_reset", r"Connection reset"),
    ("read_timed_out", r"Read timed out"),
    ("unknown_host", r"UnknownHostException|Unknown host"),
    ("dns_temporary_failure", r"Temporary failure in name resolution|Name or service not known"),
    ("dependency_resolution", r"Could not resolve all files|Could not download|No cached version"),
    ("ssl_handshake", r"SSLHandshakeException|SSL peer shut down incorrectly"),
    ("eof_exception", r"\bEOFException\b"),
    ("node_dns", r"\bENOTFOUND\b|\bEAI_AGAIN\b"),
    ("build_did_not_start", r"Couldn't start the build\. Unable to run tests\."),
]

TEST_FAILURE_PATTERNS = [
    ("assertion", r"AssertionError|Assertion failed|AssertionFailedError"),
    ("test_failures", r"Tests with failures:|Failed tests:|Tests run: .*Failures: [1-9]|test completed, \d+ failed|FAILED \(failures=|FAILED \(errors="),
    ("build_failed", r"BUILD FAILED|There were failing tests|There are test failures|MojoFailureException"),
    ("compile_error", r"COMPILATION ERROR|Compilation failure|cannot be converted|cannot find symbol"),
    ("python_traceback", r"Traceback \(most recent call last\)"),
    ("java_failure", r"java\.lang\.AssertionError|ComparisonFailure|MojoFailureException"),
    ("generic_fail_error", r"\bFAIL:|\bERROR:"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--infra-root", default=str(DEFAULT_INFRA_ROOT))
    parser.add_argument("--agents", nargs="*", help="Agents to scan; defaults to all agents with eval_results")
    parser.add_argument("--projects", nargs="*", help="Projects to scan; defaults to all projects under selected agents")
    parser.add_argument(
        "--include-test-error-signatures",
        action="store_true",
        help="Also list ordinary test/compile error signatures from false logs",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"__load_error__": str(exc)}


def discover_agents(output_root: Path) -> list[str]:
    if not output_root.exists():
        return []
    return [
        path.name
        for path in sorted(output_root.iterdir(), key=lambda p: p.name.lower())
        if (path / "eval_results").is_dir()
    ]


def discover_projects(output_root: Path, agents: list[str]) -> list[str]:
    projects: set[str] = set()
    for agent in agents:
        eval_root = output_root / agent / "eval_results"
        if eval_root.exists():
            projects.update(path.name for path in eval_root.iterdir() if path.is_dir())
    return sorted(projects, key=lambda item: item.lower())


def parse_times(path: Path) -> tuple[datetime | None, datetime | None, float | None]:
    if not path.exists():
        return None, None, None
    first = last = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
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
    except Exception:
        return None, None, None
    return first, last, (last - first).total_seconds() if first and last else None


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def read_tail(path: Path, n: int = 8) -> str:
    text = read_text(path)
    if not text:
        return ""
    return "\\n".join(text.splitlines()[-n:])


def first_hit(text: str, patterns: list[tuple[str, str]]) -> tuple[str, str] | None:
    for line in text.splitlines():
        for label, pattern in patterns:
            if re.search(pattern, line, flags=re.IGNORECASE):
                return label, line.strip()[:240]
    return None


def all_hit_labels(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    labels = []
    for label, pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            labels.append(label)
    return labels


def load_infra(infra_root: Path) -> dict[tuple[str, str], dict]:
    infra: dict[tuple[str, str], dict] = {}
    if not infra_root.exists():
        return infra
    for project_dir in sorted(infra_root.iterdir(), key=lambda p: p.name.lower()):
        if not project_dir.is_dir() or project_dir.name in {"eval_infra_cache_logs", "maven_cache"}:
            continue
        for path in project_dir.glob("*.json"):
            infra[(project_dir.name, path.stem)] = load_json(path)
    return infra


def load_infra_durations(infra_root: Path) -> dict[tuple[str, str], float]:
    durations: dict[tuple[str, str], float] = {}
    log_root = infra_root / "eval_infra_cache_logs"
    if not log_root.exists():
        return durations
    for project_dir in sorted(log_root.iterdir(), key=lambda p: p.name.lower()):
        if not project_dir.is_dir():
            continue
        for instance_dir in project_dir.iterdir():
            _, _, duration = parse_times(instance_dir / "step2.log")
            if duration is not None:
                durations[(project_dir.name, instance_dir.name)] = duration
    return durations


def real_failure_after_last_external(text: str) -> str:
    last = -1
    for _, pattern in EXTERNAL_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            last = max(last, match.start())
    tail = text[last:] if last >= 0 else text[-5000:]
    return ";".join(all_hit_labels(tail, TEST_FAILURE_PATTERNS)[:5])


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root)
    infra_root = Path(args.infra_root)
    agents = args.agents or discover_agents(output_root)
    projects = args.projects or discover_projects(output_root, agents)
    infra = load_infra(infra_root)
    infra_durations = load_infra_durations(infra_root)

    rows = []
    project_agent_counts = []
    agent_results_on_invalid = []
    patch_apply_bad = []
    bad_json = []
    missing_status_or_log = []
    hard_candidates = []
    external_false = []
    duration_candidates = []
    test_error_rows = []
    agent_totals: dict[str, Counter] = {agent: Counter() for agent in agents}

    for project in projects:
        for agent in agents:
            result_dir = output_root / agent / "eval_results" / project
            done = passed = failed = 0
            suspicious_count = 0
            durations = []
            if not result_dir.exists():
                project_agent_counts.append((project, agent, done, passed, failed, None, suspicious_count))
                continue

            for result_path in sorted(result_dir.glob("*.json")):
                instance_id = result_path.stem
                payload = load_json(result_path)
                agent_totals[agent]["total"] += 1
                if "__load_error__" in payload:
                    bad_json.append((project, agent, instance_id, payload["__load_error__"]))
                    suspicious_count += 1
                    continue

                infra_payload = infra.get((project, instance_id))
                infra_valid = infra_payload and infra_payload.get("eval_infrastructure_valid") is True
                if not infra_valid:
                    agent_results_on_invalid.append(
                        (
                            project,
                            agent,
                            instance_id,
                            payload.get("agent_patch_passed"),
                            infra_payload.get("eval_infrastructure_valid") if infra_payload else "missing",
                        )
                    )
                    suspicious_count += 1
                    continue

                pass_value = payload.get("agent_patch_passed")
                if isinstance(pass_value, bool):
                    done += 1
                    agent_totals[agent]["pass" if pass_value else "fail"] += 1
                    if pass_value:
                        passed += 1
                    else:
                        failed += 1
                else:
                    bad_json.append((project, agent, instance_id, "agent_patch_passed_not_bool"))
                    suspicious_count += 1

                if payload.get("agent_patch_applied") is not True:
                    patch_apply_bad.append((project, agent, instance_id, payload.get("agent_patch_applied"), pass_value))
                    suspicious_count += 1

                log_path = output_root / agent / "logs" / project / instance_id / "step2.log"
                _, _, duration = parse_times(log_path)
                infra_duration = infra_durations.get((project, instance_id))
                text = read_text(log_path)
                if duration is not None:
                    durations.append(duration)
                else:
                    missing_status_or_log.append((project, agent, instance_id, pass_value, "missing_or_unparseable_step2_log"))
                    suspicious_count += 1
                if "agent_eval_status=" not in text:
                    missing_status_or_log.append((project, agent, instance_id, pass_value, "missing_agent_eval_status"))
                    suspicious_count += 1

                hard_hit = first_hit(text, HARD_PATTERNS)
                if hard_hit:
                    hard_candidates.append(
                        (
                            project,
                            agent,
                            instance_id,
                            pass_value,
                            round(duration, 1) if duration else "",
                            hard_hit[0],
                            hard_hit[1],
                        )
                    )
                    suspicious_count += 1

                external_hit = first_hit(text, EXTERNAL_PATTERNS)
                if pass_value is False and external_hit:
                    external_false.append(
                        (
                            project,
                            agent,
                            instance_id,
                            round(duration, 1) if duration else "",
                            external_hit[0],
                            external_hit[1],
                            real_failure_after_last_external(text),
                        )
                    )
                    suspicious_count += 1

                if args.include_test_error_signatures and pass_value is False:
                    test_labels = all_hit_labels(text, TEST_FAILURE_PATTERNS)
                    if test_labels:
                        test_error_rows.append(
                            (
                                project,
                                agent,
                                instance_id,
                                round(duration, 1) if duration else "",
                                ";".join(test_labels[:5]),
                                read_tail(log_path, 5),
                            )
                        )

                if duration is not None and infra_duration is not None:
                    if duration > max(3600, infra_duration * 4):
                        duration_candidates.append(
                            (project, agent, instance_id, pass_value, round(duration, 1), round(infra_duration, 1), "slow_vs_infra")
                        )
                        suspicious_count += 1
                    if duration < 3:
                        duration_candidates.append(
                            (project, agent, instance_id, pass_value, round(duration, 1), round(infra_duration, 1), "too_short")
                        )
                        suspicious_count += 1

            median_duration = median(durations) if durations else None
            project_agent_counts.append((project, agent, done, passed, failed, median_duration, suspicious_count))

    print("SUMMARY")
    print("project\tagent\tdone_valid_infra\tpass\tfail\tmedian_agent_sec\tsuspicious_count")
    for row in project_agent_counts:
        print("\t".join(str(item) if item is not None else "" for item in row))

    print("\nAGENT_TOTALS")
    print("agent\ttotal\tpass\tfail")
    for agent in agents:
        totals = agent_totals[agent]
        print(f"{agent}\t{totals['total']}\t{totals['pass']}\t{totals['fail']}")

    print("\nAGENT_RESULTS_ON_INVALID_OR_MISSING_INFRA")
    if agent_results_on_invalid:
        print("project\tagent\tinstance\tagent_patch_passed\tinfra_valid")
        for row in agent_results_on_invalid:
            print("\t".join(map(str, row)))
    else:
        print("none")

    print("\nPATCH_APPLY_BAD")
    if patch_apply_bad:
        print("project\tagent\tinstance\tagent_patch_applied\tagent_patch_passed")
        for row in patch_apply_bad:
            print("\t".join(map(str, row)))
    else:
        print("none")

    print("\nBAD_JSON_OR_MISSING_BOOLEAN")
    if bad_json:
        print("project\tagent\tinstance\treason")
        for row in bad_json:
            print("\t".join(map(str, row)))
    else:
        print("none")

    print("\nMISSING_STATUS_OR_LOG")
    if missing_status_or_log:
        print("project\tagent\tinstance\tagent_patch_passed\treason")
        for row in missing_status_or_log:
            print("\t".join(map(str, row)))
    else:
        print("none")

    print("\nHARD_INFRA_OR_CONTAINER_CANDIDATES")
    if hard_candidates:
        print("project\tagent\tinstance\tagent_patch_passed\tagent_sec\tpattern\tsample")
        for row in hard_candidates:
            print("\t".join(map(str, row)))
    else:
        print("none")

    print("\nEXTERNAL_PATTERN_FALSE")
    if external_false:
        print("project\tagent\tinstance\tagent_sec\tpattern\tsample\treal_failure_after_last_external")
        for row in external_false:
            print("\t".join(map(str, row)))
    else:
        print("none")

    print("\nDURATION_CANDIDATES")
    if duration_candidates:
        print("project\tagent\tinstance\tagent_patch_passed\tagent_sec\tinfra_sec\treason")
        for row in duration_candidates:
            print("\t".join(map(str, row)))
    else:
        print("none")

    if args.include_test_error_signatures:
        print("\nTEST_ERROR_SIGNATURE_FALSE")
        if test_error_rows:
            print("project\tagent\tinstance\tagent_sec\tlabels\ttail")
            for row in test_error_rows:
                print("\t".join(map(str, row)))
        else:
            print("none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
