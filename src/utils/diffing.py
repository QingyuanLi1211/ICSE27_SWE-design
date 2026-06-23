"""清理 worktree 运行噪音并生成标准化 agent patch。"""

from __future__ import annotations

import difflib
import os
import re
import shutil
from pathlib import Path


NOISE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    ".nox",
    # Harness outputs must never be treated as repository edits.
    "output_data",
    "output_data_batch",
}

ALWAYS_EXCLUDED_PARTS = {
    ".git",
    ".gradle",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
}

ALWAYS_EXCLUDED_FILENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

ALWAYS_EXCLUDED_SUFFIXES = {
    ".bak",
    ".class",
    ".ear",
    ".gz",
    ".jar",
    ".log",
    ".orig",
    ".png",
    ".pyo",
    ".pyc",
    ".tar",
    ".temp",
    ".tgz",
    ".tmp",
    ".war",
    ".zip",
}

JAVA_REPO_KEYS = {
    "buck",
    "closure-compiler",
    "closure-templates",
    "doris",
    "elasticsearch",
    "media",
    "nomulus",
}

PYTHON_REPO_KEYS = {
    "checkmk",
    "pants",
    "pytorch",
    "ray",
    "sentry",
    "zulip",
}

JAVA_ALLOWED_SUFFIXES = {
    ".bzl",
    ".bazel",
    ".c",
    ".cc",
    ".css",
    ".cpp",
    ".gradle",
    ".groovy",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".jj",
    ".js",
    ".json",
    ".kt",
    ".kts",
    ".md",
    ".properties",
    ".proto",
    ".scala",
    ".sh",
    ".soy",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

PYTHON_ALLOWED_SUFFIXES = {
    ".bzl",
    ".bazel",
    ".cfg",
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".cu",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".jinja",
    ".js",
    ".json",
    ".md",
    ".py",
    ".pyi",
    ".pyx",
    ".pxd",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}

GENERIC_ALLOWED_SUFFIXES = JAVA_ALLOWED_SUFFIXES | PYTHON_ALLOWED_SUFFIXES

CHECKMK_ALLOWED_SUFFIXES = PYTHON_ALLOWED_SUFFIXES | {
    ".include",
    ".mk",
}

ALLOWED_EXTENSIONLESS_FILENAMES = {
    "BUILD",
    "BUCK",
    "Dockerfile",
    "Makefile",
    "MODULE.bazel",
    "WORKSPACE",
}


def sanitize_worktree_for_diff(root: Path) -> Path:
    for directory in root.rglob("*"):
        if directory.is_dir() and directory.name in NOISE_DIR_NAMES:
            shutil.rmtree(directory, ignore_errors=True)
    for suffix in ("*.pyc", "*.pyo", "*.tmp", "*.temp"):
        for path in root.rglob(suffix):
            path.unlink(missing_ok=True)
    return root


def write_agent_patch(
    *,
    pristine_dir: Path,
    candidate_dir: Path,
    output_path: Path,
    runner,
    repo_key: str | None = None,
) -> str:
    diff = runner(
        ["git", "diff", "--no-index", "--binary", "--ignore-cr-at-eol", str(pristine_dir), str(candidate_dir)],
        cwd=None,
        check=False,
        timeout_seconds=600,
    )
    if diff.returncode not in (0, 1):
        raise RuntimeError(f"Failed to diff worktrees\nstdout:\n{diff.stdout}\nstderr:\n{diff.stderr}")
    normalized = _normalize_no_index_diff(diff.stdout, pristine_dir=pristine_dir, candidate_dir=candidate_dir)
    if not normalized and diff.stderr.strip():
        normalized = _fallback_tree_diff(pristine_dir=pristine_dir, candidate_dir=candidate_dir)
    filtered = filter_agent_patch_text(normalized, repo_key=repo_key)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(filtered, encoding="utf-8")
    return filtered


def filter_agent_patch_text(patch_text: str, *, repo_key: str | None = None) -> str:
    filtered, _ = filter_agent_patch_text_with_report(patch_text, repo_key=repo_key)
    return filtered


def filter_agent_patch_text_with_report(
    patch_text: str, *, repo_key: str | None = None
) -> tuple[str, list[str]]:
    """Drop generated/dependency artifacts and non-source edits from agent patches.

    Agents sometimes run builds before submission; without filtering, generated
    files such as `build/classes/*.class` can dominate the patch and make eval
    fail for reasons unrelated to the intended repair.
    """
    kept_sections: list[str] = []
    dropped_paths: list[str] = []
    for section in _split_git_patch_sections(patch_text):
        paths = _paths_from_git_patch_section(section)
        if paths and all(_is_reasonable_patch_path(path, repo_key=repo_key) for path in paths):
            kept_sections.append(section)
            continue
        for path in paths:
            if path not in dropped_paths:
                dropped_paths.append(path)
    return "".join(kept_sections), dropped_paths


def _split_git_patch_sections(patch_text: str) -> list[str]:
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", patch_text)]
    if not starts:
        return []
    starts.append(len(patch_text))
    return [patch_text[starts[index] : starts[index + 1]] for index in range(len(starts) - 1)]


def _paths_from_git_patch_section(section: str) -> list[str]:
    header = re.search(r"(?m)^diff --git a/(.*?) b/(.*?)$", section)
    paths: list[str] = []
    if header:
        paths.extend([header.group(1), header.group(2)])
    for line in section.splitlines():
        candidate = _extract_path_from_diff_header(line)
        if candidate and candidate != "/dev/null":
            paths.append(candidate)
    unique: list[str] = []
    for path in paths:
        normalized = path.strip().strip('"')
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique


def _is_reasonable_patch_path(path: str, *, repo_key: str | None = None) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized or normalized in {"EOF", "TESTEOF"}:
        return False
    parts = normalized.split("/")
    basename = parts[-1]
    if any(part in ALWAYS_EXCLUDED_PARTS for part in parts):
        return False
    if basename in ALWAYS_EXCLUDED_FILENAMES:
        return False
    suffix = Path(basename).suffix
    if suffix in ALWAYS_EXCLUDED_SUFFIXES:
        return False
    if repo_key == "checkmk" and _is_checkmk_legacy_source_path(parts, suffix):
        return True
    if basename in ALLOWED_EXTENSIONLESS_FILENAMES:
        return True
    if suffix == "":
        # Allow extensionless scripts in conventional source/script locations.
        return parts[0] in {"bin", "dev", "script", "scripts", "tools"}
    allowed_suffixes = _allowed_suffixes_for_repo(repo_key)
    return suffix in allowed_suffixes


def _allowed_suffixes_for_repo(repo_key: str | None) -> set[str]:
    if repo_key == "checkmk":
        return CHECKMK_ALLOWED_SUFFIXES
    if repo_key in JAVA_REPO_KEYS:
        return JAVA_ALLOWED_SUFFIXES
    if repo_key in PYTHON_REPO_KEYS:
        return PYTHON_ALLOWED_SUFFIXES
    return GENERIC_ALLOWED_SUFFIXES


def _is_checkmk_legacy_source_path(parts: list[str], suffix: str) -> bool:
    """Allow Checkmk's legacy source files that intentionally have no .py suffix."""
    if suffix:
        return False
    if len(parts) == 2 and parts[0] == "checks":
        return True
    if len(parts) == 3 and parts[0] == "agents" and parts[1] in {"plugins", "special"}:
        return True
    if len(parts) == 2 and parts[0] == "active_checks":
        return True
    return False


def extract_edited_paths_from_patch(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        candidate = _extract_path_from_diff_header(line)
        if candidate and candidate != "/dev/null" and candidate not in paths:
            paths.append(candidate)
    return paths


def _extract_path_from_diff_header(line: str) -> str | None:
    match = re.match(r'^(?:\+\+\+|---)\s+"?([ab])/(.+?)"?$', line.strip())
    if match is None:
        return None
    return match.group(2).strip()


def _normalize_no_index_diff(text: str, *, pristine_dir: Path, candidate_dir: Path) -> str:
    normalized = text
    for prefix, path in (("a", pristine_dir), ("b", candidate_dir)):
        escaped = str(path).replace("\\", "\\\\")
        normalized = normalized.replace(f"{prefix}/{escaped}", prefix)
    return normalized


def _fallback_tree_diff(*, pristine_dir: Path, candidate_dir: Path) -> str:
    """Generate a text patch when git's directory diff trips over bad fixtures."""
    chunks: list[str] = []
    pristine_files = _regular_files_by_relpath(pristine_dir)
    candidate_files = _regular_files_by_relpath(candidate_dir)
    for relpath in sorted(set(pristine_files) | set(candidate_files)):
        old_path = pristine_files.get(relpath)
        new_path = candidate_files.get(relpath)
        old_bytes = old_path.read_bytes() if old_path is not None else b""
        new_bytes = new_path.read_bytes() if new_path is not None else b""
        if old_bytes == new_bytes:
            continue
        old_text = _decode_patch_text(old_bytes)
        new_text = _decode_patch_text(new_bytes)
        if old_text is None or new_text is None:
            continue
        chunks.append(
            _unified_file_diff(
                relpath=relpath,
                old_text=old_text,
                new_text=new_text,
                old_exists=old_path is not None,
                new_exists=new_path is not None,
            )
        )
    return "".join(chunks)


def _regular_files_by_relpath(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in NOISE_DIR_NAMES and not (Path(current_root) / dirname).is_symlink()
        ]
        for filename in filenames:
            path = Path(current_root) / filename
            if path.is_symlink() or not path.is_file():
                continue
            relpath = path.relative_to(root).as_posix()
            files[relpath] = path
    return files


def _decode_patch_text(content: bytes) -> str | None:
    if b"\x00" in content:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _unified_file_diff(
    *, relpath: str, old_text: str, new_text: str, old_exists: bool, new_exists: bool
) -> str:
    old_lines = _normalized_diff_lines(old_text)
    new_lines = _normalized_diff_lines(new_text)
    fromfile = f"a/{relpath}"
    tofile = f"b/{relpath}"
    header = f"diff --git a/{relpath} b/{relpath}\n"
    if not old_exists and new_exists:
        fromfile = "/dev/null"
        header += "new file mode 100644\n"
    elif old_exists and not new_exists:
        tofile = "/dev/null"
        header += "deleted file mode 100644\n"
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm="\n",
    )
    return header + "".join(diff_lines)


def _normalized_diff_lines(text: str) -> list[str]:
    """Compare logical lines so CRLF/LF rewrites do not become whole-file patches."""
    if text == "":
        return []
    lines = text.splitlines()
    normalized = [f"{line}\n" for line in lines]
    if text.endswith(("\n", "\r")):
        return normalized
    if normalized:
        normalized[-1] = normalized[-1].removesuffix("\n")
    return normalized
