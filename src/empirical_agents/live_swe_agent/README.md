# Live-SWE-agent

从仓库根目录运行。需要本机已安装 Docker、已能使用 benchmark JSONL 中的 Docker image。这里复用 `org_src` 中打包进来的 Live-SWE-agent/mini-SWE-agent 源码。

## 1. 准备环境

```powershell
conda create -n livesweagent python=3.12 -y
conda activate livesweagent
pip install -r src\empirical_agents\live_swe_agent\requirements.txt
```

## 2. Gemini

默认 Gemini base URL 是 `https://bitexingai.com/v1`。
如果你显式传入 `http://127.0.0.1:8080/v1beta` 这类地址，才会走本地 native Gemini 路径。

## 3. 修复单条 instance

```powershell
$env:OPENAI_API_KEY="YOUR_GEMINI_KEY"
python src\empirical_agents\live_swe_agent\live_swe_agent.py --jsonl-list benchmark\zulip\zulip_bench_6.jsonl --instance-id zulip__zulip-6562 --model gemini-3-pro-preview --api-base https://bitexingai.com/v1 --api-key-env OPENAI_API_KEY --thinking-effort high --agent-timeout-seconds 1800
```

## 4. 输出位置

```text
output_data\live_swe_agent_gemini3propreview\repair_results\agent_patch\zulip\zulip__zulip-6562.diff
output_data\live_swe_agent_gemini3propreview\repair_results\patch_status\zulip\zulip__zulip-6562.json
output_data\live_swe_agent_gemini3propreview\trajectory\zulip\zulip__zulip-6562
output_data\live_swe_agent_gemini3propreview\logs\zulip\zulip__zulip-6562\step1.log
```
