from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlunsplit


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
    try:
        lines = _post_json_lines(config, payload)
    except LLMError:
        raise
    for raw_line in lines:
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
    data = _post_bytes_with_retries(config, payload)
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise LLMError("model api response must be a JSON object")
    return parsed


def _post_json_lines(config: LLMConfig, payload: dict[str, Any]) -> list[bytes]:
    return _post_bytes_with_retries(config, payload).splitlines()


def _post_bytes_with_retries(config: LLMConfig, payload: dict[str, Any]) -> bytes:
    errors: list[tuple[str, str]] = []
    for url_label, api_url in _api_urls(config.api_url):
        attempts = [(False, not _llm_ssl_verify_disabled(), False)]
        if api_url.lower().startswith("https://"):
            attempts.append((True, not _llm_ssl_verify_disabled(), True))
            if not _llm_ssl_verify_disabled():
                attempts.append((True, False, True))
        for tls12_max, verify, use_ssl_context in attempts:
            label = url_label
            if tls12_max:
                label += " tls1.2"
            if not verify and api_url.lower().startswith("https://"):
                label += " insecure"
            try:
                return _post_bytes(
                    config,
                    payload,
                    api_url=api_url,
                    tls12_max=tls12_max,
                    verify=verify,
                    use_ssl_context=use_ssl_context,
                )
            except LLMError as exc:
                message = str(exc)
                if not _looks_like_tls_transport_error(message):
                    errors.append((label, message))
                    raise LLMError(_format_transport_errors(errors)) from exc
                errors.append((label, message))
    if len(errors) == 1 and config.api_url.lower().startswith("https://"):
        errors.append(
            (
                "hint",
                "HTTPS TLS failed before HTTP. Fix the LLM gateway TLS, set "
                "AUTOCUT_LLM_API_URL to http://... for trusted intranet use, or set "
                "AUTOCUT_LLM_HTTP_FALLBACK=1 in .env and restart the server.",
            )
        )
    raise LLMError(_format_transport_errors(errors))


def _post_bytes(
    config: LLMConfig,
    payload: dict[str, Any],
    *,
    api_url: str,
    tls12_max: bool = False,
    verify: bool = True,
    use_ssl_context: bool = False,
) -> bytes:
    request = _request(config, payload, api_url=api_url)
    urlopen_kwargs: dict[str, Any] = {"timeout": config.timeout}
    if use_ssl_context and api_url.lower().startswith("https://"):
        urlopen_kwargs["context"] = _ssl_context(verify=verify, tls12_max=tls12_max)
    try:
        with urllib.request.urlopen(request, **urlopen_kwargs) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"model api http {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"model api request failed: {exc}") from exc


def _request(
    config: LLMConfig,
    payload: dict[str, Any],
    *,
    api_url: str | None = None,
) -> urllib.request.Request:
    if not config.api_key:
        raise LLMError("AUTOCUT_LLM_API_KEY is not set")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(
        api_url or config.api_url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )


def _api_urls(api_url: str) -> list[tuple[str, str]]:
    scheme = urlsplit(api_url).scheme.lower() or "api"
    urls = [(scheme, api_url)]
    if _llm_http_fallback_enabled() and api_url.lower().startswith("https://"):
        parts = urlsplit(api_url)
        urls.append(
            (
                "http fallback",
                urlunsplit(("http", parts.netloc, parts.path, parts.query, parts.fragment)),
            )
        )
    return urls


def _ssl_context(*, verify: bool = True, tls12_max: bool = False) -> ssl.SSLContext:
    context = ssl.create_default_context() if verify else ssl._create_unverified_context()
    ignore_eof = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    if ignore_eof:
        context.options |= ignore_eof
    if tls12_max and hasattr(ssl, "TLSVersion"):
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


def _looks_like_tls_transport_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "ssl/tls",
            "tls/ssl",
            "ssl:",
            "handshake",
            "tls connection",
            "connection has been closed",
            "closed (eof)",
            "unexpected eof",
            "unexpected_eof",
            "eof occurred in violation of protocol",
        )
    )


def _format_transport_errors(errors: list[tuple[str, str]]) -> str:
    details = " | ".join(
        f"{name}: {message[:240].replace(chr(10), ' ')}" for name, message in errors
    )
    return f"model api request failed after transport retries: {details}"


def _llm_ssl_verify_disabled() -> bool:
    return os.environ.get("AUTOCUT_LLM_SSL_VERIFY", "").strip().lower() in {
        "0",
        "false",
        "no",
    }


def _llm_http_fallback_enabled() -> bool:
    return os.environ.get("AUTOCUT_LLM_HTTP_FALLBACK", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
