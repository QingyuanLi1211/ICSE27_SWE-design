from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


def _normalize_message_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            return content
        item_type = item.get("type")
        if item_type in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
            continue
        return content
    return "\n".join(text_parts)


def _sanitize_chat_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("role", "name", "tool_call_id", "tool_calls", "refusal"):
            if key in message:
                item[key] = message[key]
        if "content" in message:
            item["content"] = _normalize_message_content(message["content"])
        sanitized.append(_drop_none(item))
    return sanitized


def sanitize_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": _sanitize_chat_messages(payload.get("messages")),
    }
    passthrough = (
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "temperature",
        "top_p",
        "max_completion_tokens",
        "max_tokens",
        "reasoning_effort",
        "prompt_cache_retention",
        "stream",
        "stream_options",
        "response_format",
        "n",
        "stop",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "user",
    )
    for key in passthrough:
        if key in payload:
            out[key] = payload[key]
    return _drop_none(out)


def sanitize_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "input": payload.get("input"),
    }
    passthrough = (
        "instructions",
        "reasoning",
        "tools",
        "tool_choice",
        "text",
        "max_output_tokens",
        "parallel_tool_calls",
        "previous_response_id",
        "store",
        "temperature",
        "top_p",
        "truncation",
        "prompt_cache_retention",
        "metadata",
    )
    for key in passthrough:
        if key in payload:
            out[key] = payload[key]
    return _drop_none(out)


def translate_chat_to_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_chat_payload(payload)
    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in sanitized.get("messages", []):
        role = message.get("role")
        if role in {"system", "developer"}:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                instructions_parts.append(content.strip())
            continue
        item: dict[str, Any] = {"role": role}
        content = message.get("content")
        if isinstance(content, str):
            item["content"] = content
        elif content is not None:
            item["content"] = content
        if "tool_call_id" in message:
            item["tool_call_id"] = message["tool_call_id"]
        if "tool_calls" in message:
            item["tool_calls"] = message["tool_calls"]
        input_items.append(item)

    out: dict[str, Any] = {
        "model": sanitized.get("model"),
        "input": input_items,
    }
    if instructions_parts:
        out["instructions"] = "\n\n".join(instructions_parts)
    if "reasoning_effort" in sanitized:
        out["reasoning"] = {"effort": sanitized["reasoning_effort"]}
    if "prompt_cache_retention" in sanitized:
        out["prompt_cache_retention"] = sanitized["prompt_cache_retention"]
    if "parallel_tool_calls" in sanitized:
        out["parallel_tool_calls"] = sanitized["parallel_tool_calls"]
    if "temperature" in sanitized:
        out["temperature"] = sanitized["temperature"]
    if "top_p" in sanitized:
        out["top_p"] = sanitized["top_p"]
    if "stream" in sanitized:
        out["stream"] = sanitized["stream"]
    if "tools" in sanitized:
        out["tools"] = [_translate_chat_tool_to_responses(tool) for tool in sanitized["tools"]]
    if "tool_choice" in sanitized:
        out["tool_choice"] = _translate_chat_tool_choice_to_responses(sanitized["tool_choice"])
    if "max_completion_tokens" in sanitized:
        out["max_output_tokens"] = sanitized["max_completion_tokens"]
    elif "max_tokens" in sanitized:
        out["max_output_tokens"] = sanitized["max_tokens"]
    return _drop_none(out)


def _translate_chat_tool_to_responses(tool: Any) -> Any:
    if not isinstance(tool, dict):
        return tool
    if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
        return tool
    function = tool["function"]
    out = {
        "type": "function",
        "name": function.get("name"),
        "description": function.get("description"),
        "parameters": function.get("parameters"),
    }
    if "strict" in function:
        out["strict"] = function["strict"]
    return _drop_none(out)


def _translate_chat_tool_choice_to_responses(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return choice
    if choice.get("type") != "function" or not isinstance(choice.get("function"), dict):
        return choice
    function = choice["function"]
    return _drop_none({"type": "function", "name": function.get("name")})


def translate_responses_to_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": ""}
    tool_calls: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            message["role"] = item.get("role", "assistant")
            for block in item.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "output_text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
        elif item_type == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments", ""),
                    },
                }
            )

    message["content"] = "\n".join(part for part in text_parts if part)
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish_reason = "tool_calls" if tool_calls else "stop"
    usage = payload.get("usage", {}) if isinstance(payload.get("usage"), dict) else {}
    return _drop_none(
        {
            "id": payload.get("id", f"chatcmpl_shim_{int(time.time())}"),
            "object": "chat.completion",
            "created": int(payload.get("created_at", time.time())),
            "model": payload.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
    )


class _ShimServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        api_key: str,
        upstream_base_url: str,
        *,
        max_retries: int,
        retry_backoff_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        log_dir_env = os.environ.get("SDB_SUB2API_SHIM_LOG_DIR", "").strip()
        self.log_dir = Path(log_dir_env) if log_dir_env else None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(server_address, _ShimRequestHandler)


class _ShimRequestHandler(BaseHTTPRequestHandler):
    server: _ShimServer

    def do_POST(self) -> None:  # noqa: N802
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            if self.path == "/v1/chat/completions":
                sanitized = translate_chat_to_responses_payload(payload)
                upstream_path = "/responses"
            elif self.path == "/v1/responses":
                sanitized = sanitize_responses_payload(payload)
                upstream_path = "/responses"
            else:
                self._write_json(404, {"error": {"message": f"Unsupported path: {self.path}"}})
                return

            self._log_payload("raw", payload)
            self._log_payload("sanitized", sanitized)
            status, headers, response_body = self._forward_with_retries(upstream_path, sanitized)
            if self.path == "/v1/chat/completions":
                upstream_json = json.loads(response_body.decode("utf-8"))
                body = json.dumps(
                    translate_responses_to_chat_payload(upstream_json),
                    ensure_ascii=False,
                ).encode("utf-8")
                content_type = "application/json"
            else:
                body = response_body
                content_type = headers.get("Content-Type", "application/json")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            self._write_json(500, {"error": {"message": str(exc)}})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _log_payload(self, prefix: str, payload: dict[str, Any]) -> None:
        if self.server.log_dir is None:
            return
        target = self.server.log_dir / f"{prefix}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _forward_with_retries(
        self,
        upstream_path: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload).encode("utf-8")
        last_http_error: urllib.error.HTTPError | None = None
        last_url_error: urllib.error.URLError | None = None
        for attempt in range(self.server.max_retries + 1):
            request = urllib.request.Request(
                url=f"{self.server.upstream_base_url}{upstream_path}",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.server.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    return response.status, dict(response.headers.items()), response.read()
            except urllib.error.HTTPError as exc:
                last_http_error = exc
                retryable = exc.code in {429, 500, 502, 503, 504}
                if attempt >= self.server.max_retries or not retryable:
                    raise
            except urllib.error.URLError as exc:
                last_url_error = exc
                if attempt >= self.server.max_retries:
                    raise
            time.sleep(self.server.retry_backoff_seconds * (attempt + 1))
        if last_http_error is not None:
            raise last_http_error
        if last_url_error is not None:
            raise last_url_error
        raise RuntimeError("upstream forwarding failed without an exception")


class Sub2apiOpenAIShim(AbstractContextManager["Sub2apiOpenAIShim"]):
    def __init__(
        self,
        *,
        api_key: str,
        upstream_base_url: str = "http://127.0.0.1:8080/v1",
        bind_host: str = "127.0.0.1",
        public_host: str = "host.docker.internal",
        max_retries: int = 4,
        retry_backoff_seconds: float = 1.5,
    ) -> None:
        self.api_key = api_key
        self.upstream_base_url = upstream_base_url
        self.bind_host = bind_host
        self.public_host = public_host
        self.port = _find_free_port(bind_host)
        self._server = _ShimServer(
            (bind_host, self.port),
            api_key=api_key,
            upstream_base_url=upstream_base_url,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def local_base_url(self) -> str:
        return f"http://{self.bind_host}:{self.port}/v1"

    @property
    def container_base_url(self) -> str:
        return f"http://{self.public_host}:{self.port}/v1"

    def __enter__(self) -> "Sub2apiOpenAIShim":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen(1)
        return int(sock.getsockname()[1])
