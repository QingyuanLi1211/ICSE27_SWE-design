"""step1/step2 流程日志工具。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StepLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def log(self, message: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{utc_now_iso()}] {message}\n")

