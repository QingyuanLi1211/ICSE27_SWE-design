"""Instance loading, prompt rendering, and step-result validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .test_classifier import is_regression_success_matrix, is_trigger_success_matrix

JsonDict = dict[str, Any]

STATUS_VALUES = {"success", "blocked", "failed"}
CONFIDENCE_VALUES = {"high", "medium", "low"}
APPLY_CHECK_VALUES = {"pass", "fail"}
REBUILD_FROM_VALUES = {"none", "base", "env", "instance"}
BUILD_STATUS_VALUES = {"ready", "blocked", "failed", "skipped"}
VARIANT_STATUS_VALUES = {"ready", "blocked", "failed"}
VERIFICATION_STATUS_VALUES = {"PASS", "FAIL", "NOT_RUN"}


@dataclass(slots=True)
class InstanceSpec:
    id: str
    repo: str
    full_patch: str
    issue_code: str
    clean_code: str
    repo_source: str | None = None
    no_patch_ref: str | None = None
    commit_after: str | None = None
    design_issue_comment: str | None = None
    design_issue_related_patch: str | None = None
    seed_testfile_path: str | None = None
    docker_required: bool = False
    repo_version: str | None = None
    base_image_key: str | None = None
    container_repo_root: str | None = None
    docker_exec_template: str | None = None
    docker_build_hint: str | None = None
    test_command_hint: str | None = None
    install_or_bootstrap_hint: str | None = None
    extra: JsonDict = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: JsonDict) -> "InstanceSpec":
        known_fields = {
            "id",
            "repo",
            "full_patch",
            "issue_code",
            "clean_code",
            "repo_source",
            "no_patch_ref",
            "commit_after",
            "design_issue_comment",
            "design_issue_related_patch",
            "seed_testfile_path",
            "docker_required",
            "repo_version",
            "base_image_key",
            "container_repo_root",
            "docker_exec_template",
            "docker_build_hint",
            "test_command_hint",
            "install_or_bootstrap_hint",
        }
        missing = [name for name in ("id", "repo", "full_patch", "issue_code", "clean_code") if not data.get(name)]
        if missing:
            raise ValueError(f"Missing required instance fields: {', '.join(missing)}")
        extra = {key: value for key, value in data.items() if key not in known_fields}
        return cls(
            id=str(data["id"]),
            repo=str(data["repo"]),
            full_patch=str(data["full_patch"]),
            issue_code=str(data["issue_code"]),
            clean_code=str(data["clean_code"]),
            repo_source=_optional_string(data.get("repo_source")),
            no_patch_ref=_optional_string(data.get("no_patch_ref")),
            commit_after=_optional_string(data.get("commit_after")),
            design_issue_comment=_optional_string(data.get("design_issue_comment")),
            design_issue_related_patch=_optional_string(data.get("design_issue_related_patch")),
            seed_testfile_path=_optional_string(data.get("seed_testfile_path")),
            docker_required=bool(data.get("docker_required", False)),
            repo_version=_optional_string(data.get("repo_version")),
            base_image_key=_optional_string(data.get("base_image_key")),
            container_repo_root=_optional_string(data.get("container_repo_root")),
            docker_exec_template=_optional_string(data.get("docker_exec_template")),
            docker_build_hint=_optional_string(data.get("docker_build_hint")),
            test_command_hint=_optional_string(data.get("test_command_hint")),
            install_or_bootstrap_hint=_optional_string(data.get("install_or_bootstrap_hint")),
            extra=extra,
        )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def load_instances(path: Path) -> list[InstanceSpec]:
    if path.suffix.lower() == ".jsonl":
        records = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(InstanceSpec.from_mapping(json.loads(stripped)))
                except Exception as exc:
                    raise ValueError(f"Invalid JSONL record at line {line_number} in {path}: {exc}") from exc
        return records

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if "instances" not in payload or not isinstance(payload["instances"], list):
            raise ValueError("JSON input must be a list or an object containing an 'instances' list.")
        items = payload["instances"]
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("Unsupported instances file format.")
    return [InstanceSpec.from_mapping(item) for item in items]


def prompt_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def render_prompt(template: str, values: JsonDict) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", prompt_value(value))
    for key, value in values.items():
        rendered = rendered.replace(f"__CODEX_{key.upper()}__", prompt_value(value))
    return rendered


def build_retry_feedback(
    attempt_number: int,
    issues: list[str],
    *,
    previous_response: JsonDict | None,
    previous_stdout: str,
    previous_stderr: str,
) -> str:
    parts = [
        "",
        "# Outer Retry Feedback",
        f"This is retry attempt {attempt_number}. The previous attempt did not pass outer validation. Fix only the issues below, then re-run the task.",
    ]
    for issue in issues:
        parts.append(f"- {issue}")
    if previous_response is not None:
        parts.append("")
        parts.append("Previous JSON response:")
        parts.append(json.dumps(previous_response, ensure_ascii=False, indent=2))
    if previous_stdout.strip():
        parts.append("")
        parts.append("Previous stdout summary:")
        parts.append(previous_stdout.strip()[:4000])
    if previous_stderr.strip():
        parts.append("")
        parts.append("Previous stderr summary:")
        parts.append(previous_stderr.strip()[:4000])
    return "\n".join(parts)


def normalize_step_output(step_name: str, raw: JsonDict) -> JsonDict:
    if step_name != "step1":
        return dict(raw)
    normalized = dict(raw)
    if "包含测试改动" in normalized:
        normalized["contains_test_changes"] = normalized.pop("包含测试改动")
    if "包含测试改动原因" in normalized:
        normalized["contains_test_changes_reason"] = normalized.pop("包含测试改动原因")
    return normalized


def validate_step_output(step_name: str, raw: JsonDict, *, docker_required: bool) -> tuple[JsonDict, list[str]]:
    normalized = normalize_step_output(step_name, raw)
    errors = _validate_common(normalized)
    if step_name == "step1":
        errors.extend(_validate_step1(normalized))
    elif step_name == "step2":
        errors.extend(_validate_step2(normalized, docker_required=docker_required))
    elif step_name == "step3":
        errors.extend(_validate_step3(normalized))
    elif step_name == "step4":
        errors.extend(_validate_step4(normalized))
    else:
        errors.append(f"Unknown step name: {step_name}")
    return normalized, errors


def _validate_common(data: JsonDict) -> list[str]:
    errors: list[str] = []
    if data.get("status") not in STATUS_VALUES:
        errors.append("status must be one of success|blocked|failed")
    if data.get("confidence") not in CONFIDENCE_VALUES:
        errors.append("confidence must be one of high|medium|low")
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        errors.append("summary must be a non-empty string")
    return errors


def _validate_step1(data: JsonDict) -> list[str]:
    errors: list[str] = []
    if data.get("apply_check") not in APPLY_CHECK_VALUES:
        errors.append("step1.apply_check must be pass|fail")
    _validate_optional_path_list(data, "included_paths", errors)
    _validate_optional_path_list(data, "excluded_paths", errors)
    if data.get("contains_test_changes") not in {"yes", "no"}:
        errors.append("step1.contains_test_changes must be yes|no")
    if data.get("contains_test_changes") == "yes" and not _is_non_empty_string(data.get("contains_test_changes_reason")):
        errors.append("step1.contains_test_changes_reason must be present when contains_test_changes=yes")
    if data.get("status") == "success":
        if data.get("apply_check") != "pass":
            errors.append("step1 success requires apply_check=pass")
        if not data.get("included_paths"):
            errors.append("step1 success requires non-empty included_paths")
    return errors


def _validate_step2(data: JsonDict, *, docker_required: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(data.get("env_ready"), bool):
        errors.append("step2.env_ready must be boolean")
    if data.get("rebuild_from") not in REBUILD_FROM_VALUES:
        errors.append("step2.rebuild_from must be one of none|base|env|instance")
    for name in ("base_image_name", "env_image_name", "instance_image_name", "container_name"):
        value = data.get(name)
        if value is not None and not _is_non_empty_string(value):
            errors.append(f"step2.{name} must be a non-empty string or null")
    errors.extend(_validate_docker_build(data.get("docker_build"), docker_required=docker_required))
    errors.extend(_validate_variant_execution(data.get("variant_execution")))
    existing_tests = data.get("existing_tests")
    if not isinstance(existing_tests, dict):
        errors.append("step2.existing_tests must be an object")
    else:
        for key in ("regression_existing", "trigger_existing_strong", "trigger_existing_weak"):
            _validate_string_list(existing_tests.get(key), f"step2.existing_tests.{key}", errors)
    if data.get("step2_selected_test_command") is not None and not _is_non_empty_string(data.get("step2_selected_test_path")):
        errors.append("step2_selected_test_path must be present when step2_selected_test_command is present")
    if data.get("needs_new_trigger_test") not in {True, False}:
        errors.append("step2.needs_new_trigger_test must be boolean")
    if data.get("needs_new_regression_test") not in {True, False}:
        errors.append("step2.needs_new_regression_test must be boolean")
    if data.get("status") == "success":
        if data.get("env_ready") is not True:
            errors.append("step2 success requires env_ready=true")
        errors.extend(_require_variant_ready(data.get("variant_execution"), "step2"))
        if docker_required:
            for name in ("base_image_name", "env_image_name", "instance_image_name", "container_name"):
                if not _is_non_empty_string(data.get(name)):
                    errors.append(f"step2 success with docker_required=true requires non-empty {name}")
            docker_build = data.get("docker_build", {})
            for name in ("base_image", "env_image", "instance_image", "instance_container"):
                status = ((docker_build.get(name) or {}).get("status")) if isinstance(docker_build, dict) else None
                if status != "ready":
                    errors.append(f"step2 success with docker_required=true requires docker_build.{name}.status=ready")
        existing = existing_tests if isinstance(existing_tests, dict) else {}
        has_trigger = bool(existing.get("trigger_existing_strong")) or bool(existing.get("trigger_existing_weak"))
        if data.get("needs_new_trigger_test") is False and not has_trigger:
            errors.append("step2 needs_new_trigger_test=false requires at least one existing trigger test")
        if data.get("needs_new_regression_test") is False and not bool(existing.get("regression_existing")):
            errors.append("step2 needs_new_regression_test=false requires at least one regression_existing test")
    return errors


def _validate_step3(data: JsonDict) -> list[str]:
    errors: list[str] = []
    if data.get("result_matrix") not in {"FAIL/PASS/PASS", "FAIL/FAIL/PASS", None}:
        errors.append("step3.result_matrix must be FAIL/PASS/PASS | FAIL/FAIL/PASS | null")
    _validate_string_list(data.get("files_changed"), "step3.files_changed", errors)
    errors.extend(_validate_verification(data.get("verification"), ("no_patch", "design_issue_patch", "full_patch"), "step3"))
    cached_candidate = data.get("cached_candidate")
    if cached_candidate is not None:
        if not isinstance(cached_candidate, dict):
            errors.append("step3.cached_candidate must be an object or null")
        else:
            if cached_candidate.get("matrix") != "FAIL/PASS/FAIL":
                errors.append("step3.cached_candidate.matrix must be FAIL/PASS/FAIL")
            _validate_string_list(cached_candidate.get("files_changed"), "step3.cached_candidate.files_changed", errors)
    if data.get("status") == "success":
        if not is_trigger_success_matrix(data.get("result_matrix")):
            errors.append("step3 success requires a trigger success matrix")
        if not data.get("files_changed"):
            errors.append("step3 success requires non-empty files_changed")
        verification = data.get("verification") or {}
        if ((verification.get("no_patch") or {}).get("status")) != "FAIL":
            errors.append("step3 success requires verification.no_patch.status=FAIL")
        if ((verification.get("full_patch") or {}).get("status")) != "PASS":
            errors.append("step3 success requires verification.full_patch.status=PASS")
    if data.get("status") in {"blocked", "failed"} and data.get("result_matrix") is not None:
        errors.append("step3 blocked/failed requires result_matrix=null")
    return errors


def _validate_step4(data: JsonDict) -> list[str]:
    errors: list[str] = []
    if data.get("result_matrix") not in {"PASS/PASS/PASS", None}:
        errors.append("step4.result_matrix must be PASS/PASS/PASS | null")
    _validate_string_list(data.get("files_changed"), "step4.files_changed", errors)
    errors.extend(_validate_verification(data.get("verification"), ("no_patch", "design_issue_patch", "full_patch"), "step4"))
    if data.get("status") == "success":
        if not is_regression_success_matrix(data.get("result_matrix")):
            errors.append("step4 success requires PASS/PASS/PASS")
        if not data.get("files_changed"):
            errors.append("step4 success requires non-empty files_changed")
        verification = data.get("verification") or {}
        for key in ("no_patch", "design_issue_patch", "full_patch"):
            if ((verification.get(key) or {}).get("status")) != "PASS":
                errors.append(f"step4 success requires verification.{key}.status=PASS")
    if data.get("status") in {"blocked", "failed"} and data.get("result_matrix") is not None:
        errors.append("step4 blocked/failed requires result_matrix=null")
    return errors


def _validate_docker_build(value: Any, *, docker_required: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["step2.docker_build must be an object"]
    for key in ("base_image", "env_image", "instance_image", "instance_container"):
        entry = value.get(key)
        if not isinstance(entry, dict):
            errors.append(f"step2.docker_build.{key} must be an object")
            continue
        if entry.get("status") not in BUILD_STATUS_VALUES:
            errors.append(f"step2.docker_build.{key}.status must be ready|blocked|failed|skipped")
        if not _is_non_empty_string(entry.get("evidence")):
            errors.append(f"step2.docker_build.{key}.evidence must be a non-empty string")
        if not docker_required and entry.get("status") not in {"ready", "skipped"}:
            errors.append(f"step2.docker_build.{key}.status should be ready|skipped when docker_required=false")
    return errors


def _validate_variant_execution(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["variant_execution must be an object"]
    for key in ("no_patch", "design_issue_patch", "full_patch"):
        entry = value.get(key)
        if not isinstance(entry, dict):
            errors.append(f"variant_execution.{key} must be an object")
            continue
        if entry.get("status") not in VARIANT_STATUS_VALUES:
            errors.append(f"variant_execution.{key}.status must be ready|blocked|failed")
        if not _is_non_empty_string(entry.get("evidence")):
            errors.append(f"variant_execution.{key}.evidence must be a non-empty string")
    return errors


def _require_variant_ready(value: Any, prefix: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{prefix}.variant_execution must be an object"]
    for key in ("no_patch", "design_issue_patch", "full_patch"):
        if ((value.get(key) or {}).get("status")) != "ready":
            errors.append(f"{prefix}.variant_execution.{key}.status must be ready")
    return errors


def _validate_verification(value: Any, keys: tuple[str, ...], prefix: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{prefix}.verification must be an object"]
    for key in keys:
        entry = value.get(key)
        if not isinstance(entry, dict):
            errors.append(f"{prefix}.verification.{key} must be an object")
            continue
        if entry.get("status") not in VERIFICATION_STATUS_VALUES:
            errors.append(f"{prefix}.verification.{key}.status must be PASS|FAIL|NOT_RUN")
        if not _is_non_empty_string(entry.get("evidence")):
            errors.append(f"{prefix}.verification.{key}.evidence must be a non-empty string")
    return errors


def _validate_optional_path_list(data: JsonDict, key: str, errors: list[str]) -> None:
    value = data.get(key)
    if value is None:
        return
    _validate_string_list(value, key, errors)


def _validate_string_list(value: Any, name: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{name} must be a list")
        return
    for item in value:
        if not _is_non_empty_string(item):
            errors.append(f"{name} must contain only non-empty strings")
            return


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def parse_runner_command(text: str) -> list[str]:
    # Keep Windows path quoting behavior predictable.
    pattern = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')
    parts = []
    for match in pattern.finditer(text):
        parts.append(next(group for group in match.groups() if group is not None))
    if not parts:
        raise ValueError("runner command must not be empty")
    return parts
