"""repair/eval 两侧的最小状态 JSON 模板。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def new_repair_status() -> dict:
    return {
        "image_available": False,
        "workspace_prepared": None,
        "workspace_writable": None,
        "agent_run_completed": None,
        "agent_modified_worktree": None,
        "agent_patch_generated": False,
    }


def new_eval_status() -> dict:
    return {
        "eval_workspace_prepared": False,
        "trigger_test_patch_applied": None,
        "regression_test_patch_applied": None,
        "ground_truth_patch_applied": False,
        "ground_truth_trigger_test_passed": False,
        "ground_truth_regression_test_passed": False,
        "eval_infrastructure_valid": False,
        "agent_patch_applied": None,
        "trigger_test_passed": None,
        "regression_test_passed": None,
        "agent_patch_passed": None,
    }


def new_infrastructure_eval_status() -> dict:
    return {
        "eval_workspace_prepared": False,
        "trigger_test_patch_applied": None,
        "regression_test_patch_applied": None,
        "ground_truth_patch_applied": False,
        "ground_truth_trigger_test_passed": False,
        "ground_truth_regression_test_passed": False,
        "eval_infrastructure_valid": False,
    }


def new_agent_eval_status() -> dict:
    return {
        "eval_workspace_prepared": False,
        "trigger_test_patch_applied": None,
        "regression_test_patch_applied": None,
        "agent_patch_applied": None,
        "trigger_test_passed": None,
        "regression_test_passed": None,
        "agent_patch_passed": None,
    }


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
