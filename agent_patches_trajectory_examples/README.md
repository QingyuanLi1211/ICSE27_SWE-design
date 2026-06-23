# Agent Patch, Trajectory, and Evaluation Examples

This directory contains one sampled example for each evaluated agent. All samples use the same instance, `facebook__buck-1117`, to make the examples easy to compare across agents.

Each example includes:

- `patch/`: the submitted agent patch;
- `trajectory/`: one compact trajectory artifact for that run;
- `eval_result/`: the corresponding evaluation result JSON.

Trajectory files are intentionally kept small. Codex and Claude Code examples include only `runner_events.jsonl`; Mini-SWE-Agent and Live-SWE-Agent examples include only `mini_run.traj.json`.

These files are examples only. Additional trajectories and evaluation outputs will be distributed through external archival storage.
