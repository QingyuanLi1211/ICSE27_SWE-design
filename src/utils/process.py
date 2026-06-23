"""共享的本地命令执行与强制删除目录工具。"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ShellResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    args: list[str],
    *,
    cwd: Path | None,
    check: bool = False,
    timeout_seconds: int | float | None = None,
) -> ShellResult:
    completed = subprocess.run(
        args,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    result = ShellResult(args=args, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def rmtree_force(path: Path) -> None:
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
