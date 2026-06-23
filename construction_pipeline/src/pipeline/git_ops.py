"""Git and worktree helpers for the outer pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a git command fails."""


@dataclass(slots=True)
class CommandResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


def run_command(
    args: list[str],
    cwd: Path,
    *,
    check: bool = False,
) -> CommandResult:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    result = CommandResult(
        args=args,
        cwd=cwd,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"Command failed in {cwd}: {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def run_git(args: list[str], cwd: Path, *, check: bool = False) -> CommandResult:
    return run_command(["git", *args], cwd, check=check)


def slugify_repo(repo: str) -> str:
    return repo.replace("/", "__").replace("\\", "__")


class FileLock:
    """A simple cross-process lock using exclusive file creation."""

    def __init__(self, path: Path, *, poll_seconds: float = 0.1, timeout_seconds: float = 300.0) -> None:
        self.path = path
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        deadline = time.time() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self._fd, str(os.getpid()).encode("ascii", errors="ignore"))
                return self
            except FileExistsError:
                if time.time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for lock: {self.path}")
                time.sleep(self.poll_seconds)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def ensure_clean_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def ensure_mirror(
    repo: str,
    source: str,
    mirrors_root: Path,
    *,
    refresh: bool = False,
) -> Path:
    mirror_path = mirrors_root / f"{slugify_repo(repo)}.git"
    lock_path = mirrors_root / f"{slugify_repo(repo)}.lock"
    mirrors_root.mkdir(parents=True, exist_ok=True)
    with FileLock(lock_path):
        if not mirror_path.exists():
            run_git(["clone", "--mirror", source, str(mirror_path)], mirrors_root, check=True)
        elif refresh:
            remote_update = run_git(["remote", "update", "--prune"], mirror_path)
            if remote_update.returncode != 0:
                # Local-path mirrors may not have a remote configured; tolerate that.
                fetch_result = run_git(["fetch", "--all", "--prune"], mirror_path)
                if fetch_result.returncode != 0:
                    raise GitError(
                        f"Failed to refresh mirror {mirror_path}\n"
                        f"remote update stderr:\n{remote_update.stderr}\n"
                        f"fetch stderr:\n{fetch_result.stderr}"
                    )
    return mirror_path


def resolve_no_patch_ref(
    mirror_path: Path,
    *,
    no_patch_ref: str | None,
    commit_after: str | None,
) -> str:
    if no_patch_ref:
        resolved = run_git(["rev-parse", "--verify", no_patch_ref], mirror_path, check=True)
        return resolved.stdout.strip()
    if commit_after:
        resolved = run_git(["rev-parse", "--verify", f"{commit_after}^"], mirror_path, check=True)
        return resolved.stdout.strip()
    raise ValueError("Instance must provide either 'no_patch_ref' or 'commit_after'.")


def create_detached_worktree(mirror_path: Path, worktree_path: Path, ref: str) -> Path:
    remove_worktree(mirror_path, worktree_path)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", "--detach", str(worktree_path), ref], mirror_path, check=True)
    return worktree_path


def remove_worktree(mirror_path: Path, worktree_path: Path) -> None:
    if not worktree_path.exists():
        return
    result = run_git(["worktree", "remove", "--force", str(worktree_path)], mirror_path)
    if result.returncode != 0 and worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
        run_git(["worktree", "prune"], mirror_path)


def apply_patch_text(worktree_path: Path, patch_text: str) -> CommandResult:
    temp_dir = Path(tempfile.mkdtemp(prefix="pipeline-patch-"))
    patch_path = temp_dir / "input.diff"
    patch_path.write_text(patch_text, encoding="utf-8")
    try:
        return run_git(["apply", "--binary", str(patch_path)], worktree_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def get_diff_text(worktree_path: Path) -> str:
    result = run_git(["diff", "HEAD"], worktree_path, check=True)
    return result.stdout


def write_diff(worktree_path: Path, output_path: Path) -> str:
    diff_text = get_diff_text(worktree_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(diff_text, encoding="utf-8")
    return diff_text


def worktree_is_clean(worktree_path: Path) -> bool:
    result = run_git(["status", "--porcelain"], worktree_path, check=True)
    return result.stdout.strip() == ""


def changed_files(worktree_path: Path) -> list[str]:
    result = run_git(["diff", "--name-only", "HEAD"], worktree_path, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]

