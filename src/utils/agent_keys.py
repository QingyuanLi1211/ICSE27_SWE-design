"""生成 `<agent_model>` 形式的输出键。"""

from __future__ import annotations

import re


def model_slug(model_name: str) -> str:
    tail = model_name.strip().split("/")[-1]
    normalized_tail = _normalize_model_tail(tail)
    slug = re.sub(r"[^a-z0-9]+", "", normalized_tail.lower())
    if not slug:
        raise ValueError(f"Cannot derive model slug from `{model_name}`.")
    return slug


def agent_model_key(agent_family: str, model_name: str) -> str:
    return f"{agent_family}_{model_slug(model_name)}"


def _normalize_model_tail(model_tail: str) -> str:
    lowered = model_tail.lower()
    if lowered in {"minimax-m2.7-highspeed", "minimaxm27highspeed"}:
        return "MiniMax-M2.7"
    if lowered in {"gemini-3-flash-preview", "gemini3flashpreview"}:
        return "gemini-3.1-pro-preview"
    return model_tail
