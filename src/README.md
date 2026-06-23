# Docker-Based Agent Evaluation Harness

This directory contains the code used to run and evaluate SWE agents in DART.

- `eval/` contains the shared evaluation entry points.
- `utils/` contains common utilities for benchmark loading, Docker execution, patch handling, trajectories, and output layout.
- `empirical_agents/` contains the wrappers used for the evaluated agents.

The harness expects DART-style JSONL instances and either local Docker images or image bundles corresponding to each instance.
