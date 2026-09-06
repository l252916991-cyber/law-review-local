"""Local-only default; optional token principals with explicit case scopes.

Tokens come from deployment configuration, not request-supplied user names.
This is a small single-process deployment boundary, not a full identity provider.
"""
from __future__ import annotations

import hmac
import asyncio
import ipaddress
import json
import logging
import os
import re
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .db import connect, now, transaction

logger = logging.getLogger(__name__)


class AccessConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    name: str
    admin: bool
    case_ids: tuple[int, ...] = ()
    local: bool = False


_principal: ContextVar[Principal | None] = ContextVar("lexvault_principal", default=None)
router = APIRouter(prefix="/api/auth", tags=["access"])


def auth_mode() -> str:
    mode = os.getenv("LAW_REVIEW_AUTH_MODE", "local")
    if mode not in {"local", "token"}:
        raise AccessConfigurationError("LAW_REVIEW_AUTH_MODE must be local or token")
    return mode


def configured_tokens() -> dict[str, Principal]:
    try:
        raw = json.loads(os.getenv("LAW_REVIEW_API_TOKENS_JSON", "{}"))
        if not isinstance(raw, dict) or not raw:
            raise ValueError("No tokens configured")
        tokens = {}
        names = set()
        for token, value in raw.items():
            if len(token) < 32 or not isinstance(value, dict):
                raise ValueError("Invalid token configuration")
            name = value["name"]
            admin = value.get("admin", False)
            ids = value.get("case_ids", [])
            if not isinstance(name, str) or not name.strip() or name in names or type(admin) is not bool or not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids):
                raise ValueError("Invalid principal configuration")
            names.add(name)
            tokens[token] = Principal(name, admin, tuple(ids))
        return tokens
    except (ValueError, TypeError, KeyError) as exc:
        raise AccessConfigurationError("Token mode requires valid unique principals and tokens of at least 32 characters") from exc


def token_principal(token: str) -> Principal | None:
    found = None
    for configured, principal in configured_tokens().items():
        if hmac.compare_digest(configured.encode(), token.encode()):
            found = principal
    return found


def current_principal() -> Principal:
    value = _principal.get()
    if value is None:
        if auth_mode() == "token":
            raise HTTPException(401, "需要身份认证")
        return Principal("本机律师", True, local=True)
    return value


def actor_name(fallback: str = "本机律师") -> str:
    p = current_principal()
    return fallback if p.local else p.name


def allowed_case_ids() -> tuple[int, ...] | None:
    p = current_principal()
    return None if p.admin else p.case_ids


def require_case_access(case_id: int) -> None:
    ids = allowed_case_ids()
    if ids is not None and case_id not in ids:
        raise HTTPException(404, "案件不存在或无访问权限")


def _owned_case(path: str) -> int | None:
    direct = re.match(r"^/api/cases/(\d+)(?:/|$)", path)
    if direct:
        return int(direct[1])
    match = re.match(r"^/api/(documents|evidence|conversations|agent-runs|batch-imports|agent-jobs|annotations|evidence-annotations)/([^/]+)", path)
    if not match:
        return None
    group, identifier = match.groups()
    sql = {
        "documents": "SELECT case_id FROM documents WHERE id=?",
        "evidence": "SELECT case_id FROM evidence WHERE id=?",
        "conversations": "SELECT case_id FROM conversations WHERE id=?",
        "agent-runs": "SELECT case_id FROM agent_runs WHERE id=?",
        "batch-imports": "SELECT case_id FROM batch_imports WHERE id=?",
        "agent-jobs": "SELECT case_id FROM review_jobs WHERE id=?",
        "annotations": "SELECT e.case_id FROM evidence_annotations a JOIN evidence e ON e.id=a.evidence_id WHERE a.id=?",
        "evidence-annotations": "SELECT e.case_id FROM evidence_annotations a JOIN evidence e ON e.id=a.evidence_id WHERE a.id=?",
    }[group]
    with closing(connect()) as conn:
        row = conn.execute(sql, (identifier,)).fetchone()
    if not row:
        raise HTTPException(404, "资源不存在")
    return row[0]


def apply_security_headers(response: Response, path: str) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if path == "/" or path.startswith("/assets/"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class AccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        marker = None
        case_id = None
        try:
            mode = auth_mode()
            host = request.url.hostname or ""
            allowed_hosts = {h.strip() for h in os.getenv("LAW_REVIEW_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver").split(",")}
            if host not in allowed_hosts:
                raise HTTPException(400, "不允许的 Host")
            # Block cross-origin state changes, including localhost CSRF.
            if request.method not in {"GET", "HEAD", "OPTIONS"} or request.url.path.startswith("/api/"):
                origin = request.headers.get("origin")
                expected_origin = f"{request.url.scheme}://{request.url.netloc}"
                if (origin and origin.rstrip("/") != expected_origin) or request.headers.get("sec-fetch-site") == "cross-site":
                    raise HTTPException(403, "不允许跨站写入")
            if mode == "local":
                if any(key in request.headers for key in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
                    raise HTTPException(403, "代理访问必须启用 token 模式")
                peer = request.client.host if request.client else ""
                try:
                    local_peer = ipaddress.ip_address(peer).is_loopback
                except ValueError:
                    local_peer = peer == "testclient"
                if not local_peer:
                    raise HTTPException(403, "本机模式不接受远程访问，请配置 token 模式")
                principal = Principal("本机律师", True, local=True)
            else:
                configured_tokens()  # Fail closed even on auth endpoints.
                authorization = request.headers.get("authorization", "")
                token = authorization[7:] if authorization.startswith("Bearer ") else request.cookies.get("lexvault_session", "")
                principal = token_principal(token) if token else None
                public = not request.url.path.startswith("/api/") or request.url.path in {"/api/auth/session", "/api/auth/me", "/api/health"}
                if principal is None and not public:
                    raise HTTPException(401, "需要有效访问令牌")
            marker = _principal.set(principal)
            if principal and request.url.path.startswith("/api/"):
                case_id = _owned_case(request.url.path)
                if case_id is not None:
                    require_case_access(case_id)
                if not principal.admin:
                    if request.url.path == "/api/cases" and request.method != "GET":
                        raise HTTPException(403, "仅管理员可以创建案件")
                    if case_id is None and request.url.path not in {"/api/cases", "/api/health", "/api/benchmarks/lawbench", "/api/auth/me", "/api/auth/session"}:
                        raise HTTPException(403, "需要管理员权限")
            response = await call_next(request)
            if principal and not principal.local and response.status_code < 400 and (
                request.method in {"POST", "PATCH", "PUT", "DELETE"} or request.url.path.endswith("/export")
            ) and not request.url.path.startswith("/api/auth/"):
                # Do not put request bodies, tokens, legal text, or raw errors in
                # the security trail. Endpoint records retain business details.
                def write_audit():
                    with transaction() as conn:
                        conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'认证操作',?,?)",
                                     (case_id, f"{principal.name}：{request.method} {request.url.path}", now()))
                try:
                    await asyncio.to_thread(write_audit)
                except Exception as exc:
                    logger.error("Authenticated operation audit failed (%s)", type(exc).__name__)
            response = apply_security_headers(response, request.url.path)
            return response
        except HTTPException as exc:
            return apply_security_headers(JSONResponse({"detail": exc.detail}, status_code=exc.status_code), request.url.path)
        except AccessConfigurationError:
            return apply_security_headers(JSONResponse({"detail": "访问控制配置错误，请联系管理员"}, status_code=503), request.url.path)
        finally:
            if marker is not None:
                _principal.reset(marker)


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.post("/session")
def login(body: Login, request: Request, response: Response):
    if auth_mode() != "token":
        raise HTTPException(409, "本机模式无需登录")
    principal = token_principal(body.token)
    if principal is None:
        raise HTTPException(401, "无效访问令牌")
    local_names = {"localhost", "127.0.0.1", "::1", "testserver"}
    response.set_cookie("lexvault_session", body.token, httponly=True,
                        secure=request.url.scheme == "https" or request.url.hostname not in local_names,
                        samesite="strict", max_age=8 * 3600)
    return {"name": principal.name, "admin": principal.admin, "case_ids": principal.case_ids}


@router.delete("/session")
def logout(response: Response):
    response.delete_cookie("lexvault_session")
    return {"logged_out": True}


@router.get("/me")
def identity():
    value = _principal.get()
    return {"mode": auth_mode(), "authenticated": value is not None, "name": value.name if value else None, "admin": value.admin if value else False}
