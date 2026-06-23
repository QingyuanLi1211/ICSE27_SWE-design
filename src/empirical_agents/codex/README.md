# Codex

从仓库根目录运行。需要本机已安装 Docker、已能使用 benchmark JSONL 中的 Docker image，并且已安装 Codex CLI。

## 1. 准备环境

可以全局安装 Codex CLI，没必要起 Conda 虚拟环境，因为 Python 侧没有额外 pip 依赖；Codex 通过 CLI 调用。
```powershell
codex --version
codex login
```

## 2. 修复单条 instance

```powershell
python src\empirical_agents\codex\codex.py --jsonl-list benchmark\zulip\zulip_bench_6.jsonl --instance-id zulip__zulip-6562 --model gpt-5.4 --thinking-effort high --agent-timeout-seconds 1800 --agent-edit-mode danger-full-access
```

## 3. 输出位置

```text
output_data\codex_gpt54\repair_results\agent_patch\zulip\zulip__zulip-6562.diff
output_data\codex_gpt54\repair_results\patch_status\zulip\zulip__zulip-6562.json
output_data\codex_gpt54\trajectory\zulip\zulip__zulip-6562
output_data\codex_gpt54\logs\zulip\zulip__zulip-6562\step1.log
```
