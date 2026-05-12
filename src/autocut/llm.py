from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


DEFAULT_API_URL = "https://model-api.ecmax.cn/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v3.2"
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    api_url: str = DEFAULT_API_URL
    model: str = DEFAULT_MODEL
    api_key: str = ""
    timeout: float = 120.0


def config_from_env(*, model: str | None = None, api_url: str | None = None) -> LLMConfig:
    return LLMConfig(
        api_url=api_url or os.environ.get("AUTOCUT_LLM_API_URL", DEFAULT_API_URL),
        model=model or os.environ.get("AUTOCUT_LLM_MODEL", DEFAULT_MODEL),
        api_key=os.environ.get("AUTOCUT_LLM_API_KEY", ""),
        timeout=float(os.environ.get("AUTOCUT_LLM_TIMEOUT", "120")),
    )


def generate_ordered_ids(
    prompt: str,
    *,
    stream: bool = False,
    model: str | None = None,
    api_url: str | None = None,
) -> dict[str, Any]:
    content = chat_completion(prompt, stream=stream, model=model, api_url=api_url)
    parsed = parse_model_json(content)
    ordered_ids = parsed.get("ordered_ids")
    if not isinstance(ordered_ids, list):
        raise LLMError("model response missing ordered_ids")
    cleaned_ids = [str(item).strip() for item in ordered_ids if str(item).strip()]
    if not cleaned_ids:
        raise LLMError("model response ordered_ids is empty")
    return {
        "ordered_ids": cleaned_ids,
        "reason": str(parsed.get("reason") or ""),
        "raw_content": content,
    }


def chat_completion(
    prompt: str,
    *,
    stream: bool = False,
    model: str | None = None,
    api_url: str | None = None,
) -> str:
    if stream:
        return "".join(iter_chat_completion(prompt, model=model, api_url=api_url))
    config = config_from_env(model=model, api_url=api_url)
    response = _post_json(config, _payload(prompt, config.model, stream=False))
    choices = response.get("choices") or []
    if not choices:
        raise LLMError("model response missing choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
    if content is None:
        raise LLMError("model response missing content")
    return str(content)


def iter_chat_completion(
    prompt: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
) -> Iterator[str]:
    config = config_from_env(model=model, api_url=api_url)
    payload = _payload(prompt, config.model, stream=True)
    request = _request(config, payload)
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield str(content)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"model api http {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"model api request failed: {exc}") from exc


def parse_model_json(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = JSON_OBJECT_RE.search(text)
        if not match:
            raise LLMError("model response is not JSON")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError("model response JSON is invalid") from exc
    if not isinstance(parsed, dict):
        raise LLMError("model response JSON must be an object")
    return parsed


def _payload(prompt: str, model: str, *, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
        "stream": stream,
    }


def _post_json(config: LLMConfig, payload: dict[str, Any]) -> dict[str, Any]:
    request = _request(config, payload)
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            data = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"model api http {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"model api request failed: {exc}") from exc
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise LLMError("model api response must be a JSON object")
    return parsed


def _request(config: LLMConfig, payload: dict[str, Any]) -> urllib.request.Request:
    if not config.api_key:
        raise LLMError("AUTOCUT_LLM_API_KEY is not set")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(
        config.api_url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
