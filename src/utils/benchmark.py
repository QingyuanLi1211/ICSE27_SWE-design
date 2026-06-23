"""读取 benchmark JSONL，并提供 repair/eval 共用的数据选择工具。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BenchmarkRecord:
    source_jsonl: Path
    instance_id: str
    repo: str
    repo_key: str
    base_commit: str
    problem_statement: str
    filename: str | None
    docker_image: str | None
    has_ground_truth_patch: bool
    patch: str
    trigger_test_patch: str
    regression_test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]


def load_records(jsonl_paths: list[Path]) -> list[BenchmarkRecord]:
    records: list[BenchmarkRecord] = []
    seen_instance_ids: dict[str, Path] = {}
    for jsonl_path in jsonl_paths:
        for raw_record in _load_jsonl(jsonl_path):
            instance_id = str(raw_record["instance_id"])
            if instance_id in seen_instance_ids:
                raise ValueError(
                    f"Duplicate instance_id `{instance_id}` found in both "
                    f"{seen_instance_ids[instance_id]} and {jsonl_path}."
                )
            seen_instance_ids[instance_id] = jsonl_path
            repo = str(raw_record["repo"])
            records.append(
                BenchmarkRecord(
                    source_jsonl=jsonl_path,
                    instance_id=instance_id,
                    repo=repo,
                    repo_key=_repo_key_from_slug(repo),
                    base_commit=str(raw_record["base_commit"]),
                    problem_statement=str(raw_record.get("problem_statement") or ""),
                    filename=_resolve_filename(raw_record),
                    docker_image=_resolve_optional_string(raw_record.get("docker_image")),
                    has_ground_truth_patch=_has_nonempty_patch(raw_record, "patch"),
                    patch=str(raw_record.get("patch") or ""),
                    trigger_test_patch=str(raw_record.get("trigger_test_patch") or raw_record.get("test_patch") or ""),
                    regression_test_patch=str(raw_record.get("regression_test_patch") or ""),
                    fail_to_pass=[str(item) for item in (raw_record.get("FAIL_TO_PASS") or [])],
                    pass_to_pass=[str(item) for item in (raw_record.get("PASS_TO_PASS") or [])],
                )
            )
    return records


def select_records(records: list[BenchmarkRecord], instance_id: str | None) -> list[BenchmarkRecord]:
    if instance_id is None:
        return list(records)
    matched = [record for record in records if record.instance_id == instance_id]
    if not matched:
        raise KeyError(f"Could not find instance `{instance_id}` in the supplied JSONL files.")
    return matched


def build_agent_view(record: BenchmarkRecord) -> dict[str, str]:
    return {
        "repo": record.repo,
        "problem_statement": record.problem_statement,
        "filename": record.filename or "",
    }


def require_docker_image(record: BenchmarkRecord) -> str:
    if not record.docker_image:
        raise ValueError(
            f"Instance `{record.instance_id}` is missing `docker_image` in {record.source_jsonl}."
        )
    return record.docker_image


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _resolve_filename(record: dict) -> str | None:
    filename = record.get("filename") or record.get("issue_comment_file")
    if not isinstance(filename, str) or not filename.strip():
        return None
    return filename.strip()


def _resolve_optional_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _has_nonempty_patch(record: dict, key: str) -> bool:
    value = record.get(key)
    return isinstance(value, str) and bool(value.strip())


def _repo_key_from_slug(repo_slug: str) -> str:
    tail = repo_slug.split("/")[-1]
    return tail.removesuffix(".git")
