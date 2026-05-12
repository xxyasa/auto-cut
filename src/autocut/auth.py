"""业务自助成片闭环鉴权模块。

设计要点（见 .openspec/proposals/0001-business-self-service-clip.md §6）：
- 启动时读 `AUTOCUT_API_TOKEN`，未配置则启动失败。
- 三通道接受 token：`Authorization: Bearer <t>` / `X-Autocut-Token: <t>` / `Cookie: autocut_token=<t>`。
  - Authorization、X-Autocut-Token 用于业务前端 fetch。
  - Cookie 通道专为 `/business.html` refresh 设计（页面 GET 时浏览器自动带）。
- 仅用于 `/api/business/*` 与 `/business.html`；原有 `/api/runs/*` 不受影响。
- 单次比对用 `secrets.compare_digest`，避免时序侧信道。
"""

from __future__ import annotations

import os
import secrets
from typing import Optional

try:
    from fastapi import HTTPException, Request, status
except ImportError:  # pragma: no cover - API extras 未安装时仍能 import
    HTTPException = RuntimeError  # type: ignore[assignment, misc]
    Request = object  # type: ignore[assignment, misc]

    class _StatusStub:
        HTTP_401_UNAUTHORIZED = 401
        HTTP_500_INTERNAL_SERVER_ERROR = 500

    status = _StatusStub()  # type: ignore[assignment]


COOKIE_NAME = "autocut_token"
HEADER_NAME = "X-Autocut-Token"
ENV_VAR = "AUTOCUT_API_TOKEN"


class AuthConfigError(RuntimeError):
    """`AUTOCUT_API_TOKEN` 未设置或为空。启动期抛出。"""


def load_expected_token() -> str:
    """从环境变量读取期望 token。

    Raises:
        AuthConfigError: 环境变量缺失或空串。
    """
    token = os.environ.get(ENV_VAR, "").strip()
    if not token:
        raise AuthConfigError(
            f"{ENV_VAR} is required but not set. "
            f"Refusing to start business API without an API token."
        )
    return token


def extract_token(
    authorization: Optional[str],
    header_token: Optional[str],
    cookie_token: Optional[str],
) -> Optional[str]:
    """三通道按优先级提取 token，返回首个非空值。

    优先级：Authorization Bearer > X-Autocut-Token header > Cookie。
    """
    if authorization:
        value = authorization.strip()
        if value.lower().startswith("bearer "):
            candidate = value[7:].strip()
            if candidate:
                return candidate
    if header_token:
        candidate = header_token.strip()
        if candidate:
            return candidate
    if cookie_token:
        candidate = cookie_token.strip()
        if candidate:
            return candidate
    return None


def verify_token(provided: Optional[str], expected: str) -> bool:
    """常数时间比较；任一为空直接返回 False。"""
    if not provided or not expected:
        return False
    # secrets.compare_digest 对等长字符串才常数时间，但其内部会处理不等长情况
    # 用 encode 避免 unicode normalization 差异
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def require_token(request: "Request"):  # pragma: no cover - 实际依赖 FastAPI 注入
    """FastAPI 依赖项：校验请求 token，失败抛 401。

    使用方式：
        from fastapi import Depends
        from .auth import require_token

        @router.get("/api/business/brands", dependencies=[Depends(require_token)])
        def list_brands():
            ...
    """
    try:
        expected = load_expected_token()
    except AuthConfigError as exc:
        # 启动期已校验过；运行时再次失败说明环境变量被清除，500 而非 401
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"server_misconfigured: {exc}",
        ) from exc

    authorization = request.headers.get("authorization")
    header_token = request.headers.get(HEADER_NAME.lower()) or request.headers.get(HEADER_NAME)
    cookie_token = request.cookies.get(COOKIE_NAME)

    provided = extract_token(authorization, header_token, cookie_token)
    if not verify_token(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_or_missing_token",
        )
