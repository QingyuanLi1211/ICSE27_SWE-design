# Claude Code

从仓库根目录运行。需要本机已安装 Docker，且能直接使用 benchmark JSONL 里的 Docker image。

## 1. 准备环境
可以全局安装 Claude code CLI，没必要起 Conda 虚拟环境，因为 Python 侧没有额外 pip 依赖；Claude code 通过 CLI 调用。

如果当前环境没有 `npm`，先安装 Node.js：

```powershell
conda install -c conda-forge nodejs -y
```

安装并登录 Claude Code CLI：

```powershell
npm install -g @anthropic-ai/claude-code
claude login
```

## 2. 修复单条 instance

```powershell
python src\empirical_agents\claude_code\claude_code.py --jsonl-list benchmark\zulip\zulip_bench_6.jsonl --instance-id zulip__zulip-6562 --model claude-opus-4-7 --thinking-effort high --agent-timeout-seconds 1800 --agent-edit-mode danger-full-access
```

如果你本机 Claude Code CLI 的模型名不同，直接改 `--model`。

## 3. 输出位置

```text
output_data\claude_code_claudeopus47\repair_results\agent_patch\zulip\zulip__zulip-6562.diff
output_data\claude_code_claudeopus47\repair_results\patch_status\zulip\zulip__zulip-6562.json
output_data\claude_code_claudeopus47\trajectory\zulip\zulip__zulip-6562
output_data\claude_code_claudeopus47\logs\zulip\zulip__zulip-6562\step1.log
```

## 4. Windows 上的隐藏小坑

- 如果 `hi` 这类最小请求能通，但正式 repair 不改仓库、只输出一段文字说明，优先怀疑 Windows 下的 `claude.cmd` 包装层。
- 我们已经在代码里规避这个坑：优先解析到真正可执行的 `claude.exe`，不要依赖 `.cmd` 转发多行大 prompt。
- 有些本地环境里的 `claude.cmd` 还是坏的，脚本指向的 `claude.exe` 可能根本不存在，只剩一个 `claude.exe.old...`。这种情况下最小命令和正式命令的表现会很不一致。
- Claude Code 使用账号登录时，不要走 `--bare`。`--bare` 会禁用 OAuth / keychain 读取，只适合明确用 `ANTHROPIC_API_KEY` 的场景。
- 如果 agent 明明返回 `returncode=0`，但没有修改 worktree，要优先看轨迹里的 `runner_stdout.txt` 和 `runner_events.jsonl`：如果只有纯文本回答、没有真实 `Write/Edit` 工具事件，基本就是 CLI 启动层出问题了。

## 5. 和 Codex CLI 的工程差异

- Codex CLI 在我们的 Windows 使用场景里更丝滑，核心原因是它的 `codex exec` 非交互批处理路径更稳定，直接就是为“长 prompt + 非交互执行 + 改仓库”这种模式设计的。
- Claude Code CLI 也能做同样的事，但在 Windows 上更容易踩 wrapper 坑，尤其是 `.cmd` 转发多行 prompt、登录态读取、`--bare` 与账号登录模式冲突这几类问题。
- 对 Codex 来说，我们主要关心的是 sandbox/edit-mode；对 Claude Code 来说，除了权限，还要额外确认“实际被调用的是可用的 `claude.exe`，不是有问题的 `.cmd` 包装脚本”。
- 简单说：Codex 的坑更偏权限和策略；Claude Code 的坑更偏本机 CLI 安装形态、Windows 包装层和登录模式。
