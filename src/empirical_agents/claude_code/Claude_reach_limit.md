# Claude Code 限额熔断记录

## 背景

旧数据中发现 Claude Code 达到限额时，`step1.log` 不一定直接写出限额原因，但 `trajectory/.../runner_events.jsonl` 中会出现明确的限额事件，例如：

- `type=rate_limit_event` 且 `rate_limit_info.status=rejected`
- `api_error_status=429`
- `error=rate_limit`
- 文本结果包含 `You've hit your limit`

为了避免限额后 batch 继续自动调用 Claude Code，本目录下已经加入熔断机制。

## 当前实现

- `claude_code_runner.py`：流式读取 Claude Code stdout/stderr 时即时识别限额信号，一旦命中，立刻终止 Claude Code 进程树。
- `run_agent_fixing/main.py`：把限额结果写入 `agent_summary`，字段包括 `rate_limited`、`rate_limit_reason`、`completion_source`，并定义熔断退出码 `75`。
- `claude_code.py`：单条入口如果发现 `rate_limited=True`，停止后续 instance，并返回退出码 `75`。
- `batch_repair.py`：为 Claude Code batch 注册退出码 `75` 为熔断码。
- `common/batch_runner.py`：任意子进程返回熔断码后，清空 pending 队列，并终止所有正在运行的并发子进程。

## 已做的模拟验证

- 模拟输出 `rate_limit_event status=rejected` 后进程继续 sleep：runner 能立即识别并杀掉进程。
- 模拟输出 `api_error_status=429` 后进程正常退出 0：runner 仍标记 `rate_limited=True`，单条入口会返回退出码 `75`。
- 模拟正常 `result success`：不会误触发限额熔断。
- 模拟 batch 三并发：其中一条返回 `75` 后，batch 会杀掉其他正在运行的任务，并且不会启动 pending 任务。

## 重新拿到 Claude 后需要做的真实测试

1. 用一个最小 instance 跑 Claude Code 单条修复，确认正常情况下不会误触发熔断。
2. 在真实限额状态下跑单条，确认 `step1.log` 里出现：
   - `rate_limited=True`
   - `completion_source=rate_limit`
   - `rate_limit_reason=...`
3. 确认单条脚本真实返回退出码 `75`。
4. 在真实限额状态下跑 batch，并发至少开 2，确认：
   - 首个限额子进程返回 `75`
   - batch 打印 `[circuit-breaker]`
   - pending 队列不再派发
   - 其他正在运行的 Claude Code 子进程树被终止
5. 检查 `runner_events.jsonl`、`runner_stdout.txt`、`runner_stderr.txt` 是否仍完整落盘，方便后续追溯。

## 注意

由于模拟验证时，本地 Claude 账号已无法登录，因此以上真实测试尚未执行。现在完成的是代码级实现和本地模拟验证。
