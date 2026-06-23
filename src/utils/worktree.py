"""准备 repair/eval 共用的无 `.git` worktree。"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import uuid
from pathlib import Path


def prepare_repair_worktree(
    *,
    image_tag: str,
    base_commit: str,
    repo_slug: str | None,
    work_root: Path,
    runner,
) -> tuple[Path, Path]:
    work_root.mkdir(parents=True, exist_ok=True)
    # Keep directory names short on Windows to avoid MAX_PATH failures in deep vendored trees.
    pristine_dir = work_root / "p"
    candidate_dir = work_root / "c"
    _reset_dir(pristine_dir)
    _reset_dir(candidate_dir)
    prepare_pristine_tree(
        image_tag=image_tag,
        base_commit=base_commit,
        repo_slug=repo_slug,
        destination=pristine_dir,
        runner=runner,
    )
    copy_prepared_tree(pristine_dir, candidate_dir)
    _remove_git_metadata(candidate_dir)
    return pristine_dir, candidate_dir


def prepare_pristine_tree(
    *,
    image_tag: str,
    base_commit: str,
    repo_slug: str | None,
    destination: Path,
    runner,
) -> Path:
    _reset_dir(destination)
    exported_from_image = _try_export_from_image(
        image_tag=image_tag,
        base_commit=base_commit,
        destination=destination,
        runner=runner,
    )
    if not exported_from_image:
        if not repo_slug:
            raise RuntimeError(
                f"Failed to export repo from `{image_tag}`, and no repo slug was provided for clone fallback."
            )
        _clone_repo_at_commit(
            repo_slug=repo_slug,
            base_commit=base_commit,
            destination=destination,
            runner=runner,
        )
    _remove_git_metadata(destination)
    return destination


def workspace_is_writable(path: Path) -> bool:
    probe = path / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        return True
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


def copy_prepared_tree(source: Path, destination: Path) -> None:
    """Copy a prepared repo without expanding symlink-heavy test fixtures."""
    if shutil.which("robocopy") is not None:
        completed = subprocess.run(
            [
                "robocopy",
                str(source),
                str(destination),
                "/E",
                "/SL",
                "/R:2",
                "/W:1",
                "/NFL",
                "/NDL",
                "/NJH",
                "/NJS",
                "/NP",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        # Robocopy uses a bitmask: 0-7 are success/non-fatal copy states.
        if completed.returncode < 8:
            return
        raise RuntimeError(
            f"Failed to copy prepared worktree with robocopy from `{source}` to `{destination}`\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )

    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        symlinks=True,
        ignore_dangling_symlinks=True,
    )


def _copy_pristine_tree(source: Path, destination: Path) -> None:
    copy_prepared_tree(source, destination)


def _try_export_from_image(*, image_tag: str, base_commit: str, destination: Path, runner) -> bool:
    container_name = f"repo-export-{base_commit[:12]}-{uuid.uuid4().hex[:8]}"
    runner(["docker", "rm", "-f", container_name], cwd=None, check=False)
    create = runner(
        ["docker", "create", "--name", container_name, "--entrypoint", "bash", image_tag, "-lc", "tail -f /dev/null"],
        cwd=None,
        check=False,
        timeout_seconds=300,
    )
    if create.returncode != 0:
        raise RuntimeError(f"Failed to create export container for `{image_tag}`\n{create.stderr}")

    try:
        start = runner(["docker", "start", container_name], cwd=None, check=False, timeout_seconds=300)
        if start.returncode != 0:
            raise RuntimeError(f"Failed to start export container `{container_name}`\n{start.stderr}")

        try:
            repo_root = detect_repo_root(container_name=container_name, runner=runner)
        except RuntimeError:
            return False

        if _container_has_git_repo(container_name=container_name, repo_root=repo_root, runner=runner):
            checkout = runner(
                ["docker", "exec", container_name, "bash", "-lc", f"git -C {repo_root} checkout --detach {base_commit}"],
                cwd=None,
                check=False,
                timeout_seconds=300,
            )
            if checkout.returncode != 0:
                raise RuntimeError(
                    f"Failed to checkout `{base_commit}` in image `{image_tag}`\n"
                    f"stdout:\n{checkout.stdout}\nstderr:\n{checkout.stderr}"
                )
            _export_git_repo_tree(
                container_name=container_name,
                repo_root=repo_root,
                base_commit=base_commit,
                destination=destination,
            )
            return True

        _export_repo_tree(container_name=container_name, repo_root=repo_root, destination=destination)
        return True
    finally:
        runner(["docker", "rm", "-f", container_name], cwd=None, check=False, timeout_seconds=120)


def detect_repo_root(*, container_name: str, runner) -> str:
    script = textwrap.dedent(
        r"""
        set -e
        best_path=""
        best_score=0

        consider_dir() {
          p="$1"
          [ -d "$p" ] || return 0

          if git -C "$p" rev-parse --show-toplevel >/dev/null 2>&1; then
            git -C "$p" rev-parse --show-toplevel
            exit 0
          fi

          score=0
          for f in pants.toml pants.ini pyproject.toml setup.py setup.cfg package.json pom.xml build.gradle build.gradle.kts settings.gradle settings.gradle.kts mvnw gradlew Cargo.toml go.mod WORKSPACE WORKSPACE.bazel MODULE.bazel BUILD BUILD.bazel; do
            [ -f "$p/$f" ] && score=$((score + 10))
          done
          for d in src src/main src/main/java src/test src/test/java tests test tools app zerver java python; do
            [ -d "$p/$d" ] && score=$((score + 3))
          done

          if [ -f "$p/README.md" ] || [ -f "$p/README.rst" ] || [ -f "$p/README.txt" ]; then
            score=$((score + 1))
          fi

          if [ "$score" -gt "$best_score" ]; then
            best_score="$score"
            best_path="$p"
          fi
        }

        # Only inspect a fixed set of top-level candidate directories from the image.
        # Avoid scanning arbitrary child directories, which can select unrelated repos
        # such as tool caches or helper checkouts bundled in the container.
        for p in /workspace/repo /workspace/eval_repo /workspace /app /project /root /repo /src; do
          consider_dir "$p"
        done

        if [ "$best_score" -gt 0 ] && [ -n "$best_path" ]; then
          echo "$best_path"
          exit 0
        fi
        exit 1
        """
    ).strip()
    probe = runner(["docker", "exec", container_name, "bash", "-lc", script], cwd=None, check=False, timeout_seconds=180)
    if probe.returncode != 0:
        raise RuntimeError(
            f"Failed to detect repo root in `{container_name}`\nstdout:\n{probe.stdout}\nstderr:\n{probe.stderr}"
        )
    return probe.stdout.strip().splitlines()[-1].strip()


def _clone_repo_at_commit(*, repo_slug: str, base_commit: str, destination: Path, runner) -> None:
    remote_url = f"https://github.com/{repo_slug}.git"
    clone = None
    clone_error = ""
    for attempt in range(3):
        _reset_dir(destination)
        clone = runner(
            ["git", "clone", "--filter=blob:none", "--no-checkout", remote_url, str(destination)],
            cwd=None,
            check=False,
            timeout_seconds=1800,
        )
        if clone.returncode == 0:
            break
        clone = runner(
            ["git", "clone", "--no-checkout", remote_url, str(destination)],
            cwd=None,
            check=False,
            timeout_seconds=1800,
        )
        if clone.returncode == 0:
            break
        clone_error = clone.stderr
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    if clone is None or clone.returncode != 0:
        raise RuntimeError(
            f"Failed to clone `{remote_url}` for fallback worktree preparation\n"
            f"stdout:\n{'' if clone is None else clone.stdout}\nstderr:\n{clone_error or ('' if clone is None else clone.stderr)}"
        )

    checkout = runner(
        ["git", "-c", "advice.detachedHead=false", "checkout", "--detach", base_commit],
        cwd=destination,
        check=False,
        timeout_seconds=1800,
    )
    if checkout.returncode != 0:
        raise RuntimeError(
            f"Failed to checkout `{base_commit}` after cloning `{remote_url}`\n"
            f"stdout:\n{checkout.stdout}\nstderr:\n{checkout.stderr}"
        )


def _container_has_git_repo(*, container_name: str, repo_root: str, runner) -> bool:
    probe = runner(
        ["docker", "exec", container_name, "bash", "-lc", f"git -C {repo_root} rev-parse --is-inside-work-tree"],
        cwd=None,
        check=False,
        timeout_seconds=60,
    )
    return probe.returncode == 0


def _remove_git_metadata(root: Path) -> None:
    for path in [root / ".git", *root.rglob(".git")]:
        if not path.exists():
            continue
        if path.is_dir():
            _rmtree_force(path)
        else:
            path.unlink(missing_ok=True)


def _reset_dir(path: Path) -> None:
    if path.exists():
        _rmtree_force(path)
    path.mkdir(parents=True, exist_ok=True)


def _export_repo_tree(*, container_name: str, repo_root: str, destination: Path) -> None:
    copy = subprocess.run(
        ["docker", "cp", f"{container_name}:{repo_root}/.", str(destination)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if copy.returncode == 0:
        return

    tar_path = destination.parent / f"{destination.name}.tar"
    quoted_root = shlex.quote(repo_root)
    command = [
        "docker",
        "exec",
        container_name,
        "bash",
        "-lc",
        f"tar -C {quoted_root} --ignore-failed-read --warning=no-file-removed -chf - .",
    ]
    with tar_path.open("wb") as tar_handle:
        completed = subprocess.run(command, stdout=tar_handle, stderr=subprocess.PIPE, check=False)
    # GNU tar may still emit a usable archive while returning 1 for recoverable
    # warnings such as files that disappeared during traversal.
    if completed.returncode not in (0, 1):
        stderr = completed.stderr.decode("utf-8", errors="replace")
        tar_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to export repo tree from `{container_name}:{repo_root}`\n{stderr}")
    try:
        extracted = subprocess.run(
            ["tar", "--unlink-first", "-xf", str(tar_path), "-C", str(destination)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if extracted.returncode != 0:
            raise RuntimeError(
                f"Failed to export repo tree for `{container_name}:{repo_root}`.\n"
                f"`docker cp` stderr:\n{copy.stderr}\n"
                f"tar unpack stdout:\n{extracted.stdout}\ntar unpack stderr:\n{extracted.stderr}"
            )
    finally:
        tar_path.unlink(missing_ok=True)


def _export_git_repo_tree(*, container_name: str, repo_root: str, base_commit: str, destination: Path) -> None:
    tar_path = destination.parent / f"{destination.name}.tar"
    quoted_root = shlex.quote(repo_root)
    quoted_commit = shlex.quote(base_commit)
    command = [
        "docker",
        "exec",
        container_name,
        "bash",
        "-lc",
        f"git -C {quoted_root} archive --format=tar {quoted_commit}",
    ]
    with tar_path.open("wb") as tar_handle:
        completed = subprocess.run(command, stdout=tar_handle, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        tar_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to export git repo tree from `{container_name}:{repo_root}` at `{base_commit}`\n{stderr}"
        )
    try:
        extracted = subprocess.run(
            ["tar", "--unlink-first", "-xf", str(tar_path), "-C", str(destination)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if extracted.returncode != 0:
            raise RuntimeError(
                f"Failed to unpack git archive for `{container_name}:{repo_root}` at `{base_commit}`.\n"
                f"tar unpack stdout:\n{extracted.stdout}\n"
                f"tar unpack stderr:\n{extracted.stderr}"
            )
    finally:
        tar_path.unlink(missing_ok=True)


def _rmtree_force(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path, onerror=_rmtree_force_onerror)
    except OSError:
        if os.name != "nt":
            raise
        _rmtree_force_windows(path)


def _rmtree_force_onerror(func, target, exc_info) -> None:
    target_path = Path(target)
    if target_path.is_symlink():
        target_path.unlink(missing_ok=True)
        return
    try:
        target_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    except FileNotFoundError:
        return
    func(target)


def _rmtree_force_windows(path: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved.parent == resolved:
        raise RuntimeError(f"Refusing to recursively delete filesystem root: {resolved}")

    empty_dir = Path(tempfile.mkdtemp(prefix="empty-rmtree-"))
    try:
        completed = subprocess.run(
            [
                "robocopy",
                str(empty_dir),
                str(resolved),
                "/MIR",
                "/SL",
                "/R:2",
                "/W:1",
                "/NFL",
                "/NDL",
                "/NJH",
                "/NJS",
                "/NP",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode >= 8:
            raise RuntimeError(
                f"Failed to clear directory with robocopy: {resolved}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            raise RuntimeError(f"Failed to remove directory after robocopy cleanup: {resolved}")
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)
