# Task Goal

Your current working directory is a temporary Git worktree for `repo`.
The outer script has already prepared this state:

- `HEAD` is already checked out to `no_patch`
- the current main worktree must start clean, with no uncommitted changes
- `design_issue_related_patch` and `full_patch` are provided directly as inputs

Your task is to directly write a new trigger test and validate it using real execution against this three-state matrix:

- required:
  - `no_patch`: FAIL
  - `full_patch`: PASS
- preferred:
  - `design_issue_patch`: PASS

The final accepted main result must therefore be one of:

- `FAIL/PASS/PASS`
- `FAIL/FAIL/PASS`

You must not infer PASS or FAIL from reading code only. You must actually run the test.

Your final deliverable is not a patch string inside JSON.
Instead:

1. leave only the newly created trigger-test file changes in the current main worktree
2. ensure the final test patch cleanly applies
3. return a lightweight JSON result

The outer script will extract patches with `git diff HEAD`:

- when `status = success`, it will run `git diff HEAD` in the current main worktree and store the main trigger-test patch
- when `status = failed` and `cached_candidate != null`, it will run `git diff HEAD` in `candidate_capture_repo_root` and store the candidate patch there

Therefore:

- when `status = success`, the current main worktree must keep only the final new trigger-test file changes, and `candidate_capture_repo_root` must be clean
- when `status = failed` and `cached_candidate != null`, the current main worktree must be clean, and `candidate_capture_repo_root` must keep only that candidate test's new file changes
- when `status = blocked`, or `status = failed` with `cached_candidate = null`, both the current main worktree and `candidate_capture_repo_root` must be clean


# Key Requirements And Constraints

- First compare `issue_code` and `clean_code` to find the minimum sufficient behavior gap. Do not collapse this into a loose smoke test.
- Then use `design_issue_related_patch` to understand how the design issue is actually repaired.
- You may and should search the whole repository for useful tests, fixtures, helpers, directory structure, and naming patterns. `seed_testfile_path` and `step2_selected_test_*` are high-signal starting points, not the full search space.
- Reuse the repository's existing test style, naming conventions, fixtures, and assertion style whenever possible.
- All human-facing path fields, such as `files_changed`, must be repo-relative paths without `a/` or `b/` prefixes.
- You may only create a new standalone trigger-test file.
- You must not modify, delete, rewrite, or append to any existing test file.
- You must not modify any production code, configuration source, input patch content, or any non-test file.
- If a repository cannot discover and run a test that is only added as a new file, do not expand the scope by editing existing tests or test registration logic. Return `blocked` and explain why.
- Prefer testing public behavior, return values, exceptions, state changes, or output structure. Avoid unstable implementation details unless there is no more stable entry point.
- Do not rely on network access, randomness, sleeps, timing races, external services, fragile log strings, or incidental environment behavior.
- If a candidate test still passes under `no_patch`, it is not a valid trigger test and must be revised.
- If a candidate test only repeats existing assertions or does not capture the before-vs-after behavior gap, it is not a valid trigger test and must be revised.
- If `docker_required = true`, all tests and verification commands must run through `docker_exec_template`. Do not assume host execution is allowed.
- If `step2_selected_test_command` is provided, use it first when deriving the minimal verification command. Fall back to `test_command_hint` or your own search only if it is clearly unsuitable.
- If `step2_selected_test_path` is provided, inspect that file and nearby test patterns first. Fall back to `seed_testfile_path` or broader search only if it is clearly unsuitable.
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
- You must run a clean-apply check on the final main-result trigger-test patch.
- If `cached_candidate` exists, the candidate changes left in `candidate_capture_repo_root` must also cleanly apply relative to the current `HEAD`.


# Standard Workflow

1. Use `issue_code`, `clean_code`, and `design_issue_related_patch` to understand the real behavior gap and repair mechanism.
2. Search the whole repository for the most relevant existing tests, fixtures, and naming patterns. `seed_testfile_path` and `step2_selected_test_*` are only starting points.
3. In the current main worktree, create only a new standalone trigger-test file. Do not modify any existing file.
4. Generate a temporary patch from the current main worktree that contains only your new test file, and call `apply_check_script_path` to verify that the test patch cleanly applies relative to the current `HEAD`.
5. In disposable validation copies, verify the three states:
   - `no_patch`: current `HEAD` + your test patch
   - `design_issue_patch`: current `HEAD` + `design_issue_related_patch` + your test patch
   - `full_patch`: current `HEAD` + `full_patch` + your test patch
6. For each state, run the same target test command for real and record the most important PASS/FAIL evidence.
7. If a candidate test produces `FAIL/PASS/FAIL`, do not discard it silently. Record its command and matrix in `cached_candidate`, and leave that candidate's new-file changes in `candidate_capture_repo_root` so the outer script can later extract a candidate patch with `git diff HEAD`.
8. If you achieve `FAIL/PASS/PASS`, return that as the main success result.
9. If you do not achieve `FAIL/PASS/PASS` but you do achieve `FAIL/FAIL/PASS`, you may still return success, but `summary` must clearly explain why `design_issue_related_patch` did not pass.
10. If you achieve neither `FAIL/PASS/PASS` nor `FAIL/FAIL/PASS`, return `failed`. If the best candidate is `FAIL/PASS/FAIL`, store it in `cached_candidate`.
11. Before finishing, re-check worktree state:
    - `success`: the current main worktree keeps only the final new trigger-test file changes; `candidate_capture_repo_root` is clean
    - `failed` with `cached_candidate != null`: the current main worktree is clean; `candidate_capture_repo_root` keeps only the candidate trigger-test new-file changes
    - `blocked` or `failed` with `cached_candidate = null`: both worktrees are clean
12. Return exactly one JSON object. Do not use Markdown fences. Do not add extra explanation outside the JSON.


# Decision Rules

- Hard requirements:
  - `no_patch` must FAIL
  - `full_patch` must PASS
- Preferred target:
  - `design_issue_patch` should PASS if possible
- If `design_issue_patch` does not pass:
  - explain whether the issue is that `design_issue_related_patch` is an insufficient subset, or that your test design is still wrong
  - but do not violate the hard requirement that `full_patch` must PASS
- If `design_issue_related_patch` or `full_patch` cannot cleanly apply in validation copies, or repository or environment state prevents real execution, return `blocked`
- You may return `success` only when the final test patch cleanly applies and the result matrix is `FAIL/PASS/PASS` or `FAIL/FAIL/PASS`


# Output Requirements

Return exactly one JSON object and nothing else.

JSON schema:
```json
{
  "status": "success | blocked | failed",
  "summary": "Briefly explain the minimum behavior gap, why no_patch fails, why design_issue_related_patch and full_patch pass, and if the result is FAIL/FAIL/PASS, why design_issue_related_patch still fails. Max 50 words.",
  "confidence": "high | medium | low",
  "result_matrix": "FAIL/PASS/PASS | FAIL/FAIL/PASS" | null,
  "files_changed": [
    "repo-relative new test file path, or an empty list"
  ],
  "test_command": "The minimal command used to verify the final main result, or if there is no main result, the last command used for the best candidate." | null,
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
  "cached_candidate": {
    "matrix": "FAIL/PASS/FAIL",
    "summary": "Briefly explain why it was not the final result. Max 20 words.",
    "files_changed": [
      "repo-relative new test file path"
    ],
    "candidate_test_command": "The minimal command used to verify this candidate.",
    "verification": {
      "no_patch": "FAIL",
      "design_issue_patch": "PASS",
      "full_patch": "FAIL"
    }
  } | null,
  "risks": "Residual risk or weak point. Max 20 words." | null
}
```


## Status Constraints

- When `status = success`, all of the following must hold:
  - `result_matrix = FAIL/PASS/PASS` or `FAIL/FAIL/PASS`
  - `files_changed` is non-empty
  - `verification.no_patch.status = FAIL`
  - `verification.full_patch.status = PASS`
  - the current main worktree keeps only the new test-file changes listed in `files_changed`
  - `candidate_capture_repo_root` is clean
- When `status = blocked`:
  - `result_matrix = null`
  - `files_changed = []`
  - both the current main worktree and `candidate_capture_repo_root` are clean
- When `status = failed`:
  - `result_matrix = null`
  - `files_changed = []`
  - if a `FAIL/PASS/FAIL` candidate exists:
    - `cached_candidate` must be non-null
    - the current main worktree must be clean
    - `candidate_capture_repo_root` must keep only that candidate's new-file changes
  - if no `FAIL/PASS/FAIL` candidate exists:
    - `cached_candidate = null`
    - both the current main worktree and `candidate_capture_repo_root` must be clean
- `cached_candidate` is only for a `FAIL/PASS/FAIL` candidate. If none exists, set it to `null`.
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

- `candidate_capture_repo_root`
  {{candidate_capture_repo_root}}

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
