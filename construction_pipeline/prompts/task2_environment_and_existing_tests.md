# Task Goal

Your current working directory is a temporary Git worktree for `repo`.
The outer script has already prepared this state:

- `HEAD` is already checked out to `no_patch`
- the current worktree must start clean, with no uncommitted changes
- `design_issue_related_patch` and `full_patch` are provided directly as inputs
- if `docker_required = true`, the outer script also provides SWE-bench-style Docker build context so you can build or reuse an executable environment

Your task is not to write new tests, and it is not just to check whether tests pass in the final state.
Your task is equivalent to:

1. build or reuse an executable environment in a SWE-bench-style layered way
2. verify that all three variants are executable
3. within a limited search budget, classify existing repository tests as:
   - regression existing tests: `PASS/PASS/PASS`
   - trigger existing tests, preferably `FAIL/PASS/PASS`, secondarily `FAIL/FAIL/PASS`

The three variants are:

- `no_patch`
- `design_issue_patch`
- `full_patch`

Specifically:

- `no_patch` = the current `HEAD`
- `design_issue_patch` = the current `HEAD` + `design_issue_related_patch`
- `full_patch` = the current `HEAD` + `full_patch`

`step2 success` means:

- Docker, build, bootstrap, and test runner are usable in a SWE-bench-style setup
- all three variants can be executed for real
- the candidate existing tests that you actually evaluated within budget have been classified
- even if no reusable trigger test is found, this can still be `success`

Do not continue into step3 yourself.
Whether step3 runs is decided by the outer script based on your `needs_new_trigger_test`.

Your final deliverable is:

- no source-code edits
- no new test files
- exactly one structured JSON result
- a clean main worktree when you finish


## Three-Layer Success Definition

Do not collapse "it runs successfully" into one layer.
You must judge these three layers separately.

### 1. Env Ready

At minimum this includes:

- if `docker_required = true`, the SWE-bench-style layers are prepared:
  - `base image`
  - `env image`
  - `instance image`
  - `instance container`
- required bootstrap and install commands can run
- the test runner can be invoked

### 2. Variant Executable

All three states must be verified for real:

- `no_patch`
- `design_issue_patch`
- `full_patch`

"Executable" does not mean the tests must pass.
It means:

- the patch can cleanly apply
- the target command can be launched
- you can obtain a real result such as PASS, FAIL, ERROR, import error, or bootstrap error

### 3. Test Availability

Within a limited number of search-and-run attempts, try to find:

- `regression_existing`: `PASS/PASS/PASS`
- `trigger_existing_strong`: `FAIL/PASS/PASS`
- `trigger_existing_weak`: `FAIL/FAIL/PASS`

If no issue-sensitive existing test is found, that does not automatically make step2 fail.
It only means:

- `needs_new_trigger_test = true`


# Key Requirements And Constraints

- All conclusions must come from actual execution results. Do not infer environment usability, patch executability, or test classification from code inspection alone.
- By default, do not modify any production code, existing tests, configuration source, or patch contents.
- Your work should mainly happen in:
  - read-only inspection of the current main worktree
  - temporary Docker build directories, disposable validation copies, temporary worktrees, or temporary directories used for build, patch apply, and test execution
- Regardless of the final `status`, the current main worktree must be clean when you finish.
- If `docker_required = true`, you must follow the SWE-bench-style layered model:
  - `base image`
  - `env image`, shared by the same `repo_version`
  - `instance image`, specific to the current instance
  - `instance container`
- `base_image_key` is the logical cache key or recommended naming key for the base layer. It does not mean the image already exists.
- The logical cache key for the `env image` is exactly `repo_version`.
- The logical cache key for the `instance image` is derived from `id`.
- If these logical keys already map to usable images, reuse them. If they do not, build them and record the actual image names in the output.
- If `docker_required = true`, do not bypass Docker and run tests directly on the host. Bootstrap, dependency checks, and test commands must run through the container or through the execution mode implied by `docker_exec_template`.
- `rebuild_from` must indicate the minimum image layer that needs rebuilding:
  - `none`
  - `base`
  - `env`
  - `instance`
- Final `variant_execution` conclusions must come from real patch-apply and real command execution inside validation copies.
- Do not search the repository for tests aimlessly.
- If `seed_testfile_path` exists, you must start there and read that file yourself.
- You may search for other existing tests only when one of the following is true:
  - `seed_testfile_path` does not exist
  - `seed_testfile_path` is clearly unrelated to `issue_code`
  - the minimal command for `seed_testfile_path` cannot run
  - `seed_testfile_path` runs, but is not enough to determine whether an issue-sensitive existing test already exists
- Search budget is strict:
  - evaluate at most **3 candidate existing test file / command combinations**
  - if `seed_testfile_path` exists, it must consume one of those slots
  - do not run the full test suite unless `test_command_hint` or the project contract explicitly requires it
- Prefer reusing existing regression tests. Try to find existing trigger tests, but if they are not found, step3 is the fallback.
- Do not write new tests inside step2. Whether step3 runs is decided by the outer script based on `needs_new_trigger_test`.


## About SWE-bench-Style Environment Layering

Handle the environment with these layers:

1. `base image`
2. `env image`
3. `instance image`
4. `instance container / runtime`

If `docker_required = true`, your goal is not to improvise a one-off container.
Your goal is to follow this structure as closely as possible:

- `base image`: the common lowest-level base image
- `env image`: the environment layer shared by `repo_version`
- `instance image`: the per-instance layer built on top of the env image
- `instance container`: the runtime container launched from the instance image and used for actual bootstrap and testing
- `base_image_key` expresses how the base layer should be cached or named
- the logical key for the `env image` is exactly `repo_version`
- the logical key for the `instance image` is derived from `id`

If a layer already exists and is usable, prefer reuse. Do not rebuild entire stacks without reason.


# Standard Workflow

1. Use `issue_code`, `clean_code`, and `design_issue_related_patch` to understand the issue region and the minimum behavior difference.
2. If `docker_required = true`, use `repo_version`, `base_image_key`, `docker_build_hint`, and repository documentation to build or reuse:
   - `base image`
   - `env image`
   - `instance image`
   - `instance container`
3. Then use `install_or_bootstrap_hint`, repository documentation, test conventions, and the container execution mode to verify the basic environment:
   - whether the runner exists
   - whether required bootstrap succeeds
   - whether a minimal test command can be launched
   - record the actual `base_image_name`, `env_image_name`, `instance_image_name`, and `container_name`
4. In clean validation copies or temporary worktrees, check:
   - whether `design_issue_related_patch` cleanly applies
   - whether `full_patch` cleanly applies
5. In those validation copies, confirm that all three variants can actually execute, and fill `variant_execution` for:
   - `no_patch`
   - `design_issue_patch`
   - `full_patch`
6. Respect the test-search budget:
   - start from `seed_testfile_path` if it exists
   - only if necessary, evaluate up to 2 additional test files from the repository
   - each candidate should use the smallest practical test command
7. For each candidate command, run it on all three variants and classify the resulting matrix:
   - `PASS/PASS/PASS` -> `regression_existing`
   - `FAIL/PASS/PASS` -> `trigger_existing_strong`
   - `FAIL/FAIL/PASS` -> `trigger_existing_weak`
   - any other matrix -> do not treat it as a high-signal output for later steps
8. Produce `step2_selected_test_path` and `step2_selected_test_command` using this priority:
   - first: the smallest command for a `trigger_existing_strong` test
   - second: the smallest command for a `trigger_existing_weak` test
   - third: the smallest command for the regression existing test closest to the issue region
   - `step2_selected_test_path` must point to the same test file used by `step2_selected_test_command`
   - if none exists, set both fields to `null`
9. Decide:
   - whether `env_ready`
   - whether `needs_new_trigger_test`
   - whether `needs_new_regression_test`
   - if the build failed, which minimum layer needs rebuilding, as `rebuild_from`
10. Before finishing, confirm that the main worktree is clean.
11. Return exactly one JSON object. Do not use Markdown fences. Do not add extra explanation outside the JSON.


# Decision Rules

- `success`
  - `env_ready = true`
  - Docker, build, bootstrap, and the test runner are usable
  - all three variants were actually executed and classified as `ready`
  - the candidate existing tests evaluated within budget have been classified
  - it can still be `success` even if no existing trigger test was found
- `blocked`
  - Docker build, container startup, bootstrap, patch apply, test runner, repository state, or another concrete execution blocker prevents reliable execution of the three variants
  - or you cannot actually run tests in the given environment
- `failed`
  - you completed the within-budget analysis and real execution attempts, but still could not produce a reliable classification
  - and the problem is not a clear Docker / environment / patch-apply / runner blocker

Additional requirements:

- `needs_new_trigger_test = true` does not mean failure. If the environment is ready and the variants are executable, this should still be `success`.
- If no existing regression test is found, that does not automatically mean failure, but you must fill `needs_new_regression_test` truthfully.


# Output Requirements

Return exactly one JSON object and nothing else.

JSON schema:
```json
{
  "status": "success | blocked | failed",
  "summary": "Briefly explain whether the layered Docker build succeeded, whether all three variants were executable, the search scope, which regression or trigger existing tests were found, and whether step3 is needed. Max 50 words.",
  "confidence": "high | medium | low",
  "env_ready": true,
  "rebuild_from": "none | base | env | instance",
  "base_image_name": "The actual base image name used or built, or null when docker_required=false." | null,
  "env_image_name": "The actual env image name used or built, or null when docker_required=false." | null,
  "instance_image_name": "The actual instance image name used or built, or null when docker_required=false." | null,
  "container_name": "The actual instance container name used for execution, or null when docker_required=false." | null,
  "docker_build": {
    "base_image": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "Most important evidence phrase. Max 10 words."
    },
    "env_image": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "Most important evidence phrase. Max 10 words."
    },
    "instance_image": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "Most important evidence phrase. Max 10 words."
    },
    "instance_container": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "Most important evidence phrase. Max 10 words."
    }
  },
  "variant_execution": {
    "no_patch": {
      "status": "ready | blocked | failed",
      "evidence": "Most important evidence phrase. Max 10 words."
    },
    "design_issue_patch": {
      "status": "ready | blocked | failed",
      "evidence": "Most important evidence phrase. Max 10 words."
    },
    "full_patch": {
      "status": "ready | blocked | failed",
      "evidence": "Most important evidence phrase. Max 10 words."
    }
  },
  "existing_tests": {
    "regression_existing": [
      "repo-relative test file path"
    ],
    "trigger_existing_strong": [
      "repo-relative test file path"
    ],
    "trigger_existing_weak": [
      "repo-relative test file path"
    ]
  },
  "step2_selected_test_path": "The test path most worth reusing in later steps." | null,
  "step2_selected_test_command": "The test command most worth reusing in later steps." | null,
  "needs_new_trigger_test": true | false,
  "needs_new_regression_test": true | false,
  "risks": "Residual risk or weak point. Max 20 words." | null
}
```


## Status Constraints

- When `status = success`, all of the following must hold:
  - `env_ready = true`
  - if `docker_required = true`, then `base_image_name`, `env_image_name`, `instance_image_name`, and `container_name` must all be non-null
  - if `docker_required = true`, then `docker_build.base_image.status`, `docker_build.env_image.status`, `docker_build.instance_image.status`, and `docker_build.instance_container.status` must all be `ready`
  - `variant_execution.no_patch.status = ready`
  - `variant_execution.design_issue_patch.status = ready`
  - `variant_execution.full_patch.status = ready`
- When `status = blocked`:
  - at least one `docker_build[*].status = blocked`, or at least one `variant_execution[*].status = blocked`
  - `env_ready` must still reflect reality; it may be `true` if the blocker is only patch apply
- When `status = failed`:
  - the environment may still be usable
  - but you could not complete reliable classification within budget
- When `needs_new_trigger_test = false`, you must have found at least one:
  - `trigger_existing_strong`
  - or `trigger_existing_weak`
- When `needs_new_trigger_test = true`, it means step2 did not find a reusable issue-sensitive existing test; whether step3 runs is decided by the outer script
- All conclusions must come from real commands and real observed matrices. Do not fill them from inference.


# Input Context

## Repository And Version
- `id`: __CODEX_ID__
- `repo`: __CODEX_REPO__

## Docker / Build Context
- `docker_required`
  {{docker_required}}

- `repo_version`
  {{repo_version}}
  Meaning: the logical grouping key for the env layer; the logical env-image cache key is exactly this value.

- `base_image_key`
  {{base_image_key}}
  Meaning: the logical cache key or recommended naming key for the base image. It does not imply that the image already exists.

- `container_repo_root`
  {{container_repo_root}}

- `docker_exec_template`
  {{docker_exec_template}}
  Meaning: if the outer layer already defines a template for executing commands in the final container, you may reuse it. If it does not match the container you actually build, your final `container_name` is authoritative.

- `docker_build_hint`
  {{docker_build_hint}}

- `test_command_hint`
  {{test_command_hint}}

- `install_or_bootstrap_hint`
  {{install_or_bootstrap_hint}}

## Test Context
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
