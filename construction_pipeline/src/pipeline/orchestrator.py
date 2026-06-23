"""Deterministic outer orchestrator for the four-step Codex pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_runner import RunnerSettings, SubprocessCodexRunner, build_default_codex_command
from .git_ops import (
    GitError,
    apply_patch_text,
    changed_files,
    create_detached_worktree,
    ensure_mirror,
    remove_worktree,
    resolve_no_patch_ref,
    slugify_repo,
    worktree_is_clean,
    write_diff,
)
from .schemas import (
    InstanceSpec,
    build_retry_feedback,
    load_instances,
    normalize_step_output,
    parse_runner_command,
    render_prompt,
    validate_step_output,
)
from .test_classifier import is_trigger_success_matrix


STEP_ORDER = ("step1", "step2", "step3", "step4")


@dataclass(slots=True)
class PipelineConfig:
    root_dir: Path
    instances_file: Path
    artifacts_root: Path
    work_root: Path
    mirrors_root: Path
    task_paths: dict[str, Path]
    runner_settings: RunnerSettings
    step_timeouts: dict[str, int]
    repo_source_root: Path | None
    max_attempts: int
    max_workers: int
    refresh_mirrors: bool
    resume: bool
    keep_worktrees: bool
    fail_fast: bool
    enabled_steps: set[str]


@dataclass(slots=True)
class InstancePaths:
    root: Path
    results_dir: Path
    diffs_dir: Path
    attempts_dir: Path
    work_dir: Path
    manifest_path: Path


@dataclass(slots=True)
class StepPreparedContext:
    main_worktree: Path
    candidate_capture_repo_root: Path | None = None


@dataclass(slots=True)
class StepExecutionResult:
    raw: dict[str, Any]
    normalized: dict[str, Any]
    accepted: bool


class PipelineOrchestrator:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.runner = SubprocessCodexRunner(config.runner_settings)
        self.prompt_templates = {
            step_name: path.read_text(encoding="utf-8")
            for step_name, path in self.config.task_paths.items()
        }
        self.apply_check_script_path = (self.config.root_dir / "src" / "utils" / "check_patch_apply.py").resolve()

    def _timeout_for_step(self, step_name: str) -> int:
        return self.config.step_timeouts.get(step_name, self.config.runner_settings.timeout_seconds)

    def run(self) -> int:
        instances = load_instances(self.config.instances_file)
        failures = 0
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            future_map = {
                executor.submit(self.process_instance, instance): instance for instance in instances
            }
            for future in as_completed(future_map):
                instance = future_map[future]
                try:
                    final_status = future.result()
                except Exception as exc:
                    failures += 1
                    print(f"[{instance.id}] fatal orchestrator error: {exc}", file=sys.stderr)
                    if self.config.fail_fast:
                        raise
                    continue
                print(f"[{instance.id}] final_status={final_status}")
                if final_status != "success":
                    failures += 1
                    if self.config.fail_fast:
                        break
        return 0 if failures == 0 else 1

    def process_instance(self, instance: InstanceSpec) -> str:
        paths = self._instance_paths(instance.id)
        self._ensure_instance_dirs(paths)
        manifest: dict[str, Any] = self._load_manifest(paths.manifest_path, instance)

        repo_source = self._resolve_repo_source(instance)
        mirror_path = ensure_mirror(
            instance.repo,
            repo_source,
            self.config.mirrors_root,
            refresh=self.config.refresh_mirrors,
        )
        no_patch_ref = resolve_no_patch_ref(
            mirror_path,
            no_patch_ref=instance.no_patch_ref,
            commit_after=instance.commit_after,
        )
        docker_values = self._build_docker_values(instance)

        design_patch_path = paths.diffs_dir / "design_issue_related.diff"
        if "step1" in self.config.enabled_steps:
            step1_result = self._run_or_resume_step1(instance, mirror_path, no_patch_ref, paths)
            manifest["steps"]["step1"] = step1_result.normalized.get("status")
            self._write_manifest(paths.manifest_path, manifest)
            if step1_result.normalized.get("status") != "success":
                manifest["final_status"] = step1_result.normalized.get("status")
                self._write_manifest(paths.manifest_path, manifest)
                return str(manifest["final_status"])
        elif not design_patch_path.exists():
            if instance.design_issue_related_patch:
                design_patch_path.write_text(instance.design_issue_related_patch, encoding="utf-8")
            else:
                raise ValueError("step1 is disabled, but no design_issue_related_patch is available.")

        design_patch_text = design_patch_path.read_text(encoding="utf-8")

        step2_result = self._run_or_resume_step2(
            instance,
            mirror_path,
            no_patch_ref,
            design_patch_text,
            paths,
            docker_values,
        )
        manifest["steps"]["step2"] = step2_result.normalized.get("status")
        self._write_manifest(paths.manifest_path, manifest)
        if step2_result.normalized.get("status") != "success":
            manifest["final_status"] = step2_result.normalized.get("status")
            self._write_manifest(paths.manifest_path, manifest)
            return str(manifest["final_status"])

        step3_needed = bool(step2_result.normalized.get("needs_new_trigger_test"))
        step4_needed = bool(step2_result.normalized.get("needs_new_regression_test"))
        final_status = "success"

        if "step3" in self.config.enabled_steps and step3_needed:
            step3_result = self._run_or_resume_step3(
                instance,
                mirror_path,
                no_patch_ref,
                design_patch_text,
                step2_result.normalized,
                paths,
                docker_values,
            )
            manifest["steps"]["step3"] = step3_result.normalized.get("status")
            if step3_result.normalized.get("status") != "success":
                final_status = str(step3_result.normalized.get("status"))
        else:
            manifest["steps"]["step3"] = "skipped"

        if "step4" in self.config.enabled_steps and step4_needed:
            step4_result = self._run_or_resume_step4(
                instance,
                mirror_path,
                no_patch_ref,
                design_patch_text,
                step2_result.normalized,
                paths,
                docker_values,
            )
            manifest["steps"]["step4"] = step4_result.normalized.get("status")
            if step4_result.normalized.get("status") != "success" and final_status == "success":
                final_status = str(step4_result.normalized.get("status"))
        else:
            manifest["steps"]["step4"] = "skipped"

        manifest["final_status"] = final_status
        self._write_manifest(paths.manifest_path, manifest)
        return final_status

    def _run_or_resume_step1(
        self,
        instance: InstanceSpec,
        mirror_path: Path,
        no_patch_ref: str,
        paths: InstancePaths,
    ) -> StepExecutionResult:
        result_path = paths.results_dir / "step1_result.json"
        diff_path = paths.diffs_dir / "design_issue_related.diff"
        cached = self._load_cached_step(
            "step1",
            result_path,
            diff_path=diff_path,
            docker_required=instance.docker_required,
        )
        if cached is not None:
            return cached
        step_values = {
            "id": instance.id,
            "repo": instance.repo,
            "apply_check_script_path": str(self.apply_check_script_path),
            "comment": instance.design_issue_comment or "",
            "issue_code": instance.issue_code,
            "clean_code": instance.clean_code,
            "full_patch": instance.full_patch,
        }
        return self._execute_step(
            step_name="step1",
            instance=instance,
            mirror_path=mirror_path,
            no_patch_ref=no_patch_ref,
            paths=paths,
            prompt_values=step_values,
            prepare_context=self._prepare_step1_context,
            runtime_validator=self._runtime_validate_step1,
            finalize=self._finalize_step1,
        )

    def _run_or_resume_step2(
        self,
        instance: InstanceSpec,
        mirror_path: Path,
        no_patch_ref: str,
        design_patch_text: str,
        paths: InstancePaths,
        docker_values: dict[str, Any],
    ) -> StepExecutionResult:
        result_path = paths.results_dir / "step2_result.json"
        cached = self._load_cached_step("step2", result_path, docker_required=instance.docker_required)
        if cached is not None:
            return cached
        step_values = {
            "apply_check_script_path": str(self.apply_check_script_path),
            "id": instance.id,
            "repo": instance.repo,
            "docker_required": instance.docker_required,
            "repo_version": docker_values["repo_version"],
            "base_image_key": docker_values["base_image_key"],
            "container_repo_root": docker_values["container_repo_root"],
            "docker_exec_template": docker_values["docker_exec_template"],
            "docker_build_hint": docker_values["docker_build_hint"],
            "test_command_hint": instance.test_command_hint,
            "install_or_bootstrap_hint": instance.install_or_bootstrap_hint,
            "seed_testfile_path": instance.seed_testfile_path,
            "issue_code": instance.issue_code,
            "clean_code": instance.clean_code,
            "design_issue_related_patch": design_patch_text,
            "full_patch": instance.full_patch,
        }
        return self._execute_step(
            step_name="step2",
            instance=instance,
            mirror_path=mirror_path,
            no_patch_ref=no_patch_ref,
            paths=paths,
            prompt_values=step_values,
            prepare_context=self._prepare_clean_context,
            runtime_validator=self._runtime_validate_step2,
            finalize=self._finalize_step2,
        )

    def _run_or_resume_step3(
        self,
        instance: InstanceSpec,
        mirror_path: Path,
        no_patch_ref: str,
        design_patch_text: str,
        step2_result: dict[str, Any],
        paths: InstancePaths,
        docker_values: dict[str, Any],
    ) -> StepExecutionResult:
        result_path = paths.results_dir / "step3_result.json"
        diff_path = paths.diffs_dir / "trigger_test.diff"
        candidate_diff = paths.diffs_dir / "trigger_test_candidate.diff"
        cached = self._load_cached_step(
            "step3",
            result_path,
            diff_path=diff_path,
            candidate_diff_path=candidate_diff,
            docker_required=instance.docker_required,
        )
        if cached is not None:
            return cached
        step_values = {
            "apply_check_script_path": str(self.apply_check_script_path),
            "candidate_capture_repo_root": str(paths.work_dir / "step3" / "candidate_capture"),
            "docker_required": instance.docker_required,
            "id": instance.id,
            "repo": instance.repo,
            "container_repo_root": docker_values["container_repo_root"],
            "docker_exec_template": docker_values["docker_exec_template"],
            "step2_selected_test_path": step2_result.get("step2_selected_test_path"),
            "step2_selected_test_command": step2_result.get("step2_selected_test_command"),
            "test_command_hint": instance.test_command_hint,
            "install_or_bootstrap_hint": instance.install_or_bootstrap_hint,
            "seed_testfile_path": instance.seed_testfile_path,
            "issue_code": instance.issue_code,
            "clean_code": instance.clean_code,
            "design_issue_related_patch": design_patch_text,
            "full_patch": instance.full_patch,
        }
        return self._execute_step(
            step_name="step3",
            instance=instance,
            mirror_path=mirror_path,
            no_patch_ref=no_patch_ref,
            paths=paths,
            prompt_values=step_values,
            prepare_context=self._prepare_step3_context,
            runtime_validator=self._runtime_validate_step3,
            finalize=self._finalize_step3,
        )

    def _run_or_resume_step4(
        self,
        instance: InstanceSpec,
        mirror_path: Path,
        no_patch_ref: str,
        design_patch_text: str,
        step2_result: dict[str, Any],
        paths: InstancePaths,
        docker_values: dict[str, Any],
    ) -> StepExecutionResult:
        result_path = paths.results_dir / "step4_result.json"
        diff_path = paths.diffs_dir / "regression_test.diff"
        cached = self._load_cached_step(
            "step4",
            result_path,
            diff_path=diff_path,
            docker_required=instance.docker_required,
        )
        if cached is not None:
            return cached
        step_values = {
            "apply_check_script_path": str(self.apply_check_script_path),
            "docker_required": instance.docker_required,
            "id": instance.id,
            "repo": instance.repo,
            "container_repo_root": docker_values["container_repo_root"],
            "docker_exec_template": docker_values["docker_exec_template"],
            "step2_selected_test_path": step2_result.get("step2_selected_test_path"),
            "step2_selected_test_command": step2_result.get("step2_selected_test_command"),
            "test_command_hint": instance.test_command_hint,
            "install_or_bootstrap_hint": instance.install_or_bootstrap_hint,
            "seed_testfile_path": instance.seed_testfile_path,
            "issue_code": instance.issue_code,
            "clean_code": instance.clean_code,
            "design_issue_related_patch": design_patch_text,
            "full_patch": instance.full_patch,
        }
        return self._execute_step(
            step_name="step4",
            instance=instance,
            mirror_path=mirror_path,
            no_patch_ref=no_patch_ref,
            paths=paths,
            prompt_values=step_values,
            prepare_context=self._prepare_clean_context,
            runtime_validator=self._runtime_validate_step4,
            finalize=self._finalize_step4,
        )

    def _execute_step(
        self,
        *,
        step_name: str,
        instance: InstanceSpec,
        mirror_path: Path,
        no_patch_ref: str,
        paths: InstancePaths,
        prompt_values: dict[str, Any],
        prepare_context,
        runtime_validator,
        finalize,
    ) -> StepExecutionResult:
        base_prompt = render_prompt(self.prompt_templates[step_name], prompt_values)
        previous_raw: dict[str, Any] | None = None
        previous_stdout = ""
        previous_stderr = ""
        issues: list[str] = []

        for attempt in range(1, self.config.max_attempts + 1):
            attempt_dir = paths.attempts_dir / f"{step_name}_attempt_{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=True)

            try:
                context = prepare_context(step_name, mirror_path, no_patch_ref, paths, instance, prompt_values)
            except Exception as exc:
                synthetic = self._synthetic_blocked_result(step_name, f"orchestrator prepare failed: {exc}")
                self._write_json(attempt_dir / "synthetic_result.json", synthetic)
                self._write_json(paths.results_dir / f"{step_name}_result.json", synthetic)
                return StepExecutionResult(synthetic, normalize_step_output(step_name, synthetic), True)

            prompt = base_prompt
            if issues:
                prompt += build_retry_feedback(
                    attempt,
                    issues,
                    previous_response=previous_raw,
                    previous_stdout=previous_stdout,
                    previous_stderr=previous_stderr,
                )

            runner_output = self.runner.run(
                prompt,
                workdir=context.main_worktree,
                attempt_dir=attempt_dir,
                timeout_seconds=self._timeout_for_step(step_name),
            )
            previous_stdout = runner_output.stdout
            previous_stderr = runner_output.stderr

            if runner_output.parsed_json is None:
                issues = [runner_output.parse_error or "runner did not return parseable JSON"]
                if runner_output.timed_out:
                    issues.append("runner timed out")
                self._write_text(attempt_dir / "validator_feedback.txt", "\n".join(issues))
                self._cleanup_context(mirror_path, context)
                continue

            previous_raw = runner_output.parsed_json
            self._write_json(attempt_dir / "raw_result.json", previous_raw)

            normalized, validation_errors = validate_step_output(
                step_name,
                previous_raw,
                docker_required=instance.docker_required,
            )
            validation_errors.extend(runtime_validator(normalized, context))
            self._write_json(attempt_dir / "normalized_result.json", normalized)

            if validation_errors:
                issues = validation_errors
                self._write_text(attempt_dir / "validator_feedback.txt", "\n".join(validation_errors))
                self._cleanup_context(mirror_path, context)
                continue

            try:
                finalize(normalized, context, paths)
            except Exception as exc:
                issues = [f"artifact finalization failed: {exc}"]
                self._write_text(attempt_dir / "validator_feedback.txt", "\n".join(issues))
                self._cleanup_context(mirror_path, context)
                continue
            self._write_json(paths.results_dir / f"{step_name}_result.json", previous_raw)
            self._cleanup_context(mirror_path, context)
            return StepExecutionResult(previous_raw, normalized, True)

        synthetic = self._synthetic_blocked_result(step_name, "; ".join(issues) or "runner/validator retries exhausted")
        self._write_json(paths.results_dir / f"{step_name}_result.json", synthetic)
        return StepExecutionResult(synthetic, normalize_step_output(step_name, synthetic), True)

    def _prepare_step1_context(
        self,
        step_name: str,
        mirror_path: Path,
        no_patch_ref: str,
        paths: InstancePaths,
        instance: InstanceSpec,
        prompt_values: dict[str, Any],
    ) -> StepPreparedContext:
        worktree = paths.work_dir / step_name / "main"
        create_detached_worktree(mirror_path, worktree, no_patch_ref)
        apply_result = apply_patch_text(worktree, instance.full_patch)
        if apply_result.returncode != 0:
            raise GitError(f"step1 pre-apply full_patch failed: {apply_result.stderr.strip()}")
        return StepPreparedContext(main_worktree=worktree)

    def _prepare_clean_context(
        self,
        step_name: str,
        mirror_path: Path,
        no_patch_ref: str,
        paths: InstancePaths,
        instance: InstanceSpec,
        prompt_values: dict[str, Any],
    ) -> StepPreparedContext:
        worktree = paths.work_dir / step_name / "main"
        create_detached_worktree(mirror_path, worktree, no_patch_ref)
        return StepPreparedContext(main_worktree=worktree)

    def _prepare_step3_context(
        self,
        step_name: str,
        mirror_path: Path,
        no_patch_ref: str,
        paths: InstancePaths,
        instance: InstanceSpec,
        prompt_values: dict[str, Any],
    ) -> StepPreparedContext:
        main_worktree = paths.work_dir / step_name / "main"
        candidate_root = Path(str(prompt_values["candidate_capture_repo_root"]))
        create_detached_worktree(mirror_path, main_worktree, no_patch_ref)
        create_detached_worktree(mirror_path, candidate_root, no_patch_ref)
        return StepPreparedContext(
            main_worktree=main_worktree,
            candidate_capture_repo_root=candidate_root,
        )

    def _runtime_validate_step1(self, normalized: dict[str, Any], context: StepPreparedContext) -> list[str]:
        errors: list[str] = []
        if normalized.get("status") == "success":
            changed = changed_files(context.main_worktree)
            if not changed:
                errors.append("step1 success requires non-empty git diff HEAD")
            included = set(normalized.get("included_paths") or [])
            if included and set(changed) != included:
                errors.append("step1 included_paths must match changed files in the final worktree")
        elif not worktree_is_clean(context.main_worktree):
            errors.append("step1 blocked/failed requires a clean main worktree")
        return errors

    def _runtime_validate_step2(self, normalized: dict[str, Any], context: StepPreparedContext) -> list[str]:
        errors: list[str] = []
        if not worktree_is_clean(context.main_worktree):
            errors.append("step2 must leave the main worktree clean")
        return errors

    def _runtime_validate_step3(self, normalized: dict[str, Any], context: StepPreparedContext) -> list[str]:
        errors: list[str] = []
        candidate_root = context.candidate_capture_repo_root
        assert candidate_root is not None
        if normalized.get("status") == "success":
            changed = changed_files(context.main_worktree)
            if not changed:
                errors.append("step3 success requires non-empty main worktree diff")
            if set(changed) != set(normalized.get("files_changed") or []):
                errors.append("step3 files_changed must match the final main-worktree changed files")
            if not worktree_is_clean(candidate_root):
                errors.append("step3 success requires candidate_capture_repo_root to be clean")
        elif normalized.get("status") == "failed" and normalized.get("cached_candidate") is not None:
            cached = normalized["cached_candidate"]
            if not worktree_is_clean(context.main_worktree):
                errors.append("step3 failed with cached_candidate requires a clean main worktree")
            changed = changed_files(candidate_root)
            if set(changed) != set(cached.get("files_changed") or []):
                errors.append("step3 cached_candidate.files_changed must match candidate_capture_repo_root")
            if not changed:
                errors.append("step3 cached_candidate requires non-empty candidate_capture_repo_root diff")
        else:
            if not worktree_is_clean(context.main_worktree):
                errors.append("step3 blocked/failed without cached_candidate requires a clean main worktree")
            if not worktree_is_clean(candidate_root):
                errors.append("step3 blocked/failed without cached_candidate requires a clean candidate_capture_repo_root")
        if normalized.get("status") == "success" and not is_trigger_success_matrix(normalized.get("result_matrix")):
            errors.append("step3 success matrix must be FAIL/PASS/PASS or FAIL/FAIL/PASS")
        return errors

    def _runtime_validate_step4(self, normalized: dict[str, Any], context: StepPreparedContext) -> list[str]:
        errors: list[str] = []
        if normalized.get("status") == "success":
            changed = changed_files(context.main_worktree)
            if not changed:
                errors.append("step4 success requires non-empty main worktree diff")
            if set(changed) != set(normalized.get("files_changed") or []):
                errors.append("step4 files_changed must match the final main-worktree changed files")
        elif not worktree_is_clean(context.main_worktree):
            errors.append("step4 blocked/failed requires a clean main worktree")
        return errors

    def _finalize_step1(self, normalized: dict[str, Any], context: StepPreparedContext, paths: InstancePaths) -> None:
        if normalized.get("status") == "success":
            diff_text = write_diff(context.main_worktree, paths.diffs_dir / "design_issue_related.diff")
            if not diff_text.strip():
                raise ValueError("step1 success but diff artifact is empty")

    def _finalize_step2(self, normalized: dict[str, Any], context: StepPreparedContext, paths: InstancePaths) -> None:
        return None

    def _finalize_step3(self, normalized: dict[str, Any], context: StepPreparedContext, paths: InstancePaths) -> None:
        if normalized.get("status") == "success":
            diff_text = write_diff(context.main_worktree, paths.diffs_dir / "trigger_test.diff")
            if not diff_text.strip():
                raise ValueError("step3 success but trigger_test.diff is empty")
        elif normalized.get("status") == "failed" and normalized.get("cached_candidate") is not None:
            candidate_root = context.candidate_capture_repo_root
            assert candidate_root is not None
            diff_text = write_diff(candidate_root, paths.diffs_dir / "trigger_test_candidate.diff")
            if not diff_text.strip():
                raise ValueError("step3 cached candidate exists but trigger_test_candidate.diff is empty")

    def _finalize_step4(self, normalized: dict[str, Any], context: StepPreparedContext, paths: InstancePaths) -> None:
        if normalized.get("status") == "success":
            diff_text = write_diff(context.main_worktree, paths.diffs_dir / "regression_test.diff")
            if not diff_text.strip():
                raise ValueError("step4 success but regression_test.diff is empty")

    def _load_cached_step(
        self,
        step_name: str,
        result_path: Path,
        *,
        diff_path: Path | None = None,
        candidate_diff_path: Path | None = None,
        docker_required: bool,
    ) -> StepExecutionResult | None:
        if not self.config.resume or not result_path.exists():
            return None
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        normalized, errors = validate_step_output(step_name, raw, docker_required=docker_required)
        if errors:
            return None
        if diff_path is not None and normalized.get("status") == "success":
            if not diff_path.exists() or not diff_path.read_text(encoding="utf-8").strip():
                return None
        if candidate_diff_path is not None and normalized.get("status") == "failed" and normalized.get("cached_candidate") is not None:
            if not candidate_diff_path.exists() or not candidate_diff_path.read_text(encoding="utf-8").strip():
                return None
        return StepExecutionResult(raw, normalized, True)

    def _cleanup_context(self, mirror_path: Path, context: StepPreparedContext) -> None:
        if self.config.keep_worktrees:
            return
        remove_worktree(mirror_path, context.main_worktree)
        if context.candidate_capture_repo_root is not None:
            remove_worktree(mirror_path, context.candidate_capture_repo_root)

    def _resolve_repo_source(self, instance: InstanceSpec) -> str:
        if instance.repo_source:
            return instance.repo_source
        if self.config.repo_source_root is not None:
            direct = self.config.repo_source_root / instance.repo
            if direct.exists():
                return str(direct)
            tail = self.config.repo_source_root / instance.repo.split("/")[-1]
            if tail.exists():
                return str(tail)
        return f"https://github.com/{instance.repo}.git"

    def _build_docker_values(self, instance: InstanceSpec) -> dict[str, Any]:
        repo_version = instance.repo_version or "default"
        repo_short_name = instance.repo.split("/")[-1]
        base_image_key = instance.base_image_key or "python311-slim-bookworm"
        derived_env_image_key = repo_version
        derived_instance_image_key = (
            instance.id if instance.id.startswith(f"{repo_short_name}__") else f"{repo_short_name}__{instance.id}"
        )
        container_repo_root = instance.container_repo_root or "/workspace/repo"
        docker_exec_template = instance.docker_exec_template or (
            f'docker exec pipeline-{instance.id} sh -lc "__CMD__"'
        )
        docker_build_hint = instance.docker_build_hint or (
            f"repo_version={repo_version}, base_image_key={base_image_key}, "
            f"derived env image key={derived_env_image_key}, "
            f"derived instance image key={derived_instance_image_key}"
        )
        return {
            "repo_version": repo_version,
            "base_image_key": base_image_key,
            "container_repo_root": container_repo_root,
            "docker_exec_template": docker_exec_template,
            "docker_build_hint": docker_build_hint,
        }

    def _instance_paths(self, instance_id: str) -> InstancePaths:
        root = self.config.artifacts_root / instance_id
        return InstancePaths(
            root=root,
            results_dir=root / "results",
            diffs_dir=root / "diffs",
            attempts_dir=root / "attempts",
            work_dir=self.config.work_root / instance_id,
            manifest_path=root / "manifest.json",
        )

    def _ensure_instance_dirs(self, paths: InstancePaths) -> None:
        paths.results_dir.mkdir(parents=True, exist_ok=True)
        paths.diffs_dir.mkdir(parents=True, exist_ok=True)
        paths.attempts_dir.mkdir(parents=True, exist_ok=True)
        paths.work_dir.mkdir(parents=True, exist_ok=True)

    def _load_manifest(self, manifest_path: Path, instance: InstanceSpec) -> dict[str, Any]:
        if manifest_path.exists():
            try:
                return json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        manifest = {
            "id": instance.id,
            "repo": instance.repo,
            "results": {
                "step1": None,
                "step2": None,
                "step3": None,
                "step4": None,
            },
            "diffs": {
                "design_issue_related": None,
                "trigger_test": None,
                "trigger_test_candidate": None,
                "regression_test": None,
            },
            "steps": {},
            "final_status": None,
            "notes": None,
        }
        self._populate_manifest_paths(manifest, self._instance_paths(instance.id))
        return manifest

    def _populate_manifest_paths(self, manifest: dict[str, Any], paths: InstancePaths) -> None:
        manifest["results"]["step1"] = str(paths.results_dir / "step1_result.json") if (paths.results_dir / "step1_result.json").exists() else None
        manifest["results"]["step2"] = str(paths.results_dir / "step2_result.json") if (paths.results_dir / "step2_result.json").exists() else None
        manifest["results"]["step3"] = str(paths.results_dir / "step3_result.json") if (paths.results_dir / "step3_result.json").exists() else None
        manifest["results"]["step4"] = str(paths.results_dir / "step4_result.json") if (paths.results_dir / "step4_result.json").exists() else None
        manifest["diffs"]["design_issue_related"] = str(paths.diffs_dir / "design_issue_related.diff") if (paths.diffs_dir / "design_issue_related.diff").exists() else None
        manifest["diffs"]["trigger_test"] = str(paths.diffs_dir / "trigger_test.diff") if (paths.diffs_dir / "trigger_test.diff").exists() else None
        manifest["diffs"]["trigger_test_candidate"] = str(paths.diffs_dir / "trigger_test_candidate.diff") if (paths.diffs_dir / "trigger_test_candidate.diff").exists() else None
        manifest["diffs"]["regression_test"] = str(paths.diffs_dir / "regression_test.diff") if (paths.diffs_dir / "regression_test.diff").exists() else None

    def _write_manifest(self, manifest_path: Path, manifest: dict[str, Any]) -> None:
        self._populate_manifest_paths(manifest, self._instance_paths(manifest["id"]))
        self._write_json(manifest_path, manifest)

    def _synthetic_blocked_result(self, step_name: str, summary: str) -> dict[str, Any]:
        if step_name == "step1":
            return {
                "status": "blocked",
                "summary": summary,
                "confidence": "low",
                "apply_check": "fail",
                "included_paths": None,
                "excluded_paths": None,
                "包含测试改动": "no",
                "包含测试改动原因": None,
            }
        if step_name == "step2":
            return {
                "status": "blocked",
                "summary": summary,
                "confidence": "low",
                "env_ready": False,
                "rebuild_from": "none",
                "base_image_name": None,
                "env_image_name": None,
                "instance_image_name": None,
                "container_name": None,
                "docker_build": {
                    "base_image": {"status": "blocked", "evidence": "orchestrator blocked"},
                    "env_image": {"status": "blocked", "evidence": "orchestrator blocked"},
                    "instance_image": {"status": "blocked", "evidence": "orchestrator blocked"},
                    "instance_container": {"status": "blocked", "evidence": "orchestrator blocked"},
                },
                "variant_execution": {
                    "no_patch": {"status": "blocked", "evidence": "orchestrator blocked"},
                    "design_issue_patch": {"status": "blocked", "evidence": "orchestrator blocked"},
                    "full_patch": {"status": "blocked", "evidence": "orchestrator blocked"},
                },
                "existing_tests": {
                    "regression_existing": [],
                    "trigger_existing_strong": [],
                    "trigger_existing_weak": [],
                },
                "step2_selected_test_path": None,
                "step2_selected_test_command": None,
                "needs_new_trigger_test": True,
                "needs_new_regression_test": True,
                "risks": summary[:120],
            }
        if step_name == "step3":
            return {
                "status": "blocked",
                "summary": summary,
                "confidence": "low",
                "result_matrix": None,
                "files_changed": [],
                "test_command": None,
                "verification": {
                    "no_patch": {"status": "NOT_RUN", "evidence": "orchestrator blocked"},
                    "design_issue_patch": {"status": "NOT_RUN", "evidence": "orchestrator blocked"},
                    "full_patch": {"status": "NOT_RUN", "evidence": "orchestrator blocked"},
                },
                "cached_candidate": None,
                "risks": summary[:120],
            }
        return {
            "status": "blocked",
            "summary": summary,
            "confidence": "low",
            "result_matrix": None,
            "files_changed": [],
            "test_command": None,
            "verification": {
                "no_patch": {"status": "NOT_RUN", "evidence": "orchestrator blocked"},
                "design_issue_patch": {"status": "NOT_RUN", "evidence": "orchestrator blocked"},
                "full_patch": {"status": "NOT_RUN", "evidence": "orchestrator blocked"},
            },
            "risks": summary[:120],
        }

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the deterministic outer Codex pipeline.")
    parser.add_argument("--instances", required=True, help="Path to a JSON or JSONL instances file.")
    parser.add_argument("--artifacts-root", default="artifacts", help="Artifact output directory.")
    parser.add_argument("--work-root", default=".pipeline_work", help="Temporary worktree directory.")
    parser.add_argument("--mirrors-root", default=".pipeline_mirrors", help="Shared git mirror cache.")
    parser.add_argument("--repo-source-root", default=None, help="Optional root directory containing repo clones.")
    parser.add_argument("--task1-prompt", default="task1.md", help="Path to task1 prompt.")
    parser.add_argument("--task2-prompt", default="task2.md", help="Path to task2 prompt.")
    parser.add_argument("--task3-prompt", default="task3.md", help="Path to task3 prompt.")
    parser.add_argument("--task4-prompt", default="task4.md", help="Path to task4 prompt.")
    parser.add_argument(
        "--runner-command",
        default=None,
        help=(
            "Optional override for the Codex invocation command. Use __PROMPT_FILE__, "
            "__WORKDIR__, and/or __LAST_MESSAGE_FILE__ placeholders. If omitted, the "
            "pipeline uses an embedded `codex exec` command that assumes local CLI login."
        ),
    )
    parser.add_argument(
        "--codex-cli-path",
        default=None,
        help="Optional path/name for the Codex CLI binary when using the embedded runner.",
    )
    parser.add_argument(
        "--codex-model",
        default="gpt-5.2",
        help="Model passed to the embedded `codex exec` runner.",
    )
    parser.add_argument(
        "--codex-reasoning-effort",
        default="high",
        help="Reasoning effort passed to the embedded `codex exec` runner.",
    )
    parser.add_argument("--runner-timeout", type=int, default=1800, help="Runner timeout in seconds.")
    parser.add_argument("--step1-timeout", type=int, default=None, help="Optional timeout override for step1 in seconds.")
    parser.add_argument("--step2-timeout", type=int, default=None, help="Optional timeout override for step2 in seconds.")
    parser.add_argument("--step3-timeout", type=int, default=None, help="Optional timeout override for step3 in seconds.")
    parser.add_argument("--step4-timeout", type=int, default=None, help="Optional timeout override for step4 in seconds.")
    parser.add_argument("--max-attempts", type=int, default=3, help="Maximum attempts per step.")
    parser.add_argument("--max-workers", type=int, default=1, help="Maximum concurrent instances.")
    parser.add_argument("--refresh-mirrors", action="store_true", help="Refresh shared git mirrors before use.")
    parser.add_argument("--keep-worktrees", action="store_true", help="Keep temporary worktrees for debugging.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first failing instance.")
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse valid existing artifacts when possible.",
    )
    parser.add_argument(
        "--steps",
        default="1,2,3,4",
        help="Comma-separated subset of steps to run, e.g. 1,2 or 2,3,4.",
    )
    return parser


def parse_config(argv: list[str] | None = None) -> PipelineConfig:
    parser = build_parser()
    args = parser.parse_args(argv)
    root_dir = Path(__file__).resolve().parents[2]
    enabled_steps = {f"step{part.strip()}" for part in args.steps.split(",") if part.strip()}
    for step_name in enabled_steps:
        if step_name not in STEP_ORDER:
            raise ValueError(f"Unsupported step name: {step_name}")
    task_paths = {
        "step1": (root_dir / args.task1_prompt).resolve(),
        "step2": (root_dir / args.task2_prompt).resolve(),
        "step3": (root_dir / args.task3_prompt).resolve(),
        "step4": (root_dir / args.task4_prompt).resolve(),
    }
    if args.runner_command:
        runner_command = parse_runner_command(args.runner_command)
    else:
        runner_command = build_default_codex_command(
            cli_path=args.codex_cli_path,
            model=args.codex_model,
            reasoning_effort=args.codex_reasoning_effort,
        )
    runner_settings = RunnerSettings(command=runner_command, timeout_seconds=args.runner_timeout)
    step_timeouts = {
        "step1": max(1, args.step1_timeout or args.runner_timeout),
        "step2": max(1, args.step2_timeout or args.runner_timeout),
        "step3": max(1, args.step3_timeout or args.runner_timeout),
        "step4": max(1, args.step4_timeout or args.runner_timeout),
    }
    return PipelineConfig(
        root_dir=root_dir,
        instances_file=(root_dir / args.instances).resolve(),
        artifacts_root=(root_dir / args.artifacts_root).resolve(),
        work_root=(root_dir / args.work_root).resolve(),
        mirrors_root=(root_dir / args.mirrors_root).resolve(),
        task_paths=task_paths,
        runner_settings=runner_settings,
        step_timeouts=step_timeouts,
        repo_source_root=((root_dir / args.repo_source_root).resolve() if args.repo_source_root else None),
        max_attempts=max(1, args.max_attempts),
        max_workers=max(1, args.max_workers),
        refresh_mirrors=args.refresh_mirrors,
        resume=args.resume,
        keep_worktrees=args.keep_worktrees,
        fail_fast=args.fail_fast,
        enabled_steps=enabled_steps,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_config(argv)
    orchestrator = PipelineOrchestrator(config)
    return orchestrator.run()


if __name__ == "__main__":
    raise SystemExit(main())
