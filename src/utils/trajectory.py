"""生成统一的 normalized_traj.jsonl。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .benchmark import BenchmarkRecord
from .diffing import extract_edited_paths_from_patch
from .logs import utc_now_iso


def normalize_trajectory(
    *,
    agent_name: str,
    record: BenchmarkRecord,
    raw_paths: dict[str, Path | None],
    worktree_root: Path | None,
    prompt_path: Path,
    output_path: Path,
    repair_status: dict,
    patch_text: str,
    patch_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    events = []
    events.append(
        _base_event(
            seq=1,
            record=record,
            agent_name=agent_name,
            event_type="session_start",
            source_format="harness",
            source_file=str(prompt_path),
            source_event_id=None,
            action_name=None,
            action_status="info",
            content="repair session started",
            payload={
                "source_jsonl": str(record.source_jsonl),
                "docker_image": record.docker_image,
                "worktree_root": str(worktree_root) if worktree_root is not None else None,
            },
            edited_paths=None,
        )
    )

    seq = 2
    for event in _iter_agent_events(agent_name=agent_name, raw_paths=raw_paths, record=record):
        event["seq"] = seq
        seq += 1
        events.append(event)

    for edited_path in extract_edited_paths_from_patch(patch_text):
        events.append(
            _base_event(
                seq=seq,
                record=record,
                agent_name=agent_name,
                event_type="file_edit",
                source_format="diff",
                source_file=str(patch_path),
                source_event_id=None,
                action_name="patch_diff",
                action_status="info",
                content=edited_path,
                payload=None,
                edited_paths=[edited_path],
            )
        )
        seq += 1

    events.append(
        _base_event(
            seq=seq,
            record=record,
            agent_name=agent_name,
            event_type="session_end",
            source_format="harness",
            source_file=str(output_path),
            source_event_id=None,
            action_name=None,
            action_status="succeeded" if repair_status["agent_patch_generated"] else "failed",
            content="repair finished successfully" if repair_status["agent_patch_generated"] else "repair finished with failure",
            payload=_session_end_payload(repair_status=repair_status, patch_path=patch_path),
            edited_paths=extract_edited_paths_from_patch(patch_text) or None,
        )
    )

    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _iter_agent_events(*, agent_name: str, raw_paths: dict[str, Path | None], record: BenchmarkRecord) -> Iterable[dict[str, Any]]:
    agent_family = _agent_family_from_key(agent_name)
    if agent_family == "codex":
        yield from _iter_codex_events(raw_paths.get("events_path"), record, agent_name)
        return
    if agent_family == "trae":
        yield from _iter_codex_events(raw_paths.get("events_path"), record, agent_name)
        return
    if agent_family in {"mini_swe_agent", "live_swe_agent"}:
        yield from _iter_mini_events(raw_paths.get("trajectory_path"), record, agent_name, agent_family)
        return
    if agent_family == "openhands":
        yield from _iter_openhands_events(raw_paths.get("events_path"), record, agent_name)
        return
    if agent_family == "claude_code":
        yield from _iter_claude_code_events(raw_paths.get("events_path"), record, agent_name)
        return


def _agent_family_from_key(agent_name: str) -> str:
    if agent_name.startswith("codex"):
        return "codex"
    if agent_name.startswith("trae"):
        return "trae"
    if agent_name.startswith("mini_swe_agent"):
        return "mini_swe_agent"
    if agent_name.startswith("live_swe_agent"):
        return "live_swe_agent"
    if agent_name.startswith("openhands"):
        return "openhands"
    if agent_name.startswith("claude_code"):
        return "claude_code"
    return agent_name


def _iter_codex_events(path: Path | None, record: BenchmarkRecord, agent_name: str) -> Iterable[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    events = []
    for line_index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_type = str(raw.get("type") or "")
        item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        source_event_id = item.get("id") or line_index
        if item_type == "agent_message":
            events.append(
                _base_event(
                    seq=0,
                    record=record,
                    agent_name=agent_name,
                    event_type="agent_message",
                    source_format="codex.runner_events.jsonl",
                    source_file=str(path),
                    source_event_id=source_event_id,
                    action_name=None,
                    action_status="info",
                    content=_trim_text(str(item.get("text") or "")),
                    payload={"raw_type": raw_type},
                    edited_paths=None,
                )
            )
        elif item_type == "command_execution":
            if raw_type == "item.started":
                events.append(
                    _base_event(
                        seq=0,
                        record=record,
                        agent_name=agent_name,
                        event_type="tool_call",
                        source_format="codex.runner_events.jsonl",
                        source_file=str(path),
                        source_event_id=source_event_id,
                        action_name="command_execution",
                        action_status="started",
                        content=_trim_text(str(item.get("command") or "")),
                        payload=None,
                        edited_paths=None,
                    )
                )
            else:
                exit_code = item.get("exit_code")
                status = "succeeded" if exit_code == 0 else "failed"
                events.append(
                    _base_event(
                        seq=0,
                        record=record,
                        agent_name=agent_name,
                        event_type="tool_result",
                        source_format="codex.runner_events.jsonl",
                        source_file=str(path),
                        source_event_id=source_event_id,
                        action_name="command_execution",
                        action_status=status,
                        content=_trim_text(str(item.get("aggregated_output") or "")),
                        payload={"exit_code": exit_code, "command": item.get("command")},
                        edited_paths=None,
                    )
                )
        elif item_type in {"reasoning", "todo_list"} or raw_type in {"thread.started", "turn.started", "turn.completed"}:
            events.append(
                _base_event(
                    seq=0,
                    record=record,
                    agent_name=agent_name,
                    event_type="status",
                    source_format="codex.runner_events.jsonl",
                    source_file=str(path),
                    source_event_id=source_event_id,
                    action_name=item_type or raw_type,
                    action_status="info",
                    content=_trim_text(str(item.get("text") or raw_type)),
                    payload={"raw_type": raw_type},
                    edited_paths=None,
                )
            )
    return events


def _iter_mini_events(
    path: Path | None,
    record: BenchmarkRecord,
    agent_name: str,
    agent_family: str = "mini_swe_agent",
) -> Iterable[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return []

    events = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        source_event_id = index
        if role == "assistant":
            content = message.get("reasoning_content") or message.get("content")
            if content:
                events.append(
                    _base_event(
                        seq=0,
                        record=record,
                        agent_name=agent_name,
                        event_type="agent_message",
                        source_format=f"{agent_family}.traj.json",
                        source_file=str(path),
                        source_event_id=source_event_id,
                        action_name=None,
                        action_status="info",
                        content=_trim_text(_coerce_content(content)),
                        payload=None,
                        edited_paths=None,
                    )
                )
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") if isinstance(tool_call, dict) else {}
                events.append(
                    _base_event(
                        seq=0,
                        record=record,
                        agent_name=agent_name,
                        event_type="tool_call",
                        source_format=f"{agent_family}.traj.json",
                        source_file=str(path),
                        source_event_id=tool_call.get("id") or source_event_id,
                        action_name=str(function.get("name") or "tool_call"),
                        action_status="started",
                        content=_trim_text(str(function.get("arguments") or "")),
                        payload=None,
                        edited_paths=None,
                    )
                )
        elif role in {"tool", "function"}:
            events.append(
                _base_event(
                    seq=0,
                    record=record,
                    agent_name=agent_name,
                    event_type="tool_result",
                    source_format=f"{agent_family}.traj.json",
                    source_file=str(path),
                    source_event_id=source_event_id,
                    action_name=str(message.get("name") or "tool"),
                    action_status="succeeded",
                    content=_trim_text(_coerce_content(message.get("content"))),
                    payload=None,
                    edited_paths=None,
                )
            )
    return events


def _iter_openhands_events(path: Path | None, record: BenchmarkRecord, agent_name: str) -> Iterable[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    events = []
    for line_index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        source = str(raw.get("source") or "")
        source_event_id = raw.get("id") or line_index
        if source == "agent" and isinstance(raw.get("action"), dict):
            action = raw["action"]
            action_name = str(raw.get("tool_name") or action.get("kind") or "action")
            if action.get("kind") == "FileEditorAction":
                edited_path = action.get("path")
                events.append(
                    _base_event(
                        seq=0,
                        record=record,
                        agent_name=agent_name,
                        event_type="file_edit",
                        source_format="openhands.events.jsonl",
                        source_file=str(path),
                        source_event_id=source_event_id,
                        action_name=action_name,
                        action_status="started",
                        content=_trim_text(str(action.get("command") or edited_path or "")),
                        payload={"path": edited_path, "command": action.get("command")},
                        edited_paths=[edited_path] if isinstance(edited_path, str) and edited_path else None,
                    )
                )
            else:
                events.append(
                    _base_event(
                        seq=0,
                        record=record,
                        agent_name=agent_name,
                        event_type="tool_call",
                        source_format="openhands.events.jsonl",
                        source_file=str(path),
                        source_event_id=source_event_id,
                        action_name=action_name,
                        action_status="started",
                        content=_trim_text(_coerce_content(action.get("command") or action)),
                        payload=None,
                        edited_paths=None,
                    )
                )
        elif source == "environment" and isinstance(raw.get("observation"), dict):
            observation = raw["observation"]
            action_name = str(raw.get("tool_name") or observation.get("kind") or "observation")
            events.append(
                _base_event(
                    seq=0,
                    record=record,
                    agent_name=agent_name,
                    event_type="tool_result",
                    source_format="openhands.events.jsonl",
                    source_file=str(path),
                    source_event_id=source_event_id,
                    action_name=action_name,
                    action_status="failed" if observation.get("is_error") else "succeeded",
                    content=_trim_text(_coerce_content(observation.get("content"))),
                    payload={"command": observation.get("command"), "path": observation.get("path")},
                    edited_paths=None,
                )
            )
        elif source == "agent":
            agent_text = raw.get("reasoning_content") or raw.get("content") or raw.get("system_prompt", {}).get("text")
            if agent_text:
                events.append(
                    _base_event(
                        seq=0,
                        record=record,
                        agent_name=agent_name,
                        event_type="agent_message",
                        source_format="openhands.events.jsonl",
                        source_file=str(path),
                        source_event_id=source_event_id,
                        action_name=None,
                        action_status="info",
                        content=_trim_text(_coerce_content(agent_text)),
                        payload=None,
                        edited_paths=None,
                    )
                )
        else:
            events.append(
                _base_event(
                    seq=0,
                    record=record,
                    agent_name=agent_name,
                    event_type="status",
                    source_format="openhands.events.jsonl",
                    source_file=str(path),
                    source_event_id=source_event_id,
                    action_name=str(raw.get("kind") or raw.get("source") or "status"),
                    action_status="info",
                    content=_trim_text(_coerce_content(raw.get("key") or raw.get("source") or "status")),
                    payload=None,
                    edited_paths=None,
                )
            )
    return events


def _iter_claude_code_events(path: Path | None, record: BenchmarkRecord, agent_name: str) -> Iterable[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    events = []
    for line_index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_type = str(raw.get("type") or "")
        source_event_id = raw.get("message", {}).get("id") if isinstance(raw.get("message"), dict) else line_index
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        content_blocks = message.get("content") if isinstance(message.get("content"), list) else []

        if raw_type == "assistant":
            for block_index, block in enumerate(content_blocks):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type == "text":
                    events.append(
                        _base_event(
                            seq=0,
                            record=record,
                            agent_name=agent_name,
                            event_type="agent_message",
                            source_format="claude_code.stream_json",
                            source_file=str(path),
                            source_event_id=f"{source_event_id}:{block_index}",
                            action_name=None,
                            action_status="info",
                            content=_trim_text(_coerce_content(block.get("text"))),
                            payload={"raw_type": raw_type},
                            edited_paths=None,
                        )
                    )
                elif block_type == "tool_use":
                    events.append(
                        _base_event(
                            seq=0,
                            record=record,
                            agent_name=agent_name,
                            event_type="tool_call",
                            source_format="claude_code.stream_json",
                            source_file=str(path),
                            source_event_id=block.get("id") or f"{source_event_id}:{block_index}",
                            action_name=str(block.get("name") or "tool_use"),
                            action_status="started",
                            content=_trim_text(_coerce_content(block.get("input"))),
                            payload=None,
                            edited_paths=None,
                        )
                    )
        elif raw_type == "user":
            for block_index, block in enumerate(content_blocks):
                if not isinstance(block, dict):
                    continue
                if str(block.get("type") or "") == "tool_result":
                    events.append(
                        _base_event(
                            seq=0,
                            record=record,
                            agent_name=agent_name,
                            event_type="tool_result",
                            source_format="claude_code.stream_json",
                            source_file=str(path),
                            source_event_id=block.get("tool_use_id") or f"{source_event_id}:{block_index}",
                            action_name="tool_result",
                            action_status="failed" if block.get("is_error") else "succeeded",
                            content=_trim_text(_coerce_content(block.get("content"))),
                            payload=None,
                            edited_paths=None,
                        )
                    )
        elif raw_type == "result":
            events.append(
                _base_event(
                    seq=0,
                    record=record,
                    agent_name=agent_name,
                    event_type="status",
                    source_format="claude_code.stream_json",
                    source_file=str(path),
                    source_event_id=source_event_id,
                    action_name=str(raw.get("subtype") or "result"),
                    action_status="succeeded" if raw.get("subtype") in {None, "success"} else "failed",
                    content=_trim_text(_coerce_content(raw.get("result") or raw.get("subtype") or "result")),
                    payload={
                        "duration_ms": raw.get("duration_ms"),
                        "total_cost_usd": raw.get("total_cost_usd"),
                        "num_turns": raw.get("num_turns"),
                    },
                    edited_paths=None,
                )
            )
        else:
            events.append(
                _base_event(
                    seq=0,
                    record=record,
                    agent_name=agent_name,
                    event_type="status",
                    source_format="claude_code.stream_json",
                    source_file=str(path),
                    source_event_id=source_event_id,
                    action_name=raw_type or "status",
                    action_status="info",
                    content=_trim_text(_coerce_content(raw.get("subtype") or raw_type or "status")),
                    payload=None,
                    edited_paths=None,
                )
            )
    return events


def _session_end_payload(*, repair_status: dict, patch_path: Path) -> dict[str, Any]:
    payload = {
        "image_available": repair_status["image_available"],
        "workspace_prepared": repair_status["workspace_prepared"],
        "workspace_writable": repair_status["workspace_writable"],
        "agent_run_completed": repair_status["agent_run_completed"],
        "agent_modified_worktree": repair_status["agent_modified_worktree"],
        "agent_patch_generated": repair_status["agent_patch_generated"],
    }
    if repair_status["agent_patch_generated"]:
        payload["agent_patch_path"] = str(patch_path)
    else:
        payload["failure_stage"] = _infer_failure_stage(repair_status)
    return payload


def _infer_failure_stage(repair_status: dict) -> str:
    if repair_status["image_available"] is False:
        return "build_worktree"
    if repair_status["workspace_prepared"] is False:
        return "build_worktree"
    if repair_status["workspace_writable"] is False:
        return "run_agent_fixing"
    if repair_status["agent_run_completed"] is False:
        return "run_agent_fixing"
    if repair_status["agent_modified_worktree"] is False:
        return "diff_agent_patch"
    return "diff_agent_patch"


def _base_event(
    *,
    seq: int,
    record: BenchmarkRecord,
    agent_name: str,
    event_type: str,
    source_format: str,
    source_file: str,
    source_event_id: str | int | None,
    action_name: str | None,
    action_status: str | None,
    content: str | None,
    payload: dict[str, Any] | None,
    edited_paths: list[str] | None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "timestamp": utc_now_iso(),
        "type": event_type,
        "agent_name": agent_name,
        "instance_id": record.instance_id,
        "repo": record.repo_key,
        "source_format": source_format,
        "source_file": source_file,
        "source_event_id": source_event_id,
        "action_name": action_name,
        "action_status": action_status,
        "content": content,
        "payload": payload,
        "edited_paths": edited_paths,
    }


def _trim_text(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _coerce_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
