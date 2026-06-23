"""共享 eval 实现：准备 eval workspace、打补丁、跑测试并生成最小结果 JSON。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

from .benchmark import BenchmarkRecord, require_docker_image
from .docker_bundle import ensure_image_loaded
from .process import rmtree_force
from .status import new_agent_eval_status, new_eval_status, new_infrastructure_eval_status
from .worktree import copy_prepared_tree, prepare_pristine_tree


@dataclass(slots=True)
class CommandOutcome:
    command: str
    returncode: int
    passed: bool
    stdout_excerpt: str
    stderr_excerpt: str


def evaluate_agent_patch(
    *,
    record: BenchmarkRecord,
    bundle_root: Path,
    agent_patch_path: Path,
    work_root: Path,
    logger,
    runner,
) -> dict:
    """Compatibility wrapper for the old combined eval JSON shape."""
    status = new_eval_status()
    infra_status, agent_status = evaluate_full(
        record=record,
        bundle_root=bundle_root,
        agent_patch_path=agent_patch_path,
        work_root=work_root,
        logger=logger,
        runner=runner,
    )
    status.update(infra_status)
    if agent_status is not None:
        for key in ("agent_patch_applied", "trigger_test_passed", "regression_test_passed", "agent_patch_passed"):
            status[key] = agent_status[key]
    return status


def evaluate_infrastructure(
    *,
    record: BenchmarkRecord,
    bundle_root: Path,
    work_root: Path,
    logger,
    runner,
) -> dict:
    status = new_infrastructure_eval_status()
    prepared = _prepare_eval_workspace(
        record=record,
        bundle_root=bundle_root,
        work_root=work_root,
        logger=logger,
        runner=runner,
        status=status,
    )
    if prepared is None:
        return status
    image_tag, pristine_dir = prepared
    _evaluate_infrastructure_from_pristine(
        record=record,
        image_tag=image_tag,
        pristine_dir=pristine_dir,
        work_root=work_root,
        status=status,
        logger=logger,
        runner=runner,
    )
    return status


def evaluate_agent_only(
    *,
    record: BenchmarkRecord,
    bundle_root: Path,
    agent_patch_path: Path,
    work_root: Path,
    logger,
    runner,
) -> dict:
    status = new_agent_eval_status()
    prepared = _prepare_eval_workspace(
        record=record,
        bundle_root=bundle_root,
        work_root=work_root,
        logger=logger,
        runner=runner,
        status=status,
    )
    if prepared is None:
        return status
    image_tag, pristine_dir = prepared
    _evaluate_agent_from_pristine(
        record=record,
        image_tag=image_tag,
        pristine_dir=pristine_dir,
        agent_patch_path=agent_patch_path,
        work_root=work_root,
        status=status,
        logger=logger,
        runner=runner,
    )
    return status


def evaluate_full(
    *,
    record: BenchmarkRecord,
    bundle_root: Path,
    agent_patch_path: Path,
    work_root: Path,
    logger,
    runner,
) -> tuple[dict, dict | None]:
    infra_status = new_infrastructure_eval_status()
    prepared = _prepare_eval_workspace(
        record=record,
        bundle_root=bundle_root,
        work_root=work_root,
        logger=logger,
        runner=runner,
        status=infra_status,
    )
    if prepared is None:
        return infra_status, None
    image_tag, pristine_dir = prepared
    _evaluate_infrastructure_from_pristine(
        record=record,
        image_tag=image_tag,
        pristine_dir=pristine_dir,
        work_root=work_root,
        status=infra_status,
        logger=logger,
        runner=runner,
    )
    if infra_status["eval_infrastructure_valid"] is not True:
        logger.log("skip agent patch evaluation because eval_infrastructure_valid=False")
        return infra_status, None

    agent_status = new_agent_eval_status()
    agent_status["eval_workspace_prepared"] = True
    _evaluate_agent_from_pristine(
        record=record,
        image_tag=image_tag,
        pristine_dir=pristine_dir,
        agent_patch_path=agent_patch_path,
        work_root=work_root,
        status=agent_status,
        logger=logger,
        runner=runner,
    )
    return infra_status, agent_status


def _prepare_eval_workspace(
    *,
    record: BenchmarkRecord,
    bundle_root: Path,
    work_root: Path,
    logger,
    runner,
    status: dict,
) -> tuple[str, Path] | None:
    image_tag = require_docker_image(record)
    pristine_dir = work_root / "pristine"
    try:
        ensure_image_loaded(bundle_root, image_tag, runner=runner)
        prepare_pristine_tree(
            image_tag=image_tag,
            base_commit=record.base_commit,
            repo_slug=record.repo,
            destination=pristine_dir,
            runner=runner,
        )
        status["eval_workspace_prepared"] = True
        logger.log(f"eval_workspace_prepared=True image={image_tag}")
    except Exception as exc:  # noqa: BLE001
        status["eval_workspace_prepared"] = False
        logger.log(f"eval_workspace_prepared=False error={exc}")
        return None
    return image_tag, pristine_dir


def _evaluate_infrastructure_from_pristine(
    *,
    record: BenchmarkRecord,
    image_tag: str,
    pristine_dir: Path,
    work_root: Path,
    status: dict,
    logger,
    runner,
) -> None:
    ground_truth_dir = work_root / "ground_truth_repo"
    _copy_pristine_tree(pristine_dir, ground_truth_dir)
    status["trigger_test_patch_applied"] = _apply_benchmark_patch(
        worktree=ground_truth_dir,
        patch_text=record.trigger_test_patch,
        runner=runner,
        logger=logger,
        label="trigger_test_patch",
    )
    status["regression_test_patch_applied"] = _apply_benchmark_patch(
        worktree=ground_truth_dir,
        patch_text=record.regression_test_patch,
        runner=runner,
        logger=logger,
        label="regression_test_patch",
    )

    if record.has_ground_truth_patch:
        status["ground_truth_patch_applied"] = _apply_patch_text(
            ground_truth_dir,
            record.patch,
            runner=runner,
            logger=logger,
            label="ground_truth_patch",
        )
    else:
        status["ground_truth_patch_applied"] = False
        logger.log("ground_truth_patch_applied=False because JSONL `patch` is missing or empty.")

    should_run_ground_truth_tests = (
        status["trigger_test_patch_applied"] is not False
        and status["regression_test_patch_applied"] is not False
        and status["ground_truth_patch_applied"] is True
    )
    if should_run_ground_truth_tests:
        status["ground_truth_trigger_test_passed"] = _run_benchmark_suite(
            image_tag=image_tag,
            worktree=ground_truth_dir,
            commands=record.fail_to_pass,
            patch_present=bool(record.trigger_test_patch.strip()),
            patch_applied=status["trigger_test_patch_applied"],
            label_prefix=f"ground_truth_trigger_{record.instance_id}",
            stage_dir=work_root / "ground_truth_trigger_runs",
            logger=logger,
            runner=runner,
        )
        status["ground_truth_regression_test_passed"] = _run_benchmark_suite(
            image_tag=image_tag,
            worktree=ground_truth_dir,
            commands=record.pass_to_pass,
            patch_present=bool(record.regression_test_patch.strip()),
            patch_applied=status["regression_test_patch_applied"],
            label_prefix=f"ground_truth_regression_{record.instance_id}",
            stage_dir=work_root / "ground_truth_regression_runs",
            logger=logger,
            runner=runner,
        )
    else:
        logger.log("ground_truth_tests_skipped=True because a prerequisite patch/apply step failed.")

    status["eval_infrastructure_valid"] = (
        status["eval_workspace_prepared"] is True
        and status["ground_truth_patch_applied"] is True
        and status["ground_truth_trigger_test_passed"] is True
        and status["ground_truth_regression_test_passed"] is True
    )
    logger.log(f"eval_infrastructure_valid={status['eval_infrastructure_valid']}")


def _evaluate_agent_from_pristine(
    *,
    record: BenchmarkRecord,
    image_tag: str,
    pristine_dir: Path,
    agent_patch_path: Path,
    work_root: Path,
    status: dict,
    logger,
    runner,
) -> None:
    patch_text = agent_patch_path.read_text(encoding="utf-8") if agent_patch_path.exists() else ""
    agent_dir = work_root / "agent_repo"
    _copy_pristine_tree(pristine_dir, agent_dir)
    status["trigger_test_patch_applied"] = _apply_benchmark_patch(
        worktree=agent_dir,
        patch_text=record.trigger_test_patch,
        runner=runner,
        logger=logger,
        label="agent_trigger_test_patch",
    )
    status["regression_test_patch_applied"] = _apply_benchmark_patch(
        worktree=agent_dir,
        patch_text=record.regression_test_patch,
        runner=runner,
        logger=logger,
        label="agent_regression_test_patch",
    )

    if patch_text.strip():
        status["agent_patch_applied"] = _apply_patch_text(
            agent_dir,
            patch_text,
            runner=runner,
            logger=logger,
            label="agent_patch",
        )
    else:
        status["agent_patch_applied"] = False
        logger.log("agent_patch_applied=False because patch file is empty or missing.")

    if status["agent_patch_applied"] is not True:
        status["agent_patch_passed"] = False
        logger.log("agent_patch_passed=False because agent_patch_applied is not True.")
        return

    status["trigger_test_passed"] = _run_benchmark_suite(
        image_tag=image_tag,
        worktree=agent_dir,
        commands=record.fail_to_pass,
        patch_present=bool(record.trigger_test_patch.strip()),
        patch_applied=status["trigger_test_patch_applied"],
        label_prefix=f"agent_trigger_{record.instance_id}",
        stage_dir=work_root / "agent_trigger_runs",
        logger=logger,
        runner=runner,
    )

    status["regression_test_passed"] = _run_benchmark_suite(
        image_tag=image_tag,
        worktree=agent_dir,
        commands=record.pass_to_pass,
        patch_present=bool(record.regression_test_patch.strip()),
        patch_applied=status["regression_test_patch_applied"],
        label_prefix=f"agent_regression_{record.instance_id}",
        stage_dir=work_root / "agent_regression_runs",
        logger=logger,
        runner=runner,
    )

    status["agent_patch_passed"] = (
        status["trigger_test_passed"] is True and status["regression_test_passed"] is True
    )
    logger.log(f"agent_patch_passed={status['agent_patch_passed']}")


def _copy_pristine_tree(pristine_dir: Path, destination: Path) -> None:
    _reset_dir(destination)
    copy_prepared_tree(pristine_dir, destination)


def _apply_benchmark_patch(*, worktree: Path, patch_text: str, runner, logger, label: str) -> bool | None:
    if not patch_text.strip():
        logger.log(f"{label}=null because JSONL patch is null.")
        return None
    return _apply_patch_text(worktree, patch_text, runner=runner, logger=logger, label=label)


def _run_benchmark_suite(
    *,
    image_tag: str,
    worktree: Path,
    commands: list[str],
    patch_present: bool,
    patch_applied: bool | None,
    label_prefix: str,
    stage_dir: Path,
    logger,
    runner,
) -> bool:
    if not commands:
        logger.log(f"{label_prefix}_test_passed=False because the benchmark command list is empty.")
        return False
    if patch_present and patch_applied is False:
        logger.log(f"{label_prefix}_test_passed=False because required benchmark patch failed to apply.")
        return False

    outcomes = []
    use_zulip_runtime = _is_zulip_worktree(worktree)
    for index, raw_command in enumerate(commands, start=1):
        resolved = normalize_test_command(
            resolve_test_command(worktree, raw_command),
            use_zulip_runtime=use_zulip_runtime,
        )
        try:
            outcome = _run_in_eval_container(
                image_tag=image_tag,
                worktree=worktree,
                command=resolved,
                stage_dir=stage_dir,
                label=f"{label_prefix}_{index}",
                runner=runner,
            )
        except Exception as exc:  # noqa: BLE001
            logger.log(f"{label_prefix}_command index={index} passed=False error={exc}")
            return False
        logger.log(
            f"{label_prefix}_command index={index} returncode={outcome.returncode} passed={outcome.passed} command={resolved}"
        )
        if not outcome.passed:
            logger.log(
                f"{label_prefix}_command_failed index={index} "
                f"stdout={outcome.stdout_excerpt} stderr={outcome.stderr_excerpt}"
            )
        outcomes.append(outcome)
    return all(item.passed for item in outcomes)


def _apply_patch_text(worktree: Path, patch_text: str, *, runner, logger, label: str) -> bool:
    import tempfile

    temp_dir = Path(tempfile.mkdtemp(prefix=f"eval-{label}-"))
    patch_path = temp_dir / "input.diff"
    patch_path.write_text(patch_text, encoding="utf-8")
    try:
        result = runner(
            ["git", "apply", "--binary", "--ignore-whitespace", str(patch_path)],
            cwd=worktree,
            check=False,
            timeout_seconds=300,
        )
        if result.returncode != 0:
            logger.log(f"{label}=False stdout={_tail_text(result.stdout)} stderr={_tail_text(result.stderr)}")
            return False
        logger.log(f"{label}=True")
        return True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def normalize_test_command(
    command: str,
    *,
    repo_root: str = "/workspace/eval_repo",
    use_zulip_runtime: bool = False,
) -> str:
    repo_root_q = shlex.quote(repo_root)
    inner_command = extract_inner_command(command)
    stable_network_env = (
        "export MAVEN_OPTS=\"${MAVEN_OPTS:-} "
        "-Dmaven.wagon.http.retryHandler.count=6 "
        "-Dmaven.wagon.http.retryHandler.requestSentEnabled=true "
        "-Dhttps.protocols=TLSv1.2\" && "
    )
    if use_zulip_runtime:
        inner = (
            f"cd {repo_root_q} && "
            "export PATH=/srv/zulip-py3-venv/bin:$PATH && "
            "export LANG=C.UTF-8 LC_ALL=C.UTF-8 && "
            f"{stable_network_env}"
            ". /srv/zulip-py3-venv/bin/activate && "
            f"{inner_command}"
        )
        return f"su zulip -c {shlex.quote(inner)}"
    return f"cd {repo_root_q} && export LANG=C.UTF-8 LC_ALL=C.UTF-8 && {stable_network_env}{inner_command}"


def extract_inner_command(command: str) -> str:
    text = command.strip()
    text = _rewrite_windows_python_command(text)
    docker_inner = _extract_docker_exec_shell_command(text)
    if docker_inner is not None:
        text = docker_inner
    backend_match = re.search(r"((?:python(?:[0-9.]*)\s+)?(?:\./)?tools/test-backend\b.*)", text)
    if backend_match is not None:
        backend_text = backend_match.group(1).strip()
        backend_text = re.sub(r"^python(?:[0-9.]*)\s+", "", backend_text)
        if "su zulip -c '" in text and backend_text.endswith("'"):
            backend_text = backend_text[:-1]
        if backend_text.startswith("tools/test-backend"):
            return f"./{backend_text}"
        return backend_text

    script_match = re.search(r"((?:python(?:[0-9.]*)\s+)?(?:\./)?tools/tests/[^\s]+\.py(?:\s.*)?)", text)
    if script_match is not None:
        script_text = script_match.group(1).strip()
        if "su zulip -c '" in text and script_text.endswith("'"):
            script_text = script_text[:-1]
        if script_text.startswith("python"):
            return script_text
        return f"python {script_text.removeprefix('./')}"

    for prefix in ("cd /workspace/repo && ", "cd /workspace/eval_repo && "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return _strip_known_repo_cd(text)


def _extract_docker_exec_shell_command(command: str) -> str | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(tokens) < 6 or tokens[0] != "docker" or tokens[1] != "exec":
        return None
    for index, token in enumerate(tokens):
        if token in {"-lc", "-c"} and index + 1 < len(tokens):
            return _strip_known_repo_cd(tokens[index + 1])
    return None


def _strip_known_repo_cd(command: str) -> str:
    text = command.strip()
    for pattern in (
        r"^cd\s+/workspace/repo\s*&&\s*",
        r"^cd\s+/workspace/eval_repo\s*&&\s*",
        r"^cd\s+/tmp/full_patch_clean[0-9A-Za-z_-]*\s*&&\s*",
        r"^cd\s+/tmp/runtime\s*&&\s*",
    ):
        text = re.sub(pattern, "", text)
    return text


def _rewrite_windows_python_command(command: str) -> str:
    match = re.match(
        r"""^(?:"[A-Za-z]:[\\/][^"]*python(?:\.exe)?"|'[A-Za-z]:[\\/][^']*python(?:\.exe)?'|[A-Za-z]:[\\/]\S*python(?:\.exe)?)\s+(.*)$""",
        command,
        flags=re.IGNORECASE,
    )
    if match is None:
        return command
    return f"python {match.group(1).strip()}"


def _is_zulip_worktree(worktree: Path) -> bool:
    return (worktree / "zproject").is_dir() and (worktree / "tools" / "test-backend").exists()


def resolve_test_command(worktree: Path, command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return command
    if not tokens:
        return command

    target_index = None
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token.startswith("-"):
            continue
        if "tools/test-backend" in token:
            break
        if "." in token:
            target_index = index
            break
    if target_index is None:
        return command

    target = tokens[target_index]
    corrected = resolve_test_target(worktree, target)
    if corrected is None or corrected == target:
        return command
    tokens[target_index] = corrected
    return " ".join(shlex.quote(token) for token in tokens)


def resolve_test_target(worktree: Path, target: str) -> str | None:
    parts = target.split(".")
    if len(parts) < 3:
        return None
    method_name = parts[-1]
    needle = f"def {method_name}("
    matches: list[tuple[Path, int]] = []
    for path in (worktree / "zerver" / "tests").rglob("*.py"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(lines):
            if needle in line:
                matches.append((path, index))
    if len(matches) != 1:
        return None

    path, line_index = matches[0]
    lines = path.read_text(encoding="utf-8").splitlines()
    class_name = None
    for index in range(line_index, -1, -1):
        stripped = lines[index].lstrip()
        if stripped.startswith("class "):
            remainder = stripped[len("class ") :]
            class_name = remainder.split("(", 1)[0].split(":", 1)[0].strip()
            break
    if not class_name:
        return None
    module = ".".join(path.relative_to(worktree).with_suffix("").parts)
    return f"{module}.{class_name}.{method_name}"


def _run_in_eval_container(
    *,
    image_tag: str,
    worktree: Path,
    command: str,
    stage_dir: Path,
    label: str,
    runner,
) -> CommandOutcome:
    stage_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).replace("_", "-").strip("-")
    container_name = f"eval-{os.getpid()}-{safe_label}"[:120]
    runner(["docker", "rm", "-f", container_name], cwd=None, check=False, timeout_seconds=60)
    maven_seed_cache = _maven_seed_cache_path()
    maven_cache = _prepare_isolated_maven_cache(stage_dir=stage_dir, seed_cache=maven_seed_cache)
    create = runner(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-v",
            f"{maven_cache.resolve()}:/root/.m2",
            "--entrypoint",
            "bash",
            image_tag,
            "-lc",
            "tail -f /dev/null",
        ],
        cwd=None,
        check=False,
        timeout_seconds=300,
    )
    if create.returncode != 0:
        raise RuntimeError(f"Failed to create eval container `{container_name}`\n{create.stderr}")
    try:
        repo_root = "/workspace/eval_repo"
        is_zulip_repo = _is_zulip_worktree(worktree)
        has_zulip_user = _container_has_user(container_name=container_name, username="zulip", runner=runner)
        clear = runner(
            ["docker", "exec", container_name, "bash", "-lc", f"rm -rf {repo_root} && mkdir -p {repo_root}"],
            cwd=None,
            check=False,
            timeout_seconds=120,
        )
        if clear.returncode != 0:
            raise RuntimeError(f"Failed to prepare repo dir in `{container_name}`\n{clear.stderr}")
        copy = runner(
            ["docker", "cp", str(worktree.resolve()) + os.sep + ".", f"{container_name}:{repo_root}"],
            cwd=None,
            check=False,
            timeout_seconds=1800,
        )
        if copy.returncode != 0:
            raise RuntimeError(f"Failed to copy repo into `{container_name}`\n{copy.stderr}")
        _normalize_gradle_wrappers(container_name=container_name, repo_root=repo_root, runner=runner)
        if is_zulip_repo and has_zulip_user:
            fix_owner = runner(
                ["docker", "exec", container_name, "bash", "-lc", f"chown -R zulip:zulip {repo_root}"],
                cwd=None,
                check=False,
                timeout_seconds=120,
            )
            if fix_owner.returncode != 0:
                raise RuntimeError(f"Failed to chown repo in `{container_name}`\n{fix_owner.stderr}")

        if is_zulip_repo:
            _bootstrap_zulip_test_settings(
                container_name=container_name,
                repo_root=repo_root,
                has_zulip_user=has_zulip_user,
                runner=runner,
            )
            bootstrap = runner(
                ["docker", "exec", container_name, "bash", "-lc", _service_bootstrap_script()],
                cwd=None,
                check=False,
                timeout_seconds=180,
            )
            if bootstrap.returncode != 0:
                raise RuntimeError(f"Failed to bootstrap services in `{container_name}`\n{bootstrap.stderr}")
            _sync_zulip_postgres_roles(
                container_name=container_name,
                repo_root=repo_root,
                runner=runner,
            )
            _configure_zulip_rabbitmq(
                container_name=container_name,
                repo_root=repo_root,
                runner=runner,
            )
            if "tools/test-backend" in command:
                _prepare_zulip_test_database(
                    container_name=container_name,
                    repo_root=repo_root,
                    runner=runner,
                )

        effective_command = _adapt_command_for_container_user(command, has_zulip_user=has_zulip_user)
        outcome = runner(["docker", "exec", container_name, "bash", "-lc", effective_command], cwd=None, check=False, timeout_seconds=1800)
        return CommandOutcome(
            command=effective_command,
            returncode=outcome.returncode,
            passed=outcome.returncode == 0,
            stdout_excerpt=_tail_text(outcome.stdout),
            stderr_excerpt=_tail_text(outcome.stderr),
        )
    finally:
        runner(["docker", "rm", "-f", container_name], cwd=None, check=False, timeout_seconds=120)
        _merge_isolated_maven_cache(source_cache=maven_cache, seed_cache=maven_seed_cache)


def _maven_seed_cache_path() -> Path:
    return Path(
        os.environ.get(
            "SDB_MAVEN_CACHE",
            str(Path(__file__).resolve().parents[2] / "eval_infra_cache" / "maven_cache"),
        )
    )


def _prepare_isolated_maven_cache(*, stage_dir: Path, seed_cache: Path) -> Path:
    private_cache = stage_dir / "maven_cache"
    seed_cache.mkdir(parents=True, exist_ok=True)
    lock_dir = _acquire_maven_cache_lock(seed_cache)
    try:
        if seed_cache.exists():
            shutil.copytree(
                seed_cache,
                private_cache,
                dirs_exist_ok=True,
                ignore=_ignore_maven_cache_entries,
            )
        else:
            private_cache.mkdir(parents=True, exist_ok=True)
    finally:
        _release_maven_cache_lock(lock_dir)
    _remove_maven_failure_markers(private_cache)
    return private_cache


def _merge_isolated_maven_cache(*, source_cache: Path, seed_cache: Path) -> None:
    if not source_cache.exists():
        return
    lock_dir = _acquire_maven_cache_lock(seed_cache)
    try:
        seed_cache.mkdir(parents=True, exist_ok=True)
        for source in source_cache.rglob("*"):
            if not source.is_file() or _is_ignored_maven_cache_file(source):
                continue
            try:
                if source.stat().st_size == 0:
                    continue
            except OSError:
                continue
            target = seed_cache / source.relative_to(source_cache)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, target)
            except OSError:
                continue
    finally:
        _release_maven_cache_lock(lock_dir)


def _ignore_maven_cache_entries(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _is_ignored_maven_cache_name(name)}


def _remove_maven_failure_markers(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_ignored_maven_cache_file(path):
            try:
                path.unlink()
            except OSError:
                pass


def _is_ignored_maven_cache_file(path: Path) -> bool:
    name = path.name
    if _is_ignored_maven_cache_name(name):
        return True
    try:
        return path.stat().st_size == 0 and path.suffix.lower() in {".jar", ".pom", ".sha1", ".md5"}
    except OSError:
        return True


def _is_ignored_maven_cache_name(name: str) -> bool:
    lower = name.lower()
    return lower.endswith((".lastupdated", ".part", ".tmp", ".lock", ".lck"))


def _acquire_maven_cache_lock(seed_cache: Path) -> Path:
    lock_dir = seed_cache.with_name(f"{seed_cache.name}.lock")
    started = time.monotonic()
    while True:
        try:
            lock_dir.mkdir(parents=True)
        except FileExistsError:
            if _maven_cache_lock_is_stale(lock_dir):
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            if time.monotonic() - started > 600:
                raise TimeoutError(f"Timed out waiting for Maven cache lock: {lock_dir}")
            time.sleep(0.25)
            continue
        try:
            (lock_dir / "owner.txt").write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        return lock_dir


def _release_maven_cache_lock(lock_dir: Path) -> None:
    shutil.rmtree(lock_dir, ignore_errors=True)


def _maven_cache_lock_is_stale(lock_dir: Path) -> bool:
    try:
        return time.time() - lock_dir.stat().st_mtime > 3600
    except OSError:
        return False


def _normalize_gradle_wrappers(*, container_name: str, repo_root: str, runner) -> None:
    repo_root_q = shlex.quote(repo_root)
    script = (
        f"find {repo_root_q} -name gradlew -type f "
        "-exec sed -i 's/\r$//' {} + "
        "-exec chmod +x {} +"
    )
    result = runner(
        ["docker", "exec", container_name, "bash", "-lc", script],
        cwd=None,
        check=False,
        timeout_seconds=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to normalize Gradle wrappers in `{container_name}`\n{result.stderr}")


def _bootstrap_zulip_test_settings(*, container_name: str, repo_root: str, has_zulip_user: bool, runner) -> None:
    repo_root_q = shlex.quote(repo_root)
    base_env = (
        f"cd {repo_root_q} && "
        "export PATH=/srv/zulip-py3-venv/bin:$PATH && "
        "export LANG=C.UTF-8 LC_ALL=C.UTF-8 && "
        ". /srv/zulip-py3-venv/bin/activate && "
    )
    dev_inner = base_env + "python scripts/setup/generate_secrets.py --development"
    dev_command = f"su zulip -c {shlex.quote(dev_inner)}" if has_zulip_user else dev_inner
    prod_command = base_env + "python scripts/setup/generate_secrets.py --production"
    bootstrap_script = textwrap.dedent(
        f"""
        set -euxo pipefail
        if grep -q '^deploy_type' /etc/zulip/zulip.conf 2>/dev/null; then
          if [ ! -f /etc/zulip/zulip-secrets.conf ]; then
            {prod_command}
          fi
        else
          if [ ! -f {repo_root_q}/zproject/dev-secrets.conf ]; then
            {dev_command}
          fi
          if [ ! -d {repo_root_q}/var/log ]; then
            mkdir -p {repo_root_q}/var/log {repo_root_q}/var/uploads {repo_root_q}/var/test_uploads {repo_root_q}/var/analytics-lock-dir
          fi
          if id -u zulip >/dev/null 2>&1; then
            chown -R zulip:zulip {repo_root_q}/var
          fi
        fi
        """
    ).strip()
    bootstrap = runner(
        ["docker", "exec", container_name, "bash", "-lc", bootstrap_script],
        cwd=None,
        check=False,
        timeout_seconds=300,
    )
    if bootstrap.returncode != 0:
        raise RuntimeError(
            f"Failed to bootstrap Zulip secrets in `{container_name}`\n"
            f"stdout:\n{bootstrap.stdout}\nstderr:\n{bootstrap.stderr}"
        )


def _service_bootstrap_script() -> str:
    return textwrap.dedent(
        """
        set -euxo pipefail
        if command -v service >/dev/null 2>&1; then
          service postgresql start || service postgresql@13-main start || true
          service redis-server start || true
          service memcached start || true
          service rabbitmq-server start || true
        fi
        if command -v pg_isready >/dev/null 2>&1; then
          for attempt in $(seq 1 60); do
            if pg_isready >/dev/null 2>&1; then
              exit 0
            fi
            sleep 1
          done
          pg_isready
        fi
        """
    ).strip()


def _sync_zulip_postgres_roles(*, container_name: str, repo_root: str, runner) -> None:
    repo_root_q = shlex.quote(repo_root)
    script = textwrap.dedent(
        f"""
        set -eo pipefail
        cd {repo_root_q}
        if [ ! -f zproject/dev-secrets.conf ]; then
          exit 0
        fi
        password="$(
          python3 - <<'PY'
import configparser
config = configparser.RawConfigParser()
config.read("zproject/dev-secrets.conf")
print(config.get("secrets", "local_database_password"))
PY
        )"
        escaped_password="$(printf "%s" "$password" | sed "s/'/''/g")"
        sudo -u postgres psql -v ON_ERROR_STOP=1 postgres <<SQL
DO \\$\\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'zulip') THEN
    CREATE ROLE zulip LOGIN CREATEDB PASSWORD '$escaped_password';
  ELSE
    ALTER ROLE zulip WITH LOGIN CREATEDB PASSWORD '$escaped_password';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'zulip_test') THEN
    CREATE ROLE zulip_test LOGIN CREATEDB PASSWORD '$escaped_password';
  ELSE
    ALTER ROLE zulip_test WITH LOGIN CREATEDB PASSWORD '$escaped_password';
  END IF;
END
\\$\\$;
SQL
        """
    ).strip()
    result = runner(
        ["docker", "exec", container_name, "bash", "-lc", script],
        cwd=None,
        check=False,
        timeout_seconds=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to sync Zulip postgres roles in `{container_name}`\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _configure_zulip_rabbitmq(*, container_name: str, repo_root: str, runner) -> None:
    repo_root_q = shlex.quote(repo_root)
    script = textwrap.dedent(
        f"""
        set -eo pipefail
        cd {repo_root_q}
        if [ ! -x scripts/setup/configure-rabbitmq ] || [ ! -f zproject/dev-secrets.conf ]; then
          exit 0
        fi
        export PATH=/srv/zulip-py3-venv/bin:$PATH
        . /srv/zulip-py3-venv/bin/activate
        ./scripts/setup/configure-rabbitmq
        """
    ).strip()
    result = runner(
        ["docker", "exec", container_name, "bash", "-lc", script],
        cwd=None,
        check=False,
        timeout_seconds=180,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to configure Zulip rabbitmq in `{container_name}`\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _prepare_zulip_test_database(*, container_name: str, repo_root: str, runner) -> None:
    repo_root_q = shlex.quote(repo_root)
    script = textwrap.dedent(
        f"""
        set -eo pipefail
        cd {repo_root_q}
        if [ ! -x tools/setup/postgres-init-test-db ] || [ ! -x scripts/setup/terminate-psql-sessions ]; then
          exit 0
        fi
        cat >/tmp/eval-prepare-zulip-test-db.sh <<'EOS'
set -e
cd {repo_root}
export PATH=/srv/zulip-py3-venv/bin:$PATH
export LANG=C.UTF-8 LC_ALL=C.UTF-8
. /srv/zulip-py3-venv/bin/activate
./tools/setup/postgres-init-dev-db
./tools/setup/postgres-init-test-db
mkdir -p zerver/fixtures
scripts/setup/terminate-psql-sessions zulip zulip_test zulip_test_base zulip_test_template
psql -h localhost postgres zulip_test <<SQL
DROP DATABASE IF EXISTS zulip_test;
CREATE DATABASE zulip_test TEMPLATE zulip_test_base;
SQL
if head -n 1 scripts/setup/flush-memcached | grep -q 'python'; then
  python scripts/setup/flush-memcached
else
  sh scripts/setup/flush-memcached
fi
./manage.py migrate --noinput --settings=zproject.test_settings
if grep -R "third_party_api_results" -n zproject scripts tools >/dev/null 2>&1; then
  ./manage.py createcachetable third_party_api_results --settings=zproject.test_settings || true
fi
./manage.py get_migration_status --settings=zproject.test_settings --output="migration_status_test"
./manage.py populate_db --settings=zproject.test_settings --test-suite -n30 --threads=1 --huddles=0 --personals=0 --percent-huddles=0 --percent-personals=0
./manage.py dumpdata --settings=zproject.test_settings zerver.UserProfile zerver.Stream zerver.Recipient zerver.Subscription zerver.Message zerver.Huddle zerver.Realm zerver.UserMessage zerver.Client zerver.DefaultStream > zerver/fixtures/messages.json
psql -h localhost postgres zulip_test <<SQL
DROP DATABASE IF EXISTS zulip_test_template;
CREATE DATABASE zulip_test_template TEMPLATE zulip_test;
SQL
EOS
        chown zulip:zulip /tmp/eval-prepare-zulip-test-db.sh
        su zulip -c 'bash /tmp/eval-prepare-zulip-test-db.sh'
        """
    ).strip()
    result = runner(
        ["docker", "exec", container_name, "bash", "-lc", script],
        cwd=None,
        check=False,
        timeout_seconds=1200,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to prepare Zulip test database in `{container_name}`\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _container_has_user(*, container_name: str, username: str, runner) -> bool:
    probe = runner(
        ["docker", "exec", container_name, "bash", "-lc", f"id -u {shlex.quote(username)} >/dev/null 2>&1"],
        cwd=None,
        check=False,
        timeout_seconds=60,
    )
    return probe.returncode == 0


def _adapt_command_for_container_user(command: str, *, has_zulip_user: bool) -> str:
    if has_zulip_user:
        return command
    prefix = "su zulip -c "
    if not command.startswith(prefix):
        return command
    inner = command[len(prefix) :].strip()
    try:
        parts = shlex.split(inner, posix=True)
    except ValueError:
        return command
    if len(parts) != 1:
        return command
    return parts[0]


def _reset_dir(path: Path) -> None:
    if path.exists():
        rmtree_force(path)
    path.mkdir(parents=True, exist_ok=True)


def _tail_text(text: str, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    lines = text.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail
