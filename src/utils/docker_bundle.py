"""从本地 docker bundle 中解析并按需装载 instance image。"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
from pathlib import Path


def ensure_image_loaded(bundle_root: Path, repo_tag: str, *, runner) -> None:
    inspect = runner(["docker", "image", "inspect", repo_tag], cwd=None, check=False)
    if inspect.returncode == 0:
        return

    _load_image_from_bundle(bundle_root, repo_tag, runner=runner)

    verify = runner(["docker", "image", "inspect", repo_tag], cwd=None, check=False)
    if verify.returncode != 0:
        raise RuntimeError(f"Docker load completed but `{repo_tag}` is still not inspectable.")


def _load_image_from_bundle(bundle_root: Path, repo_tag: str, *, runner) -> None:
    if bundle_root.is_file():
        _docker_load(bundle_root, repo_tag, runner=runner)
        return

    manifest_path = bundle_root / "manifest.json"
    repositories_path = bundle_root / "repositories"
    if manifest_path.exists() and repositories_path.exists():
        manifest_entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        selected = next((entry for entry in manifest_entries if repo_tag in (entry.get("RepoTags") or [])), None)
        if selected is None:
            raise KeyError(f"Could not locate manifest entry for `{repo_tag}` in {bundle_root}.")

        repositories = json.loads(repositories_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="instance-image-") as tmp_dir:
            tar_path = Path(tmp_dir) / "image.tar"
            _write_minimal_load_tar(bundle_root, tar_path, selected, repositories)
            _docker_load(tar_path, repo_tag, runner=runner)
        return

    if bundle_root.is_dir():
        tar_path = _find_per_instance_tar(bundle_root, repo_tag)
        if tar_path is not None:
            _docker_load(tar_path, repo_tag, runner=runner)
            return

    raise FileNotFoundError(
        f"Unsupported docker bundle input for `{repo_tag}`: {bundle_root}. "
        "Expected a docker-save tar, an extracted docker bundle directory, or a directory of per-instance tar files."
    )


def _docker_load(tar_path: Path, repo_tag: str, *, runner) -> None:
    loaded = runner(["docker", "load", "-i", str(tar_path)], cwd=None, check=False, timeout_seconds=3600)
    if loaded.returncode != 0:
        raise RuntimeError(
            f"Failed to load docker image `{repo_tag}` from {tar_path}\n"
            f"stdout:\n{loaded.stdout}\nstderr:\n{loaded.stderr}"
        )


def _find_per_instance_tar(bundle_root: Path, repo_tag: str) -> Path | None:
    candidates = [bundle_root / f"{key}.tar" for key in _repo_tag_tar_keys(repo_tag)]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Fallback for bundles whose tar filenames do not encode the repo tag directly
    # (for example `instance__Checkmk_checkmk_5990.tar` while the repo tag is
    # `customsweb/instance:5990`). Match by manifest RepoTags instead.
    for candidate in sorted(bundle_root.glob("*.tar")):
        if _tar_contains_repo_tag(candidate, repo_tag):
            return candidate
    return None


def _repo_tag_tar_keys(repo_tag: str) -> list[str]:
    keys: list[str] = []

    def _add(value: str) -> None:
        if value not in keys:
            keys.append(value)

    if ":" in repo_tag:
        _add(repo_tag.split(":", 1)[1])

    normalized = repo_tag.replace("/", "_").replace(":", "__")
    _add(normalized)
    _add(repo_tag.replace("/", "_").replace(":", "_"))

    if "-" in normalized:
        head, tail = normalized.rsplit("-", 1)
        if tail.isdigit():
            _add(f"{head}__{tail}")
    if "__" in normalized:
        head, tail = normalized.rsplit("__", 1)
        if tail.isdigit():
            _add(f"{head}-{tail}")

    return keys


def _tar_contains_repo_tag(tar_path: Path, repo_tag: str) -> bool:
    try:
        with tarfile.open(tar_path, "r") as tar:
            manifest_member = tar.extractfile("manifest.json")
            if manifest_member is None:
                return False
            manifest_entries = json.load(manifest_member)
    except (OSError, tarfile.TarError, json.JSONDecodeError):
        return False

    for entry in manifest_entries:
        repo_tags = entry.get("RepoTags") or []
        if repo_tag in repo_tags:
            return True
    return False


def _write_minimal_load_tar(bundle_root: Path, output_tar: Path, selected_entry: dict, repositories: dict) -> None:
    payload_paths = [selected_entry["Config"], *selected_entry["Layers"]]
    manifest_bytes = json.dumps([selected_entry], ensure_ascii=False).encode("utf-8")
    repositories_bytes = json.dumps(repositories, ensure_ascii=False).encode("utf-8")

    with tarfile.open(output_tar, "w") as tar:
        _add_bytes(tar, "manifest.json", manifest_bytes)
        _add_bytes(tar, "repositories", repositories_bytes)
        for relative_path in payload_paths:
            tar.add(bundle_root / relative_path, arcname=relative_path)


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))
