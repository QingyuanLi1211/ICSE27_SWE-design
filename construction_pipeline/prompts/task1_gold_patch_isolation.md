# Task Goal

Your current working directory is a temporary Git worktree for `repo`.
The outer script has already prepared this state:

- `HEAD` is checked out to the parent commit of `full_patch`
- the complete `full_patch` has already been applied to the current worktree
- the visible uncommitted changes in the current worktree are exactly the changes introduced by `full_patch`

Your task is not to suggest changes or regenerate a patch.
Your task is to directly edit the current worktree so that it ends with only the code changes that are directly related to this design issue and that form the minimum sufficient fix set.

After you finish, the outer script will run `git diff HEAD` and save the final remaining changes in the current worktree as `design_issue_related_patch`.
Therefore:

- when `status = success`, the current worktree must keep only the final design-issue-related code changes
- when `status = blocked` or `status = failed`, the current worktree must be restored to a clean state, with no leftover candidate edits

After trimming the patch, you must:

1. generate a temporary patch from the current `git diff HEAD`
2. call the provided generic apply-check script on that patch and run a real clean-apply check
3. confirm that the patch cleanly applies before returning the final JSON


# Key Requirements And Constraints

- You may only trim the already-applied `full_patch`.
- Do not re-checkout commits, re-apply `full_patch`, or switch branches in the current main worktree.
- Do not write a new apply-check script. Call the provided `apply_check_script_path` directly.
- You may create one-off temporary patch files for validation, but they must not become part of the final deliverable and must be cleaned up.
- Do not introduce any new implementation, new files, or new logic outside the already-applied `full_patch`.
- By default, revert all test-related changes. Do not keep test files, fixtures, test helpers, or other test infrastructure in the final result.
- If you still keep any test-related change in the final result, you must explicitly mark `"contains_test_changes": "yes"` and explain why.
- If a change is only documentation, example code, formatting, renaming, unrelated refactoring, opportunistic cleanup, or compatibility work, and is not required to fix this design issue, revert it.
- If an import, dependency, type, constant, helper, or cross-file adjustment is necessary for the design-issue fix to work, keep it.
- Prefer the minimum sufficient change set. Do not keep extra edits.
- Do not leave temporary scripts, notes, debug files, or unrelated worktree changes behind.


# Standard Workflow

1. Use `design_issue_comment`, `issue_code`, and `clean_code` to understand the true fix point and the minimum necessary behavior difference.
2. Compare `full_patch` with the current `git diff HEAD` and identify which changed files and hunks are directly relevant to that fix point.
3. Revert fully unrelated files back to `HEAD`. For partially related files, keep only the necessary hunks and revert unrelated hunks.
4. After trimming, inspect `git status --short` and `git diff HEAD` to confirm that only design-issue-related changes remain.
5. Generate a temporary patch from the current `git diff HEAD`. This patch must be a git-compatible unified diff relative to the current `HEAD`.
6. Write that temporary patch into a temporary patch file and call `apply_check_script_path` to run a clean-apply check. The script will run `git apply --check` inside a clean validation copy that matches the current `HEAD`. You must set `apply_check` based on the script's actual execution result.
7. Decide whether the final patch still contains any test-related changes. If yes, explain why those test changes cannot reasonably be reverted.
8. Return exactly one JSON object. Do not use Markdown fences. Do not add extra explanation outside the JSON.


# Decision Rules

- You must keep at least the code changes that correspond to the actual fix point described by `design_issue_comment`.
- If you are unsure whether a change is necessary, use `issue_code` and `clean_code` to decide whether it truly participates in the fix.
- If the same commit includes other features, tests, documentation, compatibility work, or broad refactors that are not required for this design issue, remove them.
- `included_paths` must list only the files whose changes remain in the final worktree. Use repo-relative paths with no `a/` or `b/` prefix.
- `excluded_paths` must list only the files touched by the original `full_patch` that you reverted entirely back to `HEAD`. If a file was only partially reverted and still keeps some relevant hunks, it should appear only in `included_paths`, and `summary` should mention the hunk-level trimming.
- If the final `git diff HEAD` is empty, you must not return `success`. Return `failed` or `blocked` and explain why.
- You may return:
  - `"status": "success"`
  - `"apply_check": "pass"`
  only when the final `git diff HEAD` is non-empty and the apply-check script actually passed.


# Output Requirements

Return exactly one JSON object and nothing else.

JSON schema:
```json
{
  "status": "success | blocked | failed",
  "summary": "Briefly explain the main decision basis, what you kept, what you reverted, whether you did hunk-level trimming, and the apply-check result summary. Max 50 words.",
  "confidence": "high | medium | low",
  "apply_check": "pass | fail",
  "included_paths": ["repo-relative kept path"] | null,
  "excluded_paths": ["repo-relative path reverted entirely back to HEAD"] | null,
  "contains_test_changes": "yes | no",
  "contains_test_changes_reason": "If yes, briefly explain why. Otherwise null." | null
}
```


## Status Constraints

- When `status = success`, all of the following must be true:
  - `apply_check = pass`
  - `included_paths` is a non-empty list
  - the current worktree still keeps the final design-issue-related changes so the outer script can run `git diff HEAD`
  - if `"contains_test_changes" = "yes"`, then `"contains_test_changes_reason"` must be non-empty and specific
- When `status = blocked`:
  - `apply_check = fail`
  - the current worktree must be restored to a clean state
  - this means you were blocked by repository state, apply-check script availability, unstable patch generation, conflicting context, or another non-semantic reason
- When `status = failed`:
  - `apply_check = fail`
  - the current worktree must be restored to a clean state
  - this means you completed the analysis and attempts but could not produce a non-empty patch that also cleanly applies


# Input Context

- `id`: __CODEX_ID__
- `repo`: __CODEX_REPO__
- `apply_check_script_path`: __CODEX_APPLY_CHECK_SCRIPT_PATH__
- `design_issue_comment`: __CODEX_COMMENT__
- `issue_code`: __CODEX_ISSUE_CODE__
- `clean_code`: __CODEX_CLEAN_CODE__
- `full_patch`: __CODEX_FULL_PATCH__
