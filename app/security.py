from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

SESSION_COOKIE = "ui_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_token(nbytes: int = 18) -> str:
    return secrets.token_urlsafe(nbytes)


def token_matches(expected: str, supplied: str) -> bool:
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(expected), str(supplied))


def _serializer(cfg: dict[str, Any]) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.get("secret_key", ""), salt="uninterupted-session")


def sign_session(cfg: dict[str, Any], data: dict[str, Any]) -> str:
    return _serializer(cfg).dumps(data)


def read_session(request: Request, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cfg = cfg or config.load_config()
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not cfg.get("secret_key"):
        return None
    try:
        data = _serializer(cfg).loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


def set_session(response: Response, cfg: dict[str, Any], data: dict[str, Any]) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(cfg, data),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # LAN HTTP; put a TLS reverse proxy in front if exposed.
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def is_admin(request: Request, cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg or config.load_config()
    data = read_session(request, cfg)
    return bool(data and data.get("admin"))


def require_admin(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dependency for admin pages/APIs.

    Returns (config, session). Redirects to setup/login with 303 so that a POST
    that lost its session becomes a GET instead of re-posting.
    """
    cfg = config.load_config()
    if not config.is_configured(cfg):
        raise HTTPException(status_code=303, headers={"Location": "/setup"})
    data = read_session(request, cfg)
    if not data or not data.get("admin"):
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        from urllib.parse import quote

        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={quote(target, safe='')}"},
        )
    return cfg, data
