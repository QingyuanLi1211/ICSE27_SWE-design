#!/usr/bin/env python3
"""Check whether a patch cleanly applies to a repository at a given base ref."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def build_result(
    *,
    status: str,
    apply_check: str,
    repo_root: Path,
    base_ref: str,
    resolved_base_commit: str | None,
    validation_worktree: Path | None,
    patch_file: Path,
    stdout: str,
    stderr: str,
) -> dict[str, object]:
    return {
        "status": status,
        "apply_check": apply_check,
        "repo_root": str(repo_root),
        "base_ref": base_ref,
        "resolved_base_commit": resolved_base_commit,
        "validation_worktree": str(validation_worktree) if validation_worktree else None,
        "patch_file": str(patch_file),
        "stdout": stdout.strip() or None,
        "stderr": stderr.strip() or None,
    }


def print_result(result: dict[str, object]) -> int:
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a clean detached worktree at the requested base ref and run "
            "`git apply --check` against the supplied patch."
        )
    )
    parser.add_argument("--repo-root", required=True, help="Path to the target git repository.")
    parser.add_argument("--patch-file", required=True, help="Path to the patch file to validate.")
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Git ref or commit to validate against. Defaults to HEAD.",
    )
    parser.add_argument(
        "--keep-validation-worktree",
        action="store_true",
        help="Keep the temporary validation worktree for debugging.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    patch_file = Path(args.patch_file).resolve()
    validation_worktree: Path | None = None
    temp_root: Path | None = None
    resolved_base_commit: str | None = None

    if not patch_file.is_file():
        result = build_result(
            status="failed",
            apply_check="fail",
            repo_root=repo_root,
            base_ref=args.base_ref,
            resolved_base_commit=None,
            validation_worktree=None,
            patch_file=patch_file,
            stdout="",
            stderr=f"Patch file does not exist: {patch_file}",
        )
        return print_result(result)

    repo_check = run_git(["rev-parse", "--show-toplevel"], repo_root)
    if repo_check.returncode != 0:
        result = build_result(
            status="failed",
            apply_check="fail",
            repo_root=repo_root,
            base_ref=args.base_ref,
            resolved_base_commit=None,
            validation_worktree=None,
            patch_file=patch_file,
            stdout=repo_check.stdout,
            stderr=repo_check.stderr or f"Not a git repository: {repo_root}",
        )
        return print_result(result)

    repo_root = Path(repo_check.stdout.strip()).resolve()

    base_ref_check = run_git(["rev-parse", "--verify", args.base_ref], repo_root)
    if base_ref_check.returncode != 0:
        result = build_result(
            status="failed",
            apply_check="fail",
            repo_root=repo_root,
            base_ref=args.base_ref,
            resolved_base_commit=None,
            validation_worktree=None,
            patch_file=patch_file,
            stdout=base_ref_check.stdout,
            stderr=base_ref_check.stderr or f"Cannot resolve base ref: {args.base_ref}",
        )
        return print_result(result)

    resolved_base_commit = base_ref_check.stdout.strip()
    temp_root = Path(tempfile.mkdtemp(prefix="patch-apply-check-"))
    validation_worktree = temp_root / "validation-worktree"

    try:
        worktree_add = run_git(
            ["worktree", "add", "--detach", str(validation_worktree), resolved_base_commit],
            repo_root,
        )
        if worktree_add.returncode != 0:
            result = build_result(
                status="failed",
                apply_check="fail",
                repo_root=repo_root,
                base_ref=args.base_ref,
                resolved_base_commit=resolved_base_commit,
                validation_worktree=validation_worktree,
                patch_file=patch_file,
                stdout=worktree_add.stdout,
                stderr=worktree_add.stderr,
            )
            return print_result(result)

        apply_check = run_git(["apply", "--check", "--binary", str(patch_file)], validation_worktree)
        result = build_result(
            status="success" if apply_check.returncode == 0 else "failed",
            apply_check="pass" if apply_check.returncode == 0 else "fail",
            repo_root=repo_root,
            base_ref=args.base_ref,
            resolved_base_commit=resolved_base_commit,
            validation_worktree=validation_worktree,
            patch_file=patch_file,
            stdout=apply_check.stdout,
            stderr=apply_check.stderr,
        )
        return print_result(result)
    finally:
        if validation_worktree and validation_worktree.exists() and not args.keep_validation_worktree:
            run_git(["worktree", "remove", "--force", str(validation_worktree)], repo_root)
        if temp_root and temp_root.exists() and not args.keep_validation_worktree:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

