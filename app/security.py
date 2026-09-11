"""Local-only default; optional token principals with explicit case scopes.

Tokens come from deployment configuration, not request-supplied user names.
Authentication state is durable; deployment lifecycle constraints remain separate.
"""
from __future__ import annotations

import hmac
import hashlib
import secrets
import base64
import time
import asyncio
import ipaddress
import json
import logging
import os
import re
import urllib.error
import urllib.parse
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .db import connect, now, record_security_event, transaction
from .services import egress_opener

SESSION_TTL = 8 * 3600
SESSION_IDLE_TTL = 30 * 60
LOGIN_WINDOW = 15 * 60
LOGIN_LIMIT = 10
OIDC_START_LIMIT = 100


def _session_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _session_key(value: str) -> str:
    return _session_hash(value)


def _purge_sessions() -> None:
    with transaction() as conn:
        from .db import purge_expired_sessions
        purge_expired_sessions(conn)


def session_principal(value: str) -> Principal | None:
    if not value:
        return None
    try:
        with transaction() as conn:
            from .db import purge_expired_sessions
            purge_expired_sessions(conn)
            row = conn.execute(
                "UPDATE auth_sessions SET last_seen_at=? WHERE session_hash=? AND expires_at > ? "
                "AND last_seen_at > ? RETURNING kind, credential_fingerprint, oidc_subject",
                (int(time.time()), _session_hash(value), int(time.time()), int(time.time()) - SESSION_IDLE_TTL),
            ).fetchone()
    except Exception as exc:
        logger.error("Session lookup failed (%s)", type(exc).__name__)
        return None
    if row is None:
        return None
    if row["kind"] == "token":
        for token, principal in configured_tokens().items():
            if hmac.compare_digest(_session_hash(token), row["credential_fingerprint"]):
                return principal
        return None
    settings = oidc_settings()
    return settings["principals"].get(row["oidc_subject"]) if settings else None


def oidc_session_principal(request_value: str) -> Principal | None:
    # Compatibility helper retained for callers; all session kinds now resolve
    # through the durable table and current authorization mappings.
    return session_principal(request_value)


logger = logging.getLogger(__name__)


class AccessConfigurationError(ValueError):
    pass


ALL_PERMISSIONS = frozenset({"view", "edit", "approve", "export", "manage"})
# Principals configured before permissions existed keep exactly the abilities
# they always had (admins keep everything). New configurations should list
# permissions explicitly instead of relying on this compatibility default.
LEGACY_MEMBER_PERMISSIONS = frozenset({"view", "edit", "approve", "export"})


@dataclass(frozen=True)
class Principal:
    name: str
    admin: bool
    case_ids: tuple[int, ...] = ()
    local: bool = False
    permissions: frozenset[str] = LEGACY_MEMBER_PERMISSIONS

    def has(self, permission: str) -> bool:
        return self.admin or permission in self.permissions


_principal: ContextVar[Principal | None] = ContextVar("lexvault_principal", default=None)
router = APIRouter(prefix="/api/auth", tags=["access"])


def auth_mode() -> str:
    mode = os.getenv("LAW_REVIEW_AUTH_MODE", "local")
    if mode not in {"local", "token"}:
        raise AccessConfigurationError("LAW_REVIEW_AUTH_MODE must be local or token")
    return mode


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})


def allowed_hosts() -> set[str]:
    return {item.strip() for item in os.getenv("LAW_REVIEW_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver").split(",") if item.strip()}


def _min_token_length() -> int:
    """Token length floor; a short token is a deliberate loopback-only escape hatch."""
    raw = os.getenv("LAW_REVIEW_ALLOW_INSECURE_LOCAL_TOKEN", "").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return 32
    if not allowed_hosts() <= _LOOPBACK_HOSTS:
        # Refuse instead of silently weakening: a reachable host would let a
        # guessable token be attacked from the network.
        raise AccessConfigurationError("LAW_REVIEW_ALLOW_INSECURE_LOCAL_TOKEN requires loopback-only LAW_REVIEW_ALLOWED_HOSTS")
    return 1


def configured_tokens() -> dict[str, Principal]:
    min_length = _min_token_length()
    try:
        raw = json.loads(os.getenv("LAW_REVIEW_API_TOKENS_JSON", "{}"))
        if not isinstance(raw, dict) or not raw:
            raise ValueError("No tokens configured")
        tokens = {}
        names = set()
        for token, value in raw.items():
            if len(token) < min_length or not isinstance(value, dict):
                raise ValueError("Invalid token configuration")
            name = value["name"]
            admin = value.get("admin", False)
            ids = value.get("case_ids", [])
            permissions = value.get("permissions", list(ALL_PERMISSIONS if admin else LEGACY_MEMBER_PERMISSIONS))
            valid_permissions = isinstance(permissions, list) and permissions and all(p in ALL_PERMISSIONS for p in permissions)
            if not isinstance(name, str) or not name.strip() or name in names or type(admin) is not bool or not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids) or not valid_permissions:
                raise ValueError("Invalid principal configuration")
            names.add(name)
            tokens[token] = Principal(name, admin, tuple(ids), permissions=frozenset(permissions))
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
        return Principal("本机律师", True, local=True, permissions=ALL_PERMISSIONS)
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


PERMISSION_LABELS = {"view": "查看", "edit": "编辑", "approve": "审批确认", "export": "导出", "manage": "管理"}


def require_permission(permission: str) -> None:
    """Endpoint-level check for body-dependent permissions such as approve."""
    principal = current_principal()
    if not principal.has(permission):
        raise HTTPException(403, f"需要{PERMISSION_LABELS[permission]}权限")


def _required_case_permission(method: str, path: str) -> str:
    if path.endswith("/export"):
        return "export"
    if method == "POST" and re.fullmatch(r"/api/cases/\d+/agent-tools/call", path):
        return "view"
    if method in {"POST", "PATCH", "PUT", "DELETE"}:
        return "edit"
    return "view"


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
    if path in {"/", "/mobile"} or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
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
        principal = None
        try:
            mode = auth_mode()
            host = request.url.hostname or ""
            allowed_hosts = {h.strip() for h in os.getenv("LAW_REVIEW_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver").split(",")}
            if host not in allowed_hosts:
                raise HTTPException(400, "不允许的 Host")
            # Block cross-origin state changes, including localhost CSRF.
            oidc_callback_get = (mode == "token" and request.method == "GET"
                                 and request.url.path == "/api/auth/oidc/callback")
            if not oidc_callback_get and (request.method not in {"GET", "HEAD", "OPTIONS"} or request.url.path.startswith("/api/")):
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
                trusted_local_peers = {
                    item.strip()
                    for item in os.getenv("LAW_REVIEW_TRUSTED_LOCAL_PEERS", "").split(",")
                    if item.strip()
                }
                local_peer = local_peer or peer in trusted_local_peers
                if not local_peer:
                    raise HTTPException(403, "本机模式不接受远程访问，请配置 token 模式")
                principal = Principal("本机律师", True, local=True, permissions=ALL_PERMISSIONS)
            else:
                configured_tokens()  # Fail closed even on auth endpoints.
                authorization = request.headers.get("authorization", "")
                if authorization.startswith("Bearer "):
                    principal = token_principal(authorization[7:])
                else:
                    principal = session_principal(request.cookies.get("lexvault_session", ""))
                public = not request.url.path.startswith("/api/") or request.url.path in {"/api/auth/session", "/api/auth/me", "/api/auth/oidc/login", "/api/auth/oidc/callback", "/api/health"}
                if principal is None and not public:
                    raise HTTPException(401, "需要有效访问令牌")
            marker = _principal.set(principal)
            if principal and request.url.path.startswith("/api/"):
                case_id = _owned_case(request.url.path)
                if case_id is not None:
                    require_case_access(case_id)
                    needed = _required_case_permission(request.method, request.url.path)
                    if not principal.has(needed):
                        raise HTTPException(403, f"需要{PERMISSION_LABELS[needed]}权限")
                if not principal.has("manage"):
                    if request.url.path == "/api/cases" and request.method != "GET":
                        raise HTTPException(403, "仅管理员可以创建案件")
                    if case_id is None and request.url.path not in {"/api/cases/lifecycle/archived", "/api/cases/lifecycle/trash", "/api/cases", "/api/health", "/api/benchmarks/lawbench", "/api/auth/me", "/api/auth/session", "/api/auth/oidc/login", "/api/auth/oidc/callback", "/api/export-templates"}:
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
            record_security_event(
                "access", "denied", actor=principal.name if principal else None, case_id=case_id,
                detail=str(exc.detail)[:120], request_path=request.url.path,
            )
            return apply_security_headers(JSONResponse({"detail": exc.detail}, status_code=exc.status_code), request.url.path)
        except AccessConfigurationError:
            record_security_event("config", "error", detail="访问控制配置错误", request_path=request.url.path)
            return apply_security_headers(JSONResponse({"detail": "访问控制配置错误，请联系管理员"}, status_code=503), request.url.path)
        finally:
            if marker is not None:
                _principal.reset(marker)


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=512)


def _register_session(fingerprint: str) -> str:
    """Create one opaque durable token session for a verified credential."""
    session = secrets.token_urlsafe(32)
    try:
        with transaction() as conn:
            from .db import purge_expired_sessions
            purge_expired_sessions(conn)
            count = conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0]
            if count >= 4096:
                raise HTTPException(503, "会话容量已满，请稍后重试")
            conn.execute(
                "INSERT INTO auth_sessions(session_hash,kind,credential_fingerprint,expires_at,created_at,last_seen_at) VALUES (?, 'token', ?, ?, ?, ?)",
                (_session_hash(session), fingerprint, int(time.time()) + SESSION_TTL, now(), int(time.time())),
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Session registration failed (%s)", type(exc).__name__)
        raise HTTPException(503, "会话服务暂不可用") from exc
    return session


def _register_oidc_session(subject: str, flow=None) -> str:
    """Create one opaque durable session bound to a verified OIDC subject."""
    session = secrets.token_urlsafe(32)
    try:
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if flow is not None:
                conn.execute("DELETE FROM auth_sessions WHERE session_hash=?", (flow["previous_session_hash"],))
            from .db import purge_expired_sessions
            purge_expired_sessions(conn)
            count = conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0]
            if count >= 4096:
                raise HTTPException(503, "会话容量已满，请稍后重试")
            conn.execute(
                "INSERT INTO auth_sessions(session_hash,kind,oidc_subject,expires_at,created_at,last_seen_at) VALUES (?, 'oidc', ?, ?, ?, ?)",
                (_session_hash(session), subject, int(time.time()) + SESSION_TTL, now(), int(time.time())),
            )
            if flow is not None:
                # Refund only this flow's reservation in its original window.
                # A late callback must not decrement a newer window's count.
                conn.execute(
                    "UPDATE auth_login_limits SET attempts=MAX(0,attempts-1) WHERE bucket=? AND expires_at=?",
                    (flow["limit_bucket"], flow["limit_expires_at"]),
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("OIDC session registration failed (%s)", type(exc).__name__)
        raise HTTPException(503, "会话服务暂不可用") from exc
    return session


def _revoke_session(value: str) -> None:
    if not value:
        return
    try:
        with transaction() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE session_hash=?", (_session_hash(value),))
    except Exception as exc:
        logger.error("Session revocation failed (%s)", type(exc).__name__)
        raise HTTPException(503, "会话服务暂不可用") from exc


def _issue_session_cookie(response: Response, request: Request, session: str) -> None:
    local_names = {"localhost", "127.0.0.1", "::1", "testserver"}
    response.set_cookie("lexvault_session", session, httponly=True,
                        secure=request.url.scheme == "https" or request.url.hostname not in local_names,
                        samesite="strict", max_age=8 * 3600)


@router.post("/session")
def login(body: Login, request: Request, response: Response):
    if auth_mode() != "token":
        raise HTTPException(409, "本机模式无需登录")
    principal = _limited_token_principal(body.token, request)
    if principal is None:
        record_security_event("auth", "login_failed", detail="无效访问令牌", request_path=request.url.path)
        raise HTTPException(401, "无效访问令牌")
    fingerprint = hashlib.sha256(body.token.encode()).hexdigest()
    _revoke_session(request.cookies.get("lexvault_session", ""))
    session = _register_session(fingerprint)
    record_security_event("auth", "login_success", actor=principal.name, request_path=request.url.path)
    _issue_session_cookie(response, request, session)
    return {"name": principal.name, "admin": principal.admin, "case_ids": principal.case_ids,
            "permissions": sorted(principal.permissions)}


@router.delete("/session")
def logout(request: Request, response: Response):
    _revoke_session(request.cookies.get("lexvault_session", ""))
    record_security_event("auth", "logout", request_path=request.url.path)
    response.delete_cookie("lexvault_session")
    return {"logged_out": True}


@router.get("/me")
def identity():
    value = _principal.get()
    return {"mode": auth_mode(), "authenticated": value is not None, "name": value.name if value else None, "admin": value.admin if value else False,
            "permissions": sorted(value.permissions) if value else [],
            "organization_name": os.getenv("LAW_REVIEW_ORGANIZATION_NAME", "").strip(),
            "support_contact": os.getenv("LAW_REVIEW_SUPPORT_CONTACT", "").strip(),
            "oidc_enabled": auth_mode() == "token" and oidc_settings() is not None}


# --- Optional organizational login (OIDC authorization-code flow). ---
# Unconfigured by default; a partially configured deployment fails closed.
# Real issuer reachability is a deployment acceptance item; tests use a mock
# provider at the HTTP-fetch layer.

OIDC_FLOW_TTL = 600
OIDC_FLOW_COOKIE = "lexvault_oidc_flow"
OIDC_CALLBACK_PATH = "/api/auth/oidc/callback"
_oidc_discovery_cache: dict[str, tuple[float, dict]] = {}


def oidc_settings() -> dict[str, Any] | None:
    issuer = os.getenv("LAW_REVIEW_OIDC_ISSUER", "").strip().rstrip("/")
    client_id = os.getenv("LAW_REVIEW_OIDC_CLIENT_ID", "").strip()
    client_secret = os.getenv("LAW_REVIEW_OIDC_CLIENT_SECRET", "")
    if not (issuer or client_id or client_secret):
        return None
    if not (issuer and client_id and client_secret):
        raise AccessConfigurationError("OIDC requires issuer, client id and client secret together")
    parsed = urllib.parse.urlparse(issuer)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise AccessConfigurationError("OIDC issuer URL is invalid")
    if parsed.scheme != "https" and host not in {"127.0.0.1", "::1", "localhost"}:
        raise AccessConfigurationError("OIDC issuer must use https outside loopback")
    raw = os.getenv("LAW_REVIEW_OIDC_PRINCIPALS_JSON", "{}")
    try:
        mapping = json.loads(raw)
        if not isinstance(mapping, dict):
            raise ValueError("not a mapping")
        principals = {}
        for subject, spec in mapping.items():
            if not isinstance(subject, str) or not subject or not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not spec["name"].strip():
                raise ValueError("invalid principal entry")
            admin = spec.get("admin", False)
            ids = spec.get("case_ids", [])
            permissions = spec.get("permissions", list(ALL_PERMISSIONS if admin else LEGACY_MEMBER_PERMISSIONS))
            if type(admin) is not bool or not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids) \
                    or not isinstance(permissions, list) or not permissions or any(p not in ALL_PERMISSIONS for p in permissions):
                raise ValueError("invalid principal entry")
            principals[subject] = Principal(spec["name"], admin, tuple(ids), permissions=frozenset(permissions))
    except (ValueError, TypeError) as exc:
        raise AccessConfigurationError("LAW_REVIEW_OIDC_PRINCIPALS_JSON must map subjects to valid principals") from exc
    return {"issuer": issuer, "client_id": client_id, "client_secret": client_secret, "principals": principals}


def fetch_json(url: str, *, data: dict[str, str] | None = None, timeout: int = 5) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    opener = egress_opener()
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def oidc_discovery(issuer: str) -> dict:
    cached = _oidc_discovery_cache.get(issuer)
    current = time.monotonic()
    if cached and cached[0] > current:
        return cached[1]
    document = fetch_json(f"{issuer}/.well-known/openid-configuration")
    if not all(document.get(key) for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")):
        raise ValueError("OIDC discovery document is incomplete")
    _oidc_discovery_cache[issuer] = (current + 3600, document)
    return document


def oidc_verify_id_token(id_token: str, settings: dict[str, Any], discovery: dict, expected_nonce: str) -> dict:
    import jwt as pyjwt
    try:
        header = pyjwt.get_unverified_header(id_token)
        jwks = fetch_json(discovery["jwks_uri"])
        key = next((item for item in jwks.get("keys", []) if item.get("kid") == header.get("kid")), None)
        if key is None:
            raise ValueError("no matching JWK for token")
        from jwt import PyJWK
        claims = pyjwt.decode(
            id_token, PyJWK.from_dict(key).key, algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=settings["client_id"], issuer=settings["issuer"], options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except pyjwt.PyJWTError as exc:
        # Signature, audience, issuer and expiry problems all land here so the
        # callback can answer with one sanitized failure.
        raise ValueError("id token validation failed") from exc
    if claims.get("nonce") != expected_nonce:
        raise ValueError("nonce mismatch")
    return claims


def _login_bucket(request: Request, kind: str) -> str:
    # Use the ASGI peer, never client-controlled forwarding headers. A trusted
    # proxy must normalize the ASGI client address or shares one rate bucket.
    return _session_hash(kind + ":" + (request.client.host if request.client else "unknown"))


def _check_login_limit(conn, bucket: str, limit: int = LOGIN_LIMIT) -> None:
    conn.execute("DELETE FROM auth_login_limits WHERE expires_at <= ?", (int(time.time()),))
    row = conn.execute("SELECT attempts FROM auth_login_limits WHERE bucket=?", (bucket,)).fetchone()
    if row and row[0] >= limit:
        raise HTTPException(429, "登录尝试过多，请稍后重试")


def _count_login_attempt(conn, bucket: str) -> None:
    conn.execute(
        "INSERT INTO auth_login_limits(bucket,attempts,expires_at) VALUES (?,1,?) "
        "ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1",
        (bucket, int(time.time()) + LOGIN_WINDOW),
    )


def _limited_token_principal(token: str, request: Request) -> Principal | None:
    try:
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bucket = _login_bucket(request, "token")
            _check_login_limit(conn, bucket)
            principal = token_principal(token)
            if principal is None:
                _count_login_attempt(conn, bucket)
            return principal
    except (HTTPException, AccessConfigurationError):
        raise
    except Exception as exc:
        logger.error("Login limit storage failed (%s)", type(exc).__name__)
        raise HTTPException(503, "登录服务暂不可用") from exc


def _oidc_settings_hash(settings: dict) -> str:
    return _session_hash(json.dumps([settings["issuer"], settings["client_id"], settings["client_secret"]]))


def _register_oidc_flow(request: Request, settings: dict, redirect_uri: str) -> tuple[str, str, str, str]:
    state, nonce, browser, verifier = (secrets.token_urlsafe(32) for _ in range(4))
    with transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        bucket = _login_bucket(request, "oidc-start")
        _check_login_limit(conn, bucket, OIDC_START_LIMIT)
        conn.execute("DELETE FROM oidc_flows WHERE expires_at <= ?", (int(time.time()),))
        if conn.execute("SELECT COUNT(*) FROM oidc_flows").fetchone()[0] >= 1024:
            raise HTTPException(503, "登录请求过多，请稍后重试")
        _count_login_attempt(conn, bucket)
        conn.execute(
            "INSERT INTO oidc_flows VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_session_hash(state), _session_hash(browser), nonce, verifier, redirect_uri,
             _oidc_settings_hash(settings), int(time.time()) + OIDC_FLOW_TTL,
             _session_hash(request.cookies["lexvault_session"]) if request.cookies.get("lexvault_session") else "",
             bucket, conn.execute("SELECT expires_at FROM auth_login_limits WHERE bucket=?", (bucket,)).fetchone()[0]),
        )
    return state, nonce, browser, verifier


def _consume_oidc_flow(state: str, browser: str):
    if not state or not browser or len(state) > 128 or len(browser) > 128:
        return None
    with transaction() as conn:
        # One statement claims and deletes the flow across independent workers.
        return conn.execute(
            "DELETE FROM oidc_flows WHERE state_hash=? AND browser_hash=? AND expires_at > ? RETURNING *",
            (_session_hash(state), _session_hash(browser), int(time.time())),
        ).fetchone()


def _clear_oidc_cookie(response: Response) -> None:
    response.delete_cookie(OIDC_FLOW_COOKIE, path=OIDC_CALLBACK_PATH, httponly=True, samesite="lax")


def _oidc_failure(request: Request, error: str) -> Response:
    record_security_event("auth", "login_failed", detail="OIDC " + error, request_path=request.url.path)
    redirect = RedirectResponse(url="/?auth_error=" + error, status_code=302)
    _clear_oidc_cookie(redirect)
    return redirect


@router.get("/oidc/login")
def oidc_login(request: Request):
    try:
        settings = oidc_settings()
        if auth_mode() != "token" or settings is None:
            return _oidc_failure(request, "unavailable")
        redirect_uri = str(request.base_url).rstrip("/") + OIDC_CALLBACK_PATH
        state, nonce, browser, verifier = _register_oidc_flow(request, settings, redirect_uri)
        discovery = oidc_discovery(settings["issuer"])
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": settings["client_id"], "redirect_uri": redirect_uri,
            "scope": os.getenv("LAW_REVIEW_OIDC_SCOPES", "openid profile"), "state": state, "nonce": nonce,
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        })
        redirect = RedirectResponse(url=f"{discovery['authorization_endpoint']}?{query}", status_code=302)
        redirect.set_cookie(OIDC_FLOW_COOKIE, browser, max_age=OIDC_FLOW_TTL, httponly=True,
                            secure=request.url.scheme == "https" or request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"},
                            samesite="lax", path=OIDC_CALLBACK_PATH)
        return redirect
    except HTTPException:
        return _oidc_failure(request, "unavailable")
    except Exception as exc:
        logger.warning("OIDC login failed (%s)", type(exc).__name__)
        return _oidc_failure(request, "unavailable")


@router.get("/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    try:
        flow = _consume_oidc_flow(state, request.cookies.get(OIDC_FLOW_COOKIE, ""))
        settings = oidc_settings()
        if auth_mode() != "token" or settings is None:
            return _oidc_failure(request, "unavailable")
        if flow is None or flow["settings_hash"] != _oidc_settings_hash(settings):
            return _oidc_failure(request, "state")
        if error or not code or len(code) > 4096:
            return _oidc_failure(request, "failed")
        discovery = oidc_discovery(settings["issuer"])
        token_response = fetch_json(discovery["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"],
            "client_id": settings["client_id"], "client_secret": settings["client_secret"],
            "code_verifier": flow["verifier"],
        })
        id_token = token_response.get("id_token")
        if not id_token:
            raise ValueError("missing id token")
        claims = oidc_verify_id_token(id_token, settings, discovery, flow["nonce"])
        subject = claims.get("sub")
        principal = settings["principals"].get(subject) if isinstance(subject, str) else None
        if principal is None:
            return _oidc_failure(request, "denied")
        session = _register_oidc_session(subject, flow)
    except HTTPException:
        # Revocation/storage failures must remain 503, never a successful login.
        response = JSONResponse({"detail": "会话服务暂不可用"}, status_code=503)
        _clear_oidc_cookie(response)
        return response
    except Exception as exc:
        logger.warning("OIDC callback failed (%s)", type(exc).__name__)
        return _oidc_failure(request, "failed")
    record_security_event("auth", "login_success", actor=principal.name, request_path=request.url.path)
    redirect = RedirectResponse(url="/", status_code=302)
    _issue_session_cookie(redirect, request, session)
    _clear_oidc_cookie(redirect)
    return redirect
