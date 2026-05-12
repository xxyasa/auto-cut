"""OSS / URL 视频源接入（提案 §2）。

设计要点：
- 三种输入类型：`oss` / `url` / `upload`；upload 不走下载流程。
- URL 校验：scheme（仅 https）+ ext 白名单 + host allowlist + DNS 解析私有 IP 拦截。
- 下载策略：
  - 默认 `httpx.stream("GET", url, follow_redirects=False)` 流式落盘。
  - HEAD 预检 Content-Length > 2GB → 400；边写边累计字节，超过 2GB 立即中止。
  - 任何 3xx → 400 redirect_not_allowed（业务粘最终直链）。
- minio fallback：仅当 `oss://` 输入且 HTTP 路径 401/403 时尝试一次 SDK。
- 所有失败抛 OssError，路由层转 400 / 413。

关于 SSRF 的 DNS hook 测试性：
- `_check_ip_safe(host)` 接收 host 字符串，内部走 `socket.getaddrinfo`；测试用 monkeypatch 替换。

公共接口：
    resolve_video_source(source, dest_dir, *, on_progress=None) -> Path
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# 默认与 collect-douyin 对齐的环境变量
ENV_OSS_ENDPOINT = "OSS_ENDPOINT"
ENV_OSS_USE_SSL = "OSS_USE_SSL"
ENV_OSS_ACCESS_KEY = "OSS_ACCESS_KEY"
ENV_OSS_SECRET_KEY = "OSS_SECRET_KEY"
ENV_OSS_BUCKET = "OSS_BUCKET_NAME"
ENV_URL_ALLOWLIST = "AUTOCUT_URL_ALLOWLIST"

DEFAULT_OSS_ENDPOINT = "oss.ecmax.cn"
DEFAULT_OSS_BUCKET = "rpa"
DEFAULT_URL_ALLOWLIST = "oss.ecmax.cn"

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".flv", ".ts"}
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
HTTP_CONNECT_TIMEOUT = 10.0
HTTP_READ_TIMEOUT = 300.0


# ---------- 异常 ----------


class OssError(RuntimeError):
    """视频源相关基础异常。"""


class InvalidSource(OssError):
    """source 字段格式错误（→ 422/400）。"""


class SchemeNotAllowed(OssError):
    """非 https / 非允许 scheme（→ 400 scheme_not_allowed）。"""


class ExtensionNotAllowed(OssError):
    """非视频白名单扩展名（→ 400 extension_not_allowed）。"""


class HostNotAllowed(OssError):
    """host 不在 AUTOCUT_URL_ALLOWLIST 中（→ 400 host_not_allowed）。"""


class PrivateAddressBlocked(OssError):
    """DNS 解析到私有 IP（→ 400 private_address_blocked）。"""


class RedirectNotAllowed(OssError):
    """远端返回 3xx（→ 400 redirect_not_allowed）。"""


class TooLarge(OssError):
    """声明或实际下载超过 2GB（→ 400/413 too_large）。"""


class UpstreamError(OssError):
    """远端 4xx/5xx（→ 400 upstream_status=N）。"""


# ---------- 工具 ----------


def _allowlist() -> set[str]:
    raw = os.environ.get(ENV_URL_ALLOWLIST, DEFAULT_URL_ALLOWLIST)
    return {item.strip() for item in raw.split(",") if item.strip()}


def _ext_of(path_or_url: str) -> str:
    """提取 .mp4 形式扩展名（小写）。"""
    parsed = urlparse(path_or_url)
    candidate = parsed.path if parsed.scheme else path_or_url
    return Path(candidate).suffix.lower()


def _check_extension(value: str) -> None:
    ext = _ext_of(value)
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise ExtensionNotAllowed(
            f"extension_not_allowed: {ext or '(empty)'} not in {sorted(ALLOWED_VIDEO_EXTENSIONS)}"
        )


def _resolve_ips(host: str) -> list[str]:
    """gethostbyname 等价；返回所有 A/AAAA 记录。测试侧 monkeypatch 这里。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HostNotAllowed(f"dns_resolution_failed: {host}: {exc}") from exc
    return [info[4][0] for info in infos]


def _is_private_ip(ip_str: str) -> bool:
    """判断 IP 是否落入私有 / 链路本地 / 回环 / 元数据网段。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # 解析失败按危险处理
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _check_ip_safe(host: str) -> None:
    for ip in _resolve_ips(host):
        if _is_private_ip(ip):
            raise PrivateAddressBlocked(
                f"private_address_blocked: {host} resolves to {ip}"
            )


def _check_host_allowed(host: str) -> None:
    if host not in _allowlist():
        raise HostNotAllowed(
            f"host_not_allowed: {host} not in AUTOCUT_URL_ALLOWLIST"
        )


def _validate_https_url(url: str) -> tuple[str, str]:
    """校验 url。返回 (host, normalized_url)。"""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SchemeNotAllowed(f"scheme_not_allowed: {parsed.scheme or '(empty)'}")
    host = parsed.hostname
    if not host:
        raise InvalidSource(f"invalid_url: missing host: {url}")
    _check_extension(parsed.path)
    _check_host_allowed(host)
    _check_ip_safe(host)
    return host, url


def _validate_oss_uri(uri: str) -> tuple[str, str]:
    """oss://bucket/key → (bucket, key)。"""
    parsed = urlparse(uri)
    if parsed.scheme != "oss":
        raise SchemeNotAllowed(f"scheme_not_allowed: {parsed.scheme or '(empty)'}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise InvalidSource(f"invalid_oss_uri: {uri}")
    _check_extension(key)
    expected_bucket = os.environ.get(ENV_OSS_BUCKET, DEFAULT_OSS_BUCKET)
    if bucket != expected_bucket:
        raise HostNotAllowed(
            f"oss_bucket_not_allowed: {bucket} != {expected_bucket}"
        )
    return bucket, key


def _build_https_from_oss(bucket: str, key: str) -> str:
    endpoint = os.environ.get(ENV_OSS_ENDPOINT, DEFAULT_OSS_ENDPOINT)
    use_ssl = os.environ.get(ENV_OSS_USE_SSL, "true").lower() != "false"
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{endpoint}/{bucket}/{key}"


# ---------- httpx 下载 ----------


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise OssError(
            "httpx not installed. Install via: pip install -e '.[oss]' or '.[business]'"
        ) from exc
    return httpx


def _head_check(url: str) -> Optional[int]:
    """HEAD 预检 Content-Length。返回声明大小或 None。

    超过 2GB 抛 TooLarge；3xx 抛 RedirectNotAllowed；4xx/5xx 抛 UpstreamError。
    HEAD 不支持时（405/501）允许放过去 GET 阶段再校验。
    """
    httpx = _httpx()
    timeout = httpx.Timeout(HTTP_READ_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        try:
            resp = client.head(url)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"head_failed: {exc}") from exc
        if 300 <= resp.status_code < 400:
            raise RedirectNotAllowed(f"redirect_not_allowed: HEAD {resp.status_code}")
        if resp.status_code in (405, 501):
            return None
        if resp.status_code >= 400:
            raise UpstreamError(
                f"upstream_status: HEAD {resp.status_code}"
            ).with_traceback(None)
        cl = resp.headers.get("content-length")
        if cl is None:
            return None
        try:
            size = int(cl)
        except ValueError:
            return None
        if size > MAX_VIDEO_BYTES:
            raise TooLarge(f"too_large: declared={size} > {MAX_VIDEO_BYTES}")
        return size


def _stream_download(
    url: str,
    dest_path: Path,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> int:
    """流式 GET 落盘；返回实际字节数。超过 2GB 删除半截文件并抛 TooLarge。"""
    httpx = _httpx()
    timeout = httpx.Timeout(HTTP_READ_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    declared: Optional[int] = None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream("GET", url) as resp:
                if 300 <= resp.status_code < 400:
                    raise RedirectNotAllowed(
                        f"redirect_not_allowed: GET {resp.status_code}"
                    )
                if resp.status_code >= 400:
                    raise UpstreamError(f"upstream_status: GET {resp.status_code}")
                cl = resp.headers.get("content-length")
                if cl is not None:
                    try:
                        declared = int(cl)
                    except ValueError:
                        declared = None
                with dest_path.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_VIDEO_BYTES:
                            raise TooLarge(
                                f"too_large: streamed_bytes>{MAX_VIDEO_BYTES}"
                            )
                        fh.write(chunk)
                        if on_progress is not None:
                            on_progress(written, declared)
    except (TooLarge, RedirectNotAllowed, UpstreamError):
        # 删除半截文件
        try:
            dest_path.unlink()
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            dest_path.unlink()
        except OSError:
            pass
        raise OssError(f"local_write_failed: {exc}") from exc
    except Exception as exc:
        try:
            dest_path.unlink()
        except OSError:
            pass
        # 把 httpx.HTTPError 等也归为 UpstreamError
        raise UpstreamError(f"download_failed: {exc}") from exc
    return written


def _minio_fallback_download(bucket: str, key: str, dest_path: Path) -> int:
    """私有 bucket / 401 / 403 时的 SDK 兜底。"""
    try:
        from minio import Minio
        from minio.error import S3Error
    except ImportError as exc:  # pragma: no cover
        raise OssError(
            "minio not installed. Install via: pip install -e '.[oss]'"
        ) from exc
    endpoint = os.environ.get(ENV_OSS_ENDPOINT, DEFAULT_OSS_ENDPOINT)
    use_ssl = os.environ.get(ENV_OSS_USE_SSL, "true").lower() != "false"
    access = os.environ.get(ENV_OSS_ACCESS_KEY, "")
    secret = os.environ.get(ENV_OSS_SECRET_KEY, "")
    if not access or not secret:
        raise OssError(
            f"{ENV_OSS_ACCESS_KEY}/{ENV_OSS_SECRET_KEY} required for SDK fallback"
        )
    client = Minio(endpoint, access_key=access, secret_key=secret, secure=use_ssl)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        response = client.get_object(bucket, key)
        try:
            with dest_path.open("wb") as fh:
                for chunk in response.stream(64 * 1024):
                    written += len(chunk)
                    if written > MAX_VIDEO_BYTES:
                        raise TooLarge(
                            f"too_large: streamed_bytes>{MAX_VIDEO_BYTES} (sdk)"
                        )
                    fh.write(chunk)
        finally:
            response.close()
            response.release_conn()
    except TooLarge:
        try:
            dest_path.unlink()
        except OSError:
            pass
        raise
    except S3Error as exc:
        try:
            dest_path.unlink()
        except OSError:
            pass
        raise UpstreamError(f"sdk_status: {exc.code}") from exc
    except OSError as exc:
        try:
            dest_path.unlink()
        except OSError:
            pass
        raise OssError(f"local_write_failed: {exc}") from exc
    return written


# ---------- 公共入口 ----------


def resolve_video_source(
    source: dict[str, Any],
    dest_dir: Path,
    *,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> Path:
    """根据 source 描述把视频落到 dest_dir/source<ext>，返回最终路径。

    source 形态：
      - {"type": "upload", "uploaded_path": "/abs/path"}
      - {"type": "url",    "value": "https://..."}
      - {"type": "oss",    "value": "oss://bucket/key"}
    """
    if not isinstance(source, dict):
        raise InvalidSource("source_must_be_object")
    src_type = source.get("type")

    if src_type == "upload":
        uploaded = source.get("uploaded_path")
        if not isinstance(uploaded, str) or not uploaded:
            raise InvalidSource("upload_missing_uploaded_path")
        path = Path(uploaded)
        if not path.exists():
            raise InvalidSource(f"upload_path_not_found: {uploaded}")
        _check_extension(uploaded)
        return path

    if src_type == "url":
        url = source.get("value")
        if not isinstance(url, str) or not url:
            raise InvalidSource("url_missing_value")
        host, normalized = _validate_https_url(url)
        ext = _ext_of(normalized) or ".mp4"
        dest_path = dest_dir / f"source{ext}"
        _head_check(normalized)
        _stream_download(normalized, dest_path, on_progress=on_progress)
        return dest_path

    if src_type == "oss":
        uri = source.get("value")
        if not isinstance(uri, str) or not uri:
            raise InvalidSource("oss_missing_value")
        bucket, key = _validate_oss_uri(uri)
        ext = _ext_of(key) or ".mp4"
        dest_path = dest_dir / f"source{ext}"
        https_url = _build_https_from_oss(bucket, key)
        # SSRF 校验仍走 host 路径
        host = urlparse(https_url).hostname
        if host:
            _check_ip_safe(host)
        try:
            _head_check(https_url)
            _stream_download(https_url, dest_path, on_progress=on_progress)
        except UpstreamError as exc:
            # 401/403 → SDK fallback
            msg = str(exc)
            if "401" in msg or "403" in msg:
                _minio_fallback_download(bucket, key, dest_path)
            else:
                raise
        return dest_path

    raise InvalidSource(f"unknown_source_type: {src_type!r}")
