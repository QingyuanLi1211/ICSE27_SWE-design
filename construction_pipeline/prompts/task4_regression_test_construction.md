# Task Goal

Your current working directory is a temporary Git worktree for `repo`.
The outer script has already prepared this state:

- `HEAD` is already checked out to `no_patch`
- the current main worktree must start clean, with no uncommitted changes
- `design_issue_related_patch` and `full_patch` are provided directly as inputs

This task is only called by the outer script when step2 concluded `needs_new_regression_test = true`.

Your task is to directly write a new regression test and validate it with real execution so that it passes in all three variants:

- `no_patch`: PASS
- `design_issue_patch`: PASS
- `full_patch`: PASS

The only accepted main result is:

- `PASS/PASS/PASS`

You must not infer PASS or FAIL from code inspection only. You must actually run the test.

Your final deliverable is not a patch string inside JSON.
Instead:

1. leave only the newly created regression-test file changes in the current main worktree
2. ensure the final test patch cleanly applies
3. return a lightweight JSON result

After you finish, the outer script will run `git diff HEAD` and save the final remaining changes in the current main worktree as the regression-test patch.
Therefore:

- when `status = success`, the current main worktree must keep only the final new regression-test file changes
- when `status = blocked` or `status = failed`, the current main worktree must be restored to a clean state, with no leftover candidate edits


# Key Requirements And Constraints

- First compare `issue_code` and `clean_code` to understand the issue region and identify a nearby behavior that should remain stable before and after the fix.
- The goal of a regression test is not to restate the trigger behavior. The goal is to verify that a stable public behavior near the issue was not broken by the fix.
- Do not turn the regression test into a vague smoke test. It should still stay close to the issue region and check a clear behavior boundary.
- You may and should search the whole repository for useful tests, fixtures, helpers, directory structure, and naming patterns. `seed_testfile_path` and `step2_selected_test_*` are high-signal starting points, not the full search space.
- Reuse the repository's existing test style, naming conventions, fixtures, and assertion style whenever possible.
- All human-facing path fields must be repo-relative and must not use `a/` or `b/` prefixes.
- You may only create a new standalone regression-test file.
- You must not modify, delete, rewrite, or append to any existing test file.
- You must not modify any production code, configuration source, input patch content, or any non-test file.
- If a repository cannot discover and run a test that is only added as a new file, do not expand the scope by editing existing tests or test registration logic. Return `blocked` and explain why.
- Prefer public behavior, return values, exceptions, state changes, or output shape. Avoid unstable implementation details unless there is no more stable entry point.
- Do not rely on network access, randomness, sleeps, timing races, external services, fragile log strings, or incidental environment behavior.
- If a candidate test fails in any of the three variants, it is not a valid regression test and must be revised.
- If a candidate test only repeats existing assertions or is unrelated to the issue neighborhood, it is not a useful regression test and must be revised.
- If `docker_required = true`, all tests and verification commands must run through `docker_exec_template`. Do not assume host execution is allowed.
- If `step2_selected_test_path` is provided, inspect that file and nearby patterns first. Fall back to `seed_testfile_path` or broader search only if it is clearly unsuitable.
- If `step2_selected_test_command` is provided, use it first when deriving the minimal verification command. Fall back to `test_command_hint` or your own search only if it is clearly unsuitable.
- Do not write a new patch-apply checker. Call the provided `apply_check_script_path`.


## About Environment And Execution

- If `docker_required = true`, substitute the real command into the `__CMD__` placeholder inside `docker_exec_template`.
- If you need to change directories inside the container, use `container_repo_root` as the repository root.
- If `docker_required = false`, execute commands directly in the current working directory.


## About Patches And Validation Copies

- `design_issue_related_patch` and `full_patch` are both input patches relative to the current `HEAD`.
- Do not leave either patch applied in the current main worktree.
- Patches extracted later by the outer script through `git diff HEAD` should remain standard git diffs. It is normal for those patch texts to contain `a/` and `b/` prefixes.
- When validating `no_patch`, `design_issue_patch`, and `full_patch`, create disposable validation copies, temporary worktrees, or temporary directories. Apply patches and run tests there, then clean them up.
- You must run a clean-apply check on the final regression-test patch.


# Standard Workflow

1. Use `issue_code`, `clean_code`, and `design_issue_related_patch` to understand the issue neighborhood and repair mechanism.
2. Search the whole repository for the most relevant tests, fixtures, and naming patterns. `seed_testfile_path` and `step2_selected_test_*` are only starting points.
3. Choose a stable behavior that should hold before and after the fix, and create only a new standalone regression-test file in the current main worktree. Do not modify any existing file.
4. Generate a temporary patch from the current main worktree that contains only your new test file, and call `apply_check_script_path` to verify that the test patch cleanly applies relative to the current `HEAD`.
5. In disposable validation copies, verify the three states:
   - `no_patch`: current `HEAD` + your test patch
   - `design_issue_patch`: current `HEAD` + `design_issue_related_patch` + your test patch
   - `full_patch`: current `HEAD` + `full_patch` + your test patch
6. For each state, run the same target test command for real and record the most important PASS/FAIL evidence.
7. If a candidate test does not achieve `PASS/PASS/PASS`, keep refining it until you either:
   - obtain a valid regression test
   - or determine that no valid regression test can be produced under the current constraints
8. Before finishing, re-check the current main worktree:
   - `success`: keep only the final new regression-test file changes
   - `blocked` or `failed`: restore it to a clean state
9. Return exactly one JSON object. Do not use Markdown fences. Do not add extra explanation outside the JSON.


# Decision Rules

- Hard requirements:
  - `no_patch` must PASS
  - `design_issue_patch` must PASS
  - `full_patch` must PASS
- If `design_issue_related_patch` or `full_patch` cannot cleanly apply in validation copies, or repository or environment state prevents real execution, return `blocked`
- You may return `success` only when the final test patch cleanly applies and the result matrix is `PASS/PASS/PASS`


# Output Requirements

Return exactly one JSON object and nothing else.

JSON schema:
```json
{
  "status": "success | blocked | failed",
  "summary": "Briefly explain which stable behavior you selected as the regression target, why it should hold in all three variants, and the final verification result. Max 50 words.",
  "confidence": "high | medium | low",
  "result_matrix": "PASS/PASS/PASS" | null,
  "files_changed": [
    "repo-relative new test file path"
  ],
  "test_command": "The minimal command used to verify the final main result." | null,
  "verification": {
    "no_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "Most important pass/fail evidence. Max 10 words."
    },
    "design_issue_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "Most important pass/fail evidence. Max 10 words."
    },
    "full_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "Most important pass/fail evidence. Max 10 words."
    }
  },
  "risks": "Residual risk or weak point. Max 20 words." | null
}
```


## Status Constraints

- When `status = success`, all of the following must hold:
  - `result_matrix = PASS/PASS/PASS`
  - `files_changed` is non-empty
  - `verification.no_patch.status = PASS`
  - `verification.design_issue_patch.status = PASS`
  - `verification.full_patch.status = PASS`
  - the current main worktree keeps only the new test-file changes listed in `files_changed`
- When `status = blocked`:
  - `result_matrix = null`
  - `files_changed = []`
  - the current main worktree must be clean
- When `status = failed`:
  - `result_matrix = null`
  - `files_changed = []`
  - the current main worktree must be clean
- Every `verification` status must come from real execution, not inference.
- If a state was not actually run, set it to `NOT_RUN` and explain why in `summary`.


# Input Context

## Repository And Version
- `id`: __CODEX_ID__
- `repo`: __CODEX_REPO__

## Docker / Execution Environment

- `docker_required`
  {{docker_required}}

- `container_repo_root`
  {{container_repo_root}}

- `docker_exec_template`
  {{docker_exec_template}}

## Test Context
- `apply_check_script_path`
  {{apply_check_script_path}}

- `step2_selected_test_path`
  {{step2_selected_test_path}}

- `step2_selected_test_command`
  {{step2_selected_test_command}}

- `test_command_hint`
  {{test_command_hint}}

- `install_or_bootstrap_hint`
  {{install_or_bootstrap_hint}}

- `seed_testfile_path`
  {{seed_testfile_path}}

## Code And Patches
- `issue_code`
  {{issue_code}}

- `clean_code`
  {{clean_code}}

- `design_issue_related_patch`
  {{design_issue_related_patch}}

- `full_patch`
  {{full_patch}}
