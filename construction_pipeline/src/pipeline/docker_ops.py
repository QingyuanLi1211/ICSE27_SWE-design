"""Deterministic helpers for Docker-related command rendering."""

from __future__ import annotations


def render_docker_exec_command(template: str, inner_command: str) -> str:
    """Render a docker exec/build command from a template containing ``__CMD__``."""
    if "__CMD__" not in template:
        raise ValueError("docker_exec_template must contain '__CMD__'")
    return template.replace("__CMD__", inner_command)

