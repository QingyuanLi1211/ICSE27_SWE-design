"""Helpers for interpreting existing-test matrices."""

from __future__ import annotations

from typing import Literal

VerificationStatus = Literal["PASS", "FAIL", "NOT_RUN"]
ExistingTestCategory = Literal[
    "regression_existing",
    "trigger_existing_strong",
    "trigger_existing_weak",
]


def classify_existing_test_matrix(
    no_patch: VerificationStatus,
    design_issue_patch: VerificationStatus,
    full_patch: VerificationStatus,
) -> ExistingTestCategory | None:
    matrix = (no_patch, design_issue_patch, full_patch)
    if matrix == ("PASS", "PASS", "PASS"):
        return "regression_existing"
    if matrix == ("FAIL", "PASS", "PASS"):
        return "trigger_existing_strong"
    if matrix == ("FAIL", "FAIL", "PASS"):
        return "trigger_existing_weak"
    return None


def is_trigger_success_matrix(matrix: str | None) -> bool:
    return matrix in {"FAIL/PASS/PASS", "FAIL/FAIL/PASS"}


def is_regression_success_matrix(matrix: str | None) -> bool:
    return matrix == "PASS/PASS/PASS"

