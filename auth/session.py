from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from fastapi import Request, WebSocket


@dataclass(frozen=True)
class SessionCookieConfig:
    cookie_name: str = "session_id"
    # 这里先用一个比较保守的默认时长；后续可在 config 里引入可配置项并做 TTL 清理策略
    max_age_seconds: int = 60 * 60 * 24 * 30  # 30 days
    same_site: str = "lax"
    http_only: bool = True


SESSION_COOKIE = SessionCookieConfig()


def ensure_session_id(existing_session_id: Optional[str]) -> str:
    """确保一定返回一个有效的 session_id。

    说明：
    - 本项目目前没有“登录态用户”，因此匿名 session_id 用于隔离 conversation。
    - 这里不做复杂签名校验（仍是随机 UUID），会在后续与 DB 的归属校验配合。
    """

    if existing_session_id and existing_session_id.strip():
        return existing_session_id.strip()
    return uuid4().hex


def get_session_id_from_request(request: Request) -> Optional[str]:
    """从 HTTP 请求 cookies 提取 session_id。"""

    return request.cookies.get(SESSION_COOKIE.cookie_name)


def get_session_id_from_websocket(websocket: WebSocket) -> Optional[str]:
    """从 WebSocket 握手请求中解析 cookies。

    FastAPI 的 WebSocket 对象没有 request.cookies 接口，因此需要手动解析 header。
    """

    cookie_header = websocket.headers.get("cookie") or ""
    if not cookie_header:
        return None

    # cookie_header 示例： "a=b; session_id=xxxx; c=d"
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == SESSION_COOKIE.cookie_name:
            return v.strip() or None
    return None

