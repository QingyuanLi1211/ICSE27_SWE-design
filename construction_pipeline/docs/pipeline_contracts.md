# 四步任务统一契约

这份文档定义 **orchestrator 外层代码** 使用的统一输入输出 schema。  
四步任务的 prompt 可以继续按各自任务描述展开，但外层代码不要直接以 prompt 文本为契约，而应以这里的 canonical schema 为准。


## 设计原则

- `id` 是单条 instance 的稳定主键，四步任务始终使用同一个值。
- `repo` 是仓库标识，例如 `getsentry/sentry`；不要在不同 step 混用 `project` / `repo`。
- 外层代码负责：
  - 创建和清理 worktree / 临时副本
  - 渲染 prompt
  - 解析 Codex JSON
  - 持久化 patch / result artifact
  - 做必要的 schema 校验
- step1 的主结果 patch **不**由 Codex 放进 JSON；外层代码在 `status = success` 时，直接对当前主 worktree 运行 `git diff HEAD` 并保存为 artifact。
- step3 的主结果 patch **不**由 Codex 放进 JSON；外层代码在 `status = success` 时，直接对当前主 worktree 运行 `git diff HEAD` 并保存为 artifact。
- step3 的候选测试 patch 也**不**由 Codex 放进 JSON；外层代码在 `status = failed` 且 `cached_candidate != null` 时，直接对 `candidate_capture_repo_root` 运行 `git diff HEAD` 并保存为 artifact。
- step4 的主结果 patch **不**由 Codex 放进 JSON；外层代码在 `status = success` 时，直接对当前主 worktree 运行 `git diff HEAD` 并保存为 artifact。
- 对于“Codex 通过直接修改 worktree 产出的最终 patch”，统一以 `git diff HEAD` 作为 canonical patch 来源；不要让 Codex 在 JSON 中重复转写主结果 patch 文本。
- 所有 patch 都必须是：
  - git-compatible unified diff
  - 路径相对于 repo 根目录
- 所有返回给人的**路径列表字段**都必须：
  - 使用相对于 repo 根目录的路径
  - 不带 `a/`、`b/` 前缀
- 所有 patch 文本都保留标准 git diff 格式；其中出现 `a/`、`b/` 前缀是正常的，不需要移除


## 共享类型

```json
{
  "TaskStatus": "success | blocked | failed",
  "ApplyCheck": "pass | fail",
  "Confidence": "high | medium | low",
  "BuildStatus": "ready | blocked | failed | skipped",
  "VerificationStatus": "PASS | FAIL | NOT_RUN",
  "VariantMatrix": "FAIL/PASS/PASS | FAIL/FAIL/PASS | FAIL/PASS/FAIL",
  "RebuildFrom": "none | base | env | instance"
}
```


## 共享对象

### InstanceContext

```json
{
  "id": "stable instance id",
  "repo": "owner/name",
  "repo_root": "absolute path to the writable worktree"
}
```

### ExecutionContext

```json
{
  "apply_check_script_path": "absolute path to src/utils/check_patch_apply.py",
  "docker_required": true,
  "container_repo_root": "/workspace/repo",
  "docker_exec_template": "optional string or null; preferred exec template for the eventual container",
  "docker_build_hint": "optional string or null",
  "test_command_hint": "optional string or null",
  "install_or_bootstrap_hint": "optional string or null"
}
```

### DockerBuildContext

```json
{
  "repo_version": "string or null",
  "base_image_key": "string or null; logical cache key / preferred base-image name seed"
}
```

### CodeContext

```json
{
  "design_issue_comment": "optional string; step1 required",
  "issue_code": "function/class/body containing the design issue",
  "clean_code": "post-fix code with the design issue removed"
}
```

### PatchContext

```json
{
  "full_patch": "unified diff string",
  "design_issue_related_patch": "unified diff string or null"
}
```

### TestContext

```json
{
  "step2_selected_test_path": "string or null",
  "step2_selected_test_command": "string or null",
  "candidate_capture_repo_root": "absolute path to a clean writable worktree used only for preserving FAIL/PASS/FAIL candidates; step3 only",
  "seed_testfile_path": "repo-relative test path or null"
}
```

说明：

- `step2_selected_test_path` 与 `step2_selected_test_command` 都是 **step2 的显式输出**，用于给 step3 / step4 提供最小且高信号的起点。
- `seed_testfile_path` 不是 step2 输出；它来自已有的数据，作为额外提示，Codex 应自行读取该文件内容。


## Artifact 目录结构

建议每条 instance 固定落在：

```text
artifacts/<id>/
  manifest.json
  results/
    step1_result.json
    step2_result.json
    step3_result.json
    step4_result.json
  diffs/
    design_issue_related.diff
    trigger_test.diff
    trigger_test_candidate.diff
    regression_test.diff
```

写入规则：

- `manifest.json`
  - 保存该 instance 的统一索引信息，只记录结果文件路径、diff 文件路径和关键状态，不重复内嵌大段 diff 文本
- `results/step1_result.json`
  - 直接保存 step1 输出 JSON
- `diffs/design_issue_related.diff`
  - 当且仅当 step1 `status = success` 时，由外层代码对主 worktree 运行 `git diff HEAD` 提取
- `results/step2_result.json`
  - 直接保存 step2 输出 JSON
- `results/step3_result.json`
  - 直接保存 step3 输出 JSON
- `diffs/trigger_test.diff`
  - 当且仅当 step3 `status = success` 时，由外层代码对主 worktree 运行 `git diff HEAD` 提取
- `diffs/trigger_test_candidate.diff`
  - 当 step3 `status = failed` 且 `cached_candidate != null` 时，由外层代码对 `candidate_capture_repo_root` 运行 `git diff HEAD` 提取
- `results/step4_result.json`
  - 直接保存 step4 输出 JSON
- `diffs/regression_test.diff`
  - 当且仅当 step4 `status = success` 时，由外层代码对主 worktree 运行 `git diff HEAD` 提取

### 推荐 manifest schema

```json
{
  "id": "string",
  "repo": "string",
  "results": {
    "step1": "artifacts/<id>/results/step1_result.json",
    "step2": "artifacts/<id>/results/step2_result.json",
    "step3": "artifacts/<id>/results/step3_result.json",
    "step4": "artifacts/<id>/results/step4_result.json | null"
  },
  "diffs": {
    "design_issue_related": "artifacts/<id>/diffs/design_issue_related.diff | null",
    "trigger_test": "artifacts/<id>/diffs/trigger_test.diff | null",
    "trigger_test_candidate": "artifacts/<id>/diffs/trigger_test_candidate.diff | null",
    "regression_test": "artifacts/<id>/diffs/regression_test.diff | null"
  },
  "final_status": "success | blocked | failed",
  "notes": "string | null"
}
```


## Step1

### Step1Input

```json
{
  "instance": {
    "id": "string",
    "repo": "string",
    "repo_root": "string"
  },
  "execution": {
    "apply_check_script_path": "string"
  },
  "code": {
    "design_issue_comment": "string",
    "issue_code": "string",
    "clean_code": "string"
  },
  "patches": {
    "full_patch": "string"
  }
}
```

外层前置状态：

- `repo_root` 当前 worktree 已 checkout 到 `full_patch` 的基线提交
- `full_patch` 已应用到当前 worktree
- 当前未提交改动就是 `full_patch` 带来的改动

### Step1Output

```json
{
  "status": "success | blocked | failed",
  "apply_check": "pass | fail",
  "confidence": "high | medium | low",
  "included_paths": ["repo-relative path"] | null,
  "excluded_paths": ["repo-relative path"] | null,
  "contains_test_changes": "yes | no",
  "contains_test_changes_reason": "string | null",
  "summary": "string"
}
```

说明：

- step1 当前 prompt 输出中存在中文 key：
  - `"包含测试改动"` -> `contains_test_changes`
  - `"包含测试改动原因"` -> `contains_test_changes_reason`
- 外层代码应在 ingest 时统一归一化为英文 snake_case。
- step1 JSON 本身不再携带 `design_issue_related_patch`；外层代码应在 `status = success` 时对主 worktree 执行 `git diff HEAD`，并写出 `artifacts/<id>/diffs/design_issue_related.diff`。


## Step2

step2 负责两件事：

1. 按 SWE-bench 风格构建并验证环境是否可执行
2. 发现并分类仓库中已有测试，决定是否还需要新写 trigger test

### Step2Input

```json
{
  "instance": {
    "id": "string",
    "repo": "string",
    "repo_root": "string"
  },
  "execution": {
    "apply_check_script_path": "string",
    "docker_required": true,
    "container_repo_root": "string | null",
    "docker_exec_template": "string | null",
    "docker_build_hint": "string | null",
    "test_command_hint": "string | null",
    "install_or_bootstrap_hint": "string | null"
  },
  "docker_build": {
    "repo_version": "string | null",
    "base_image_key": "string | null"
  },
  "code": {
    "issue_code": "string",
    "clean_code": "string"
  },
  "patches": {
    "full_patch": "string",
    "design_issue_related_patch": "string"
  },
  "test_context": {
    "seed_testfile_path": "string | null"
  }
}
```

### Step2Output

```json
{
  "status": "success | blocked | failed",
  "summary": "string",
  "confidence": "high | medium | low",
  "env_ready": true,
  "rebuild_from": "none | base | env | instance",
  "base_image_name": "string | null",
  "env_image_name": "string | null",
  "instance_image_name": "string | null",
  "container_name": "string | null",
  "docker_build": {
    "base_image": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "string"
    },
    "env_image": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "string"
    },
    "instance_image": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "string"
    },
    "instance_container": {
      "status": "ready | blocked | failed | skipped",
      "evidence": "string"
    }
  },
  "variant_execution": {
    "no_patch": {
      "status": "ready | blocked | failed",
      "evidence": "string"
    },
    "design_issue_patch": {
      "status": "ready | blocked | failed",
      "evidence": "string"
    },
    "full_patch": {
      "status": "ready | blocked | failed",
      "evidence": "string"
    }
  },
  "existing_tests": {
    "regression_existing": ["repo-relative test path"],
    "trigger_existing_strong": ["repo-relative test path"],
    "trigger_existing_weak": ["repo-relative test path"]
  },
  "step2_selected_test_path": "string | null",
  "step2_selected_test_command": "string | null",
  "needs_new_trigger_test": true,
  "needs_new_regression_test": false,
  "risks": "string | null"
}
```

说明：

- `step2_selected_test_path` 和 `step2_selected_test_command` 是 step3 / step4 最值得直接消费的 step2 输出。
- `existing_tests` 主要用于审计和统计，不一定全部继续传给 step3。
- `docker_build` 用于显式记录 SWE-bench 风格的 `base -> env -> instance -> container` 构建结果。
- `env image` 的逻辑缓存键直接等于 `repo_version`。
- `instance image` 的逻辑缓存键由外层代码自动派生为 `<repo_short_name>__<id>`；如果 `id` 已经带该前缀，则直接使用 `id`。
- `base_image_name` / `env_image_name` / `instance_image_name` / `container_name` 记录的是**实际使用或构建出的实体名称**；它们与 `*_image_key` 不同，后者只是逻辑缓存键或推荐命名键。
- 如果 `needs_new_trigger_test = false`，外层 orchestrator 可以直接跳过 step3。
- 如果 `needs_new_regression_test = false`，外层 orchestrator 可以直接跳过 step4。


## Step3

### Step3Input

```json
{
  "instance": {
    "id": "string",
    "repo": "string",
    "repo_root": "string"
  },
  "execution": {
    "apply_check_script_path": "string",
    "docker_required": true,
    "container_repo_root": "string | null",
    "docker_exec_template": "string | null",
    "test_command_hint": "string | null",
    "install_or_bootstrap_hint": "string | null"
  },
  "code": {
    "issue_code": "string",
    "clean_code": "string"
  },
  "patches": {
    "full_patch": "string",
    "design_issue_related_patch": "string"
  },
  "test_context": {
    "step2_selected_test_path": "string | null",
    "step2_selected_test_command": "string | null",
    "candidate_capture_repo_root": "string | null",
    "seed_testfile_path": "string | null"
  }
}
```

外层前置状态：

- `repo_root` 当前 worktree 已 checkout 到 `no_patch`
- 当前主 worktree 初始应为干净状态
- 成功时，由 step3 在主 worktree 中留下最终新增测试文件改动
- 失败或阻塞时，由 step3 将主 worktree 恢复为干净状态

### Step3Output

```json
{
  "status": "success | blocked | failed",
  "summary": "string",
  "confidence": "high | medium | low",
  "result_matrix": "FAIL/PASS/PASS | FAIL/FAIL/PASS | null",
  "files_changed": ["repo-relative new test path"],
  "test_command": "string | null",
  "verification": {
    "no_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "string"
    },
    "design_issue_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "string"
    },
    "full_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "string"
    }
  },
  "cached_candidate": {
    "matrix": "FAIL/PASS/FAIL",
    "summary": "string",
    "files_changed": ["repo-relative new test path"],
    "candidate_test_command": "string | null",
    "verification": {
      "no_patch": "FAIL",
      "design_issue_patch": "PASS",
      "full_patch": "FAIL"
    }
  } | null,
  "risks": "string | null"
}
```

说明：

- `status = success` 时，外层代码还必须额外做一件事：
  - 在当前 `repo_root` 上执行 `git diff HEAD`
  - 将得到的 patch 写入 `artifacts/<id>/diffs/trigger_test.diff`
- `status = failed` 且 `cached_candidate != null` 时，外层代码还必须额外做一件事：
  - 在 `candidate_capture_repo_root` 上执行 `git diff HEAD`
  - 将得到的 patch 写入 `artifacts/<id>/diffs/trigger_test_candidate.diff`
- step3 JSON 本身不再携带主结果 patch 字符串。
- step3 JSON 也不再携带候选测试 patch 字符串。


## 步间传递规则

### Step1 -> Step2

step2 必须显式消费：

```json
{
  "id": "step1 input id",
  "repo": "step1 input repo",
  "issue_code": "same as step1 input",
  "clean_code": "same as step1 input",
  "full_patch": "same as step1 input",
  "design_issue_related_patch": "artifacts/<id>/diffs/design_issue_related.diff"
}
```

前置条件：

- step1 `status = success`
- step1 `apply_check = pass`
- 外层执行 `git diff HEAD` 后得到非空 `artifacts/<id>/diffs/design_issue_related.diff`

### Step2 -> Step3

step3 最小必要消费：

```json
{
  "id": "same stable id",
  "repo": "same repo",
  "issue_code": "same issue_code",
  "clean_code": "same clean_code",
  "full_patch": "same full_patch",
  "design_issue_related_patch": "same patch from step1",
  "step2_selected_test_path": "step2 output",
  "step2_selected_test_command": "step2 output"
}
```

推荐额外传递：

```json
{
  "seed_testfile_path": "upstream hint",
  "test_command_hint": "execution hint",
  "install_or_bootstrap_hint": "execution hint"
}
```

### Step2 -> Step4

step4 最小必要消费：

```json
{
  "id": "same stable id",
  "repo": "same repo",
  "issue_code": "same issue_code",
  "clean_code": "same clean_code",
  "full_patch": "same full_patch",
  "design_issue_related_patch": "same patch from step1",
  "step2_selected_test_path": "step2 output",
  "step2_selected_test_command": "step2 output"
}
```

推荐额外传递：

```json
{
  "seed_testfile_path": "upstream hint",
  "test_command_hint": "execution hint",
  "install_or_bootstrap_hint": "execution hint"
}
```


## Step4

### Step4Input

```json
{
  "instance": {
    "id": "string",
    "repo": "string",
    "repo_root": "string"
  },
  "execution": {
    "apply_check_script_path": "string",
    "docker_required": true,
    "container_repo_root": "string | null",
    "docker_exec_template": "string | null",
    "test_command_hint": "string | null",
    "install_or_bootstrap_hint": "string | null"
  },
  "code": {
    "issue_code": "string",
    "clean_code": "string"
  },
  "patches": {
    "full_patch": "string",
    "design_issue_related_patch": "string"
  },
  "test_context": {
    "step2_selected_test_path": "string | null",
    "step2_selected_test_command": "string | null",
    "seed_testfile_path": "string | null"
  }
}
```

### Step4Output

```json
{
  "status": "success | blocked | failed",
  "summary": "string",
  "confidence": "high | medium | low",
  "result_matrix": "PASS/PASS/PASS | null",
  "files_changed": ["repo-relative new test path"],
  "test_command": "string | null",
  "verification": {
    "no_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "string"
    },
    "design_issue_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "string"
    },
    "full_patch": {
      "status": "PASS | FAIL | NOT_RUN",
      "evidence": "string"
    }
  },
  "risks": "string | null"
}
```


## 外层校验要点

外层 orchestrator 至少应做这些校验：

- 四步任务的 `id` 完全一致
- 四步任务的 `repo` 完全一致
- step1 成功时：
  - `apply_check = pass`
  - 主 worktree 中存在非空 diff
  - 外层执行 `git diff HEAD` 后得到非空 `artifacts/<id>/diffs/design_issue_related.diff`
- step2 成功时：
  - `env_ready = true`
  - 如果 `docker_required = true`，`base_image_name`、`env_image_name`、`instance_image_name`、`container_name` 均为非空
  - 如果 `docker_required = true`，`docker_build.base_image.status`、`docker_build.env_image.status`、`docker_build.instance_image.status`、`docker_build.instance_container.status` 均为 `ready`
  - `variant_execution.full_patch.status = ready`
- step3 成功时：
  - `result_matrix` 为 `FAIL/PASS/PASS` 或 `FAIL/FAIL/PASS`
  - `files_changed` 非空
  - 主 worktree 仅包含这些新增测试文件改动
  - 外层执行 `git diff HEAD` 后得到非空 `artifacts/<id>/diffs/trigger_test.diff`
- step3 失败且 `cached_candidate != null` 时：
  - 主 worktree 为干净状态
  - `candidate_capture_repo_root` 仅包含候选测试新增文件改动
  - 外层执行 `git diff HEAD` 后得到非空 `artifacts/<id>/diffs/trigger_test_candidate.diff`
- step4 成功时：
  - `result_matrix = PASS/PASS/PASS`
  - `files_changed` 非空
  - 主 worktree 仅包含这些新增测试文件改动
  - 外层执行 `git diff HEAD` 后得到非空 `artifacts/<id>/diffs/regression_test.diff`
