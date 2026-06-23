import json
import os
import re
import time
from typing import Any

import requests
from jinja2 import StrictUndefined, Template
from pydantic import BaseModel

from minisweagent.exceptions import FormatError
from minisweagent.models.utils.cache_control import set_cache_control
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content
from minisweagent.utils.serialize import recursive_merge


_BASH_BLOCK_RE = re.compile(r"```bash\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class Sub2apiGeminiNativeModelConfig(BaseModel):
    model_name: str
    model_kwargs: dict[str, Any] = {}
    format_error_template: str = "{{ error }}"
    observation_template: str = (
        "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
        "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
    )
    multimodal_regex: str = ""
    cost_tracking: str = "ignore_errors"


class Sub2apiGeminiNativeModel:
    def __init__(self, *, config_class: type = Sub2apiGeminiNativeModelConfig, **kwargs):
        self.config = config_class(**kwargs)

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        payload = self._build_payload(messages)
        response = requests.post(
            self._endpoint_url(),
            headers=self._headers(),
            json=payload,
            timeout=self.config.model_kwargs.get("timeout", 180),
        )
        response.raise_for_status()
        body = response.json()
        content = self._extract_response_text(body)
        actions = self._parse_actions(content)
        return {
            "role": "assistant",
            "content": content,
            "extra": {
                "actions": actions,
                "response": body,
                "cost": 0.0,
                "timestamp": time.time(),
            },
        }

    def format_message(self, **kwargs) -> dict:
        return expand_multimodal_content(kwargs, pattern=self.config.multimodal_regex)

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        results = []
        padded_outputs = outputs + [{"output": "", "returncode": -1, "exception_info": "action was not executed"}] * (
            len(actions) - len(outputs)
        )
        for action, output in zip(actions, padded_outputs):
            content = Template(self.config.observation_template, undefined=StrictUndefined).render(
                output=output, **(template_vars or {})
            )
            msg = {
                "role": "user",
                "content": content,
                "extra": {
                    "raw_output": output.get("output", ""),
                    "returncode": output.get("returncode"),
                    "timestamp": time.time(),
                    "exception_info": output.get("exception_info"),
                    **output.get("extra", {}),
                },
            }
            if self.config.multimodal_regex:
                msg = expand_multimodal_content(msg, pattern=self.config.multimodal_regex)
            results.append(msg)
        return results

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(self.config.model_dump(), kwargs)

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": self.config.model_dump(mode="json"),
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }

    def _endpoint_url(self) -> str:
        api_base = str(self.config.model_kwargs.get("api_base", "")).rstrip("/")
        if not api_base:
            raise RuntimeError("Missing Gemini api_base for Sub2apiGeminiNativeModel.")
        model_name = self.config.model_name.split("/", 1)[-1]
        return f"{api_base}/models/{model_name}:generateContent"

    def _headers(self) -> dict[str, str]:
        api_key = (
            self.config.model_kwargs.get("api_key")
            or os.environ.get(str(self.config.model_kwargs.get("api_key_env", "GEMINI_API_KEY")), "")
        )
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY for Sub2apiGeminiNativeModel.")
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = self._stringify_content(message.get("content", ""))
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
                continue
            contents.append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": content}],
                }
            )
        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        generation_config = {}
        if "temperature" in self.config.model_kwargs:
            generation_config["temperature"] = self.config.model_kwargs["temperature"]
        if "max_tokens" in self.config.model_kwargs:
            generation_config["maxOutputTokens"] = self.config.model_kwargs["max_tokens"]
        if generation_config:
            payload["generationConfig"] = generation_config
        return payload

    def _extract_response_text(self, body: dict[str, Any]) -> str:
        parts = (((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        texts = [part.get("text", "") for part in parts if part.get("text") and not part.get("thought")]
        return "\n".join(texts).strip()

    def _parse_actions(self, content: str) -> list[dict[str, Any]]:
        matches = _BASH_BLOCK_RE.findall(content)
        if len(matches) != 1:
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(self.config.format_error_template, undefined=StrictUndefined).render(
                        error=f"Expected exactly one bash code block, found {len(matches)}.",
                        actions=[],
                    ),
                    "extra": {"interrupt_type": "FormatError"},
                }
            )
        command = matches[0].strip()
        if not command:
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(self.config.format_error_template, undefined=StrictUndefined).render(
                        error="The bash code block was empty.",
                        actions=[],
                    ),
                    "extra": {"interrupt_type": "FormatError"},
                }
            )
        return [{"command": command}]

    @staticmethod
    def _stringify_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part)
        return str(content)
