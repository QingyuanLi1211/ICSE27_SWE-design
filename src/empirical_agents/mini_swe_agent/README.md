# mini-SWE-agent

从仓库根目录运行。需要本机已安装 Docker，并能使用 benchmark JSONL 中的 Docker image。

## 环境

```powershell
conda create -n minisweagent python=3.12 -y
conda activate minisweagent
pip install -r src\empirical_agents\mini_swe_agent\requirements.txt
```

## 模型路由

mini-swe-agent 不再配置默认模型，单条和 batch 都必须显式传 `--model`。

当前支持的模型别名：

- `MiniMax-M2.7`：Anthropic-compatible，`https://api.minimaxi.com/anthropic`
- `MiMo-V2.5-Pro`：Anthropic-compatible，`https://token-plan-cn.xiaomimimo.com/anthropic`
- `Kimi-K2.6`：Kimi Coding Plan，`https://api.kimi.com/coding/v1`
- `GLM-5.1`：OpenAI-compatible Coding Plan，`https://open.bigmodel.cn/api/coding/paas/v4`
- `deepseek-v4-pro`：OpenAI-compatible，`https://api.deepseek.com`

ARK Coding 已弃用，不再从 `https://ark.cn-beijing.volces.com/api/coding/v3` 接入任何模型。

## 单条示例

```powershell
python src\empirical_agents\mini_swe_agent\mini_swe_agent.py `
  --jsonl-list benchmark\bench_instance\ax\ax_bench_3.jsonl `
  --instance-id facebook__Ax-8378 `
  --bundle-root benchmark\bench_docker_image\ax_images `
  --output-root output_data_batch `
  --model MiMo-V2.5-Pro `
  --api-key "YOUR_KEY" `
  --thinking-effort high
```

## Batch 示例

```powershell
python src\empirical_agents\mini_swe_agent\batch_repair.py `
  --jsonl-list benchmark\bench_instance\ax\ax_bench_3.jsonl `
  --bundle-root benchmark\bench_docker_image\ax_images `
  --output-root output_data_batch `
  --max-parallel 3 `
  --model MiMo-V2.5-Pro `
  --api-key "YOUR_KEY" `
  --thinking-effort high
```
