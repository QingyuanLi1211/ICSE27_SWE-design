"""Shared helpers for preparing repair worktrees and extracting patches."""

from __future__ import annotations

import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

from .benchmark import BenchmarkRecord, require_docker_image
from .diffing import filter_agent_patch_text_with_report, sanitize_worktree_for_diff, write_agent_patch
from .docker_bundle import ensure_image_loaded
from .worktree import detect_repo_root, prepare_repair_worktree, workspace_is_writable


@dataclass(slots=True)
class DiffOutcome:
    patch_text: str
    worktree_changed: bool
    patch_generated: bool
    error: str | None = None


@dataclass(slots=True)
class DockerRepairWorkspace:
    container_name: str
    candidate_dir: str = "/workspace/eval_repo"


def build_worktree_for_record(
    *,
    record: BenchmarkRecord,
    bundle_root: Path,
    work_root: Path,
    repair_status: dict,
    logger,
    runner,
) -> tuple[Path | None, Path | None]:
    image_tag = require_docker_image(record)
    try:
        ensure_image_loaded(bundle_root, image_tag, runner=runner)
        repair_status["image_available"] = True
        logger.log(f"image_available=True repo_tag={image_tag}")
    except Exception as exc:  # noqa: BLE001
        repair_status["image_available"] = False
        logger.log(f"image_available=False error={exc}")
        return None, None

    try:
        pristine_dir, candidate_dir = prepare_repair_worktree(
            image_tag=image_tag,
            base_commit=record.base_commit,
            repo_slug=record.repo,
            work_root=work_root,
            runner=runner,
        )
        repair_status["workspace_prepared"] = True
        logger.log(f"workspace_prepared=True root={work_root}")
    except Exception as exc:  # noqa: BLE001
        repair_status["workspace_prepared"] = False
        logger.log(f"workspace_prepared=False error={exc}")
        return None, None

    repair_status["workspace_writable"] = workspace_is_writable(candidate_dir)
    logger.log(f"workspace_writable={repair_status['workspace_writable']}")
    return pristine_dir, candidate_dir


def build_docker_workspace_for_record(
    *,
    record: BenchmarkRecord,
    bundle_root: Path,
    repair_status: dict,
    logger,
    runner,
) -> DockerRepairWorkspace | None:
    image_tag = require_docker_image(record)
    try:
        ensure_image_loaded(bundle_root, image_tag, runner=runner)
        repair_status["image_available"] = True
        logger.log(f"image_available=True repo_tag={image_tag}")
    except Exception as exc:  # noqa: BLE001
        repair_status["image_available"] = False
        logger.log(f"image_available=False error={exc}")
        return None

    container_name = _docker_repair_container_name(record.instance_id)
    runner(["docker", "rm", "-f", container_name], cwd=None, check=False, timeout_seconds=120)
    started = runner(
        ["docker", "run", "-d", "--name", container_name, "--entrypoint", "bash", image_tag, "-lc", "sleep 86400"],
        cwd=None,
        check=False,
        timeout_seconds=300,
    )
    if started.returncode != 0:
        repair_status["workspace_prepared"] = False
        logger.log(f"docker_workspace_started=False error={started.stderr or started.stdout}")
        return None

    workspace = DockerRepairWorkspace(container_name=container_name)
    try:
        repo_root = detect_repo_root(container_name=container_name, runner=runner)
        logger.log(f"docker_repo_root={repo_root}")
        script = f"""
set -eu
candidate={shlex.quote(workspace.candidate_dir)}
rm -rf "$candidate"
mkdir -p "$candidate"
cp -a {shlex.quote(repo_root)}/. "$candidate"/
find "$candidate" -name .git -prune -exec rm -rf {{}} +
git config --global --add safe.directory "$candidate" || true
git -C "$candidate" init -q
git -C "$candidate" config --local user.email bench@example.com
git -C "$candidate" config --local user.name bench
git -C "$candidate" config --local core.autocrlf false
git -C "$candidate" config --local core.fileMode false
git -C "$candidate" add -A -- .
git -C "$candidate" commit -q -m base
test -d "$candidate"
touch "$candidate"/.write_probe
rm -f "$candidate"/.write_probe
"""
        prepared = runner(
            ["docker", "exec", container_name, "bash", "-lc", script],
            cwd=None,
            check=False,
            timeout_seconds=600,
        )
        if prepared.returncode != 0:
            repair_status["workspace_prepared"] = False
            logger.log(f"docker_workspace_prepared=False stdout={prepared.stdout} stderr={prepared.stderr}")
            return workspace
        repair_status["workspace_prepared"] = True
        repair_status["workspace_writable"] = True
        logger.log(
            "docker_workspace_prepared=True "
            f"container={container_name} candidate={workspace.candidate_dir}"
        )
        return workspace
    except Exception as exc:  # noqa: BLE001
        repair_status["workspace_prepared"] = False
        logger.log(f"docker_workspace_prepared=False error={exc}")
        return workspace


def diff_docker_agent_patch_for_record(
    *,
    record: BenchmarkRecord,
    workspace: DockerRepairWorkspace | None,
    patch_path: Path,
    logger,
    runner,
) -> DiffOutcome:
    if workspace is None:
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text("", encoding="utf-8")
        logger.log("diff_agent_patch wrote empty diff because docker workspace was not prepared.")
        return DiffOutcome(patch_text="", worktree_changed=False, patch_generated=False)

    script = f"""
set -u
candidate={shlex.quote(workspace.candidate_dir)}
git config --global --add safe.directory "$candidate" || true
cd "$candidate"
for root in .; do
  find "$root" \\( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache -o -name .hypothesis -o -name .tox -o -name .nox -o -name output_data -o -name output_data_batch \\) -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
  find "$root" \\( -name '*.pyc' -o -name '*.pyo' -o -name '*.tmp' -o -name '*.temp' \\) -type f -delete 2>/dev/null || true
done
git -C "$candidate" add -A -- .
git -C "$candidate" -c core.fileMode=false diff --cached --binary --ignore-cr-at-eol --no-ext-diff HEAD > /tmp/agent.patch 2>/tmp/agent.patch.err
rc=$?
if [ "$rc" -eq 129 ]; then
  # Older project images can have Git versions that predate --ignore-cr-at-eol.
  # Fall back to a plain binary diff rather than losing a valid agent edit.
  git -C "$candidate" -c core.fileMode=false diff --cached --binary --no-ext-diff HEAD > /tmp/agent.patch 2>/tmp/agent.patch.err
  rc=$?
fi
if [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]; then
  exit 0
fi
cat /tmp/agent.patch.err >&2 || true
exit "$rc"
"""
    diffed = runner(
        ["docker", "exec", workspace.container_name, "bash", "-lc", script],
        cwd=None,
        check=False,
        timeout_seconds=600,
    )
    if diffed.returncode != 0:
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text("", encoding="utf-8")
        logger.log(f"diff_agent_patch failed docker_diff_returncode={diffed.returncode} stderr={diffed.stderr}")
        return DiffOutcome(patch_text="", worktree_changed=False, patch_generated=False, error=diffed.stderr)

    patch_path.parent.mkdir(parents=True, exist_ok=True)
    copied = runner(
        ["docker", "cp", f"{workspace.container_name}:/tmp/agent.patch", str(patch_path)],
        cwd=None,
        check=False,
        timeout_seconds=120,
    )
    if copied.returncode != 0:
        patch_path.write_text("", encoding="utf-8")
        logger.log(f"diff_agent_patch failed docker_cp_returncode={copied.returncode} stderr={copied.stderr}")
        return DiffOutcome(patch_text="", worktree_changed=False, patch_generated=False, error=copied.stderr)

    raw_patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    patch_text, dropped_paths = filter_agent_patch_text_with_report(raw_patch_text, repo_key=record.repo_key)
    patch_path.write_text(patch_text, encoding="utf-8")
    if patch_text != raw_patch_text:
        logger.log(
            "diff_agent_patch filtered "
            f"raw_patch_bytes={len(raw_patch_text.encode('utf-8'))} "
            f"filtered_patch_bytes={len(patch_text.encode('utf-8'))}"
        )
        if dropped_paths:
            preview = dropped_paths[:20]
            suffix = "" if len(dropped_paths) <= 20 else f" ... total={len(dropped_paths)}"
            logger.log(f"diff_agent_patch filtered_dropped_paths={preview}{suffix}")
    worktree_changed = bool(patch_text)
    patch_generated = bool(patch_text.encode("utf-8"))
    logger.log(
        "diff_agent_patch completed "
        f"patch_bytes={len(patch_text.encode('utf-8'))} "
        f"worktree_changed={worktree_changed}"
    )
    return DiffOutcome(patch_text=patch_text, worktree_changed=worktree_changed, patch_generated=patch_generated)


def cleanup_docker_workspace(workspace: DockerRepairWorkspace | None, *, runner) -> None:
    if workspace is not None:
        runner(["docker", "rm", "-f", workspace.container_name], cwd=None, check=False, timeout_seconds=120)


def diff_agent_patch_for_record(
    *,
    pristine_dir: Path | None,
    candidate_dir: Path | None,
    patch_path: Path,
    logger,
    runner,
    record: BenchmarkRecord | None = None,
) -> DiffOutcome:
    if pristine_dir is None or candidate_dir is None:
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text("", encoding="utf-8")
        logger.log("diff_agent_patch wrote empty diff because worktree was not prepared.")
        return DiffOutcome(patch_text="", worktree_changed=False, patch_generated=False)

    sanitize_worktree_for_diff(pristine_dir)
    sanitize_worktree_for_diff(candidate_dir)

    try:
        patch_text = write_agent_patch(
            pristine_dir=pristine_dir,
            candidate_dir=candidate_dir,
            output_path=patch_path,
            runner=runner,
            repo_key=record.repo_key if record is not None else None,
        )
    except Exception as exc:  # noqa: BLE001
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text("", encoding="utf-8")
        logger.log(f"diff_agent_patch failed error={exc}")
        return DiffOutcome(patch_text="", worktree_changed=False, patch_generated=False, error=str(exc))

    worktree_changed = bool(patch_text)
    patch_generated = bool(patch_text.encode("utf-8"))
    logger.log(
        "diff_agent_patch completed "
        f"patch_bytes={len(patch_text.encode('utf-8'))} "
        f"worktree_changed={worktree_changed}"
    )
    if worktree_changed and not patch_generated:
        logger.log("diff_agent_patch anomaly worktree_changed=True patch_bytes=0")
    return DiffOutcome(
        patch_text=patch_text,
        worktree_changed=worktree_changed,
        patch_generated=patch_generated,
    )


def _docker_repair_container_name(instance_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", instance_id).strip("-").lower()
    return f"repair-{safe}-{uuid.uuid4().hex[:8]}"


def _normalize_docker_no_index_diff(text: str, *, pristine_dir: str, candidate_dir: str) -> str:
    normalized = text
    pristine = pristine_dir.strip("/")
    candidate = candidate_dir.strip("/")
    replacements = {
        f"a/{pristine}/": "a/",
        f"b/{candidate}/": "b/",
        f"--- {pristine_dir}/": "--- a/",
        f"+++ {candidate_dir}/": "+++ b/",
        f"--- a/{pristine}/": "--- a/",
        f"+++ b/{candidate}/": "+++ b/",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return normalized
