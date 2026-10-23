"""Enterprise auth exercised through HTTP with isolated SQLite and a signed mock IdP."""
import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, security

TOKEN = "enterprise-test-token-" * 3


@pytest.fixture
def enterprise(tmp_path, monkeypatch):
    monkeypatch.setenv("LAW_REVIEW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "token")
    monkeypatch.setenv("LAW_REVIEW_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", json.dumps({TOKEN: {"name": "Lawyer", "permissions": ["view"]}}))
    for key in tuple(os.environ):
        if key.startswith("LAW_REVIEW_OIDC_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LAW_REVIEW_OIDC_ISSUER", "https://enterprise.example")
    monkeypatch.setenv("LAW_REVIEW_OIDC_CLIENT_ID", "client")
    monkeypatch.setenv("LAW_REVIEW_OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("LAW_REVIEW_OIDC_PRINCIPALS_JSON", json.dumps({"known": {"name": "SSO Lawyer", "permissions": ["view"]}}))
    db.init_db(seed=False)
    app = FastAPI()
    app.include_router(security.router)
    app.add_middleware(security.AccessMiddleware)
    with TestClient(app, follow_redirects=False) as client:
        yield client, app


@pytest.fixture
def provider(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test"
    observed = {"nonce": "", "sub": "known", "claims": {}, "data": None}
    discovery = {"authorization_endpoint": "https://enterprise.example/authorize",
                 "token_endpoint": "https://enterprise.example/token", "jwks_uri": "https://enterprise.example/jwks"}

    def fetch(url, *, data=None, timeout=5):
        if url.endswith("openid-configuration"):
            return discovery
        if url.endswith("jwks"):
            return {"keys": [jwk]}
        assert url.endswith("token")
        observed["data"] = data
        claims = {"iss": "https://enterprise.example", "aud": "client", "sub": observed["sub"],
                  "exp": int(security.time.time()) + 300, "nonce": observed["nonce"], **observed["claims"]}
        return {"id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})}

    monkeypatch.setattr(security, "fetch_json", fetch)
    monkeypatch.setattr(security, "_oidc_discovery_cache", {})
    return observed


def start(client, provider):
    response = client.get("/api/auth/oidc/login")
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    provider["nonce"] = query["nonce"][0]
    return response, query


def callback(client, query, **kwargs):
    return client.get("/api/auth/oidc/callback", params={"code": "code", "state": query["state"][0]}, **kwargs)


def assert_failure(response, error):
    assert response.status_code == 302
    assert response.headers["location"] == "/?auth_error=" + error
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "lexvault_session=" not in response.headers["set-cookie"]


def test_public_identity_plain_text_and_no_secrets(enterprise, monkeypatch):
    client, _ = enterprise
    monkeypatch.setenv("LAW_REVIEW_ORGANIZATION_NAME", " <b>Law Firm</b> ")
    monkeypatch.setenv("LAW_REVIEW_SUPPORT_CONTACT", " support@example.test ")
    result = client.get("/api/auth/me")
    assert result.json()["organization_name"] == "<b>Law Firm</b>"
    assert result.json()["support_contact"] == "support@example.test"
    assert result.json()["oidc_enabled"] is True
    assert result.json()["authenticated"] is False
    assert "secret" not in result.text and "enterprise.example" not in result.text
    assert result.headers["cache-control"] == "no-store"
    for key in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv("LAW_REVIEW_OIDC_" + key)
    assert client.get("/api/auth/me").json()["oidc_enabled"] is False


def test_cross_site_callback_pkce_success_and_replay(enterprise, provider):
    client, _ = enterprise
    response, query = start(client, provider)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=600" in cookie
    assert "Path=/api/auth/oidc/callback" in cookie
    assert query["code_challenge_method"] == ["S256"]
    response = callback(client, query, headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://enterprise.example"})
    assert response.headers["location"] == "/"
    verifier = provider["data"]["code_verifier"]
    assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["name"] == "SSO Lawyer"
    assert_failure(callback(client, query), "state")


def test_flow_cannot_be_transferred_without_browser_cookie(enterprise, provider):
    client, app = enterprise
    _, query = start(client, provider)
    with TestClient(app, follow_redirects=False) as other:
        assert_failure(callback(other, query), "state")
    assert provider["data"] is None
    assert callback(client, query).headers["location"] == "/"


def test_flow_is_durable_and_atomically_consumed(enterprise, provider):
    client, _ = enterprise
    _, query = start(client, provider)
    browser = client.cookies.get(security.OIDC_FLOW_COOKIE)
    state = query["state"][0]
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lambda _: security._consume_oidc_flow(state, browser), range(8)))
    assert sum(row is not None for row in rows) == 1
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM oidc_flows").fetchone()[0] == 0


def test_expired_flow_and_provider_denial_are_sanitized(enterprise, provider):
    client, _ = enterprise
    _, query = start(client, provider)
    with db.transaction() as conn:
        conn.execute("UPDATE oidc_flows SET expires_at=?", (int(security.time.time()),))
    assert_failure(callback(client, query), "state")
    _, query = start(client, provider)
    response = client.get("/api/auth/oidc/callback", params={"state": query["state"][0], "error": "PROVIDER_SECRET", "error_description": "PRIVATE"})
    assert_failure(response, "failed")
    assert "PRIVATE" not in response.text and "PROVIDER_SECRET" not in str(response.headers)
    assert provider["data"] is None


@pytest.mark.parametrize("claims", [{"aud": "other"}, {"iss": "https://evil.example"}, {"exp": 1}, {"nonce": "wrong"}])
def test_signed_invalid_claims_fail_closed(enterprise, provider, claims):
    client, _ = enterprise
    _, query = start(client, provider)
    provider["claims"] = claims
    assert_failure(callback(client, query), "failed")


def test_unmapped_subject_not_email_is_authorized(enterprise, provider):
    client, _ = enterprise
    _, query = start(client, provider)
    provider["sub"] = "unknown"
    provider["claims"] = {"email": "known"}
    assert_failure(callback(client, query), "denied")


@pytest.mark.parametrize("method,path", [("GET", "/api/auth/me"), ("GET", "/api/auth/oidc/login"),
    ("GET", "/api/cases/1/export"), ("POST", "/api/auth/oidc/callback"), ("GET", "/api/auth/oidc/callback/")])
def test_cross_site_exception_is_exact(enterprise, method, path):
    client, _ = enterprise
    assert client.request(method, path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_logout_and_rotation_storage_failure_return_503(enterprise, monkeypatch):
    client, _ = enterprise
    assert client.post("/api/auth/session", json={"token": TOKEN}).status_code == 200
    cookie = client.cookies.get("lexvault_session")
    def fail():
        raise OSError("PRIVATE DATABASE ERROR")
    with monkeypatch.context() as context:
        context.setattr(security, "transaction", fail)
        for response in (client.delete("/api/auth/session"), client.post("/api/auth/session", json={"token": TOKEN})):
            assert response.status_code == 503
            assert "logged_out" not in response.text and "PRIVATE" not in response.text
            assert "set-cookie" not in response.headers
    assert security.session_principal(cookie) is not None


def test_idle_refresh_boundary_and_absolute_expiry(enterprise, monkeypatch):
    client, _ = enterprise
    clock = [100000]
    monkeypatch.setattr(security.time, "time", lambda: clock[0])
    session = security._register_session(security._session_hash(TOKEN))
    for offset in range(0, security.SESSION_TTL, 1700):
        clock[0] = 100000 + offset
        assert security.session_principal(session) is not None
    clock[0] = 100000 + security.SESSION_TTL
    assert security.session_principal(session) is None
    session = security._register_session(security._session_hash(TOKEN))
    clock[0] += security.SESSION_IDLE_TTL
    assert security.session_principal(session) is None


def test_failure_rate_counter_shared_by_clients_and_expires(enterprise, monkeypatch):
    client, app = enterprise
    for _ in range(security.LOGIN_LIMIT):
        assert client.post("/api/auth/session", json={"token": "wrong"}).status_code == 401
    with TestClient(app) as other:
        assert other.post("/api/auth/session", json={"token": TOKEN}, headers={"X-Forwarded-For": "spoofed"}).status_code == 429
    with db.transaction() as conn:
        conn.execute("UPDATE auth_login_limits SET expires_at=0")
    assert client.post("/api/auth/session", json={"token": TOKEN}).status_code == 200


def test_oidc_start_rate_limit_is_shared(enterprise, provider):
    client, app = enterprise
    for _ in range(security.OIDC_START_LIMIT):
        start(client, provider)
    with TestClient(app, follow_redirects=False) as other:
        assert_failure(other.get("/api/auth/oidc/login"), "unavailable")


def test_oidc_storage_failure_clears_cookie_and_keeps_503(enterprise, provider, monkeypatch):
    client, _ = enterprise
    _, query = start(client, provider)
    from fastapi import HTTPException
    def fail(subject, flow):
        raise HTTPException(503, "PRIVATE")
    monkeypatch.setattr(security, "_register_oidc_session", fail)
    response = callback(client, query)
    assert response.status_code == 503
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "PRIVATE" not in response.text
    assert not client.get("/api/auth/me").json()["authenticated"]


def test_parallel_failures_cannot_bypass_limit(enterprise):
    _, app = enterprise
    def attempt(_):
        with TestClient(app) as client:
            return client.post("/api/auth/session", json={"token": "wrong"}).status_code
    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(attempt, range(16)))
    assert statuses.count(401) == security.LOGIN_LIMIT
    assert statuses.count(429) == 16 - security.LOGIN_LIMIT
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT attempts FROM auth_login_limits").fetchone()[0] == security.LOGIN_LIMIT


def test_discovery_failure_is_rate_limited_and_sanitized(enterprise, monkeypatch):
    client, _ = enterprise
    calls = []
    def fail(issuer):
        calls.append(issuer)
        raise ValueError("PRIVATE PROVIDER BODY")
    monkeypatch.setattr(security, "oidc_discovery", fail)
    for _ in range(security.OIDC_START_LIMIT + 2):
        response = client.get("/api/auth/oidc/login")
        assert_failure(response, "unavailable")
        assert "PRIVATE" not in response.text
    assert len(calls) == security.OIDC_START_LIMIT


def test_settings_change_invalidates_flow(enterprise, provider, monkeypatch):
    client, _ = enterprise
    _, query = start(client, provider)
    monkeypatch.setenv("LAW_REVIEW_OIDC_CLIENT_ID", "new-client")
    assert_failure(callback(client, query), "state")
    assert provider["data"] is None


def test_v10_migration_preserves_absolute_lifetime(tmp_path, monkeypatch):
    monkeypatch.setenv("LAW_REVIEW_DATA_DIR", str(tmp_path))
    with monkeypatch.context() as context:
        context.setattr(db, "SCHEMA_VERSION", 10)
        db.init_db(seed=False)
    with db.transaction() as conn:
        conn.execute("INSERT INTO auth_sessions VALUES ('hash','token','credential',NULL,1234567890,'old')")
    db.init_db(seed=False)
    db.init_db(seed=False)
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM auth_sessions").fetchone()
        assert row["expires_at"] == 1234567890
        assert row["last_seen_at"] > 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_cross_site_missing_strict_cookie_rotates_originating_session(enterprise, provider):
    client, app = enterprise
    assert client.post("/api/auth/session", json={"token": TOKEN}).status_code == 200
    old_session = client.cookies.get("lexvault_session")
    _, query = start(client, provider)
    browser = client.cookies.get(security.OIDC_FLOW_COOKIE)
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM oidc_flows").fetchone()
        assert row["previous_session_hash"] == security._session_hash(old_session)
        assert old_session not in repr(tuple(row)) and TOKEN not in repr(tuple(row))
    # TestClient does not implement SameSite: explicitly omit the Strict cookie,
    # as a browser does on a cross-site top-level IdP navigation.
    with TestClient(app, follow_redirects=False) as returning:
        returning.cookies.set(security.OIDC_FLOW_COOKIE, browser, path=security.OIDC_CALLBACK_PATH)
        response = callback(returning, query, headers={"Sec-Fetch-Site": "cross-site"})
        assert "lexvault_session" not in response.request.headers.get("cookie", "")
        assert response.headers["location"] == "/"
        assert returning.get("/api/auth/me").json()["name"] == "SSO Lawyer"
    assert security.session_principal(old_session) is None


def test_same_nat_many_successes_refund_only_own_reservation(enterprise, provider):
    client, _ = enterprise
    # An abandoned request remains charged even after other successful logins.
    start(client, provider)
    for _ in range(25):
        _, query = start(client, provider)
        assert callback(client, query).headers["location"] == "/"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT attempts FROM auth_login_limits").fetchone()[0] == 1


def test_rotation_failure_rolls_back_old_session_and_refund(enterprise, provider):
    client, _ = enterprise
    client.post("/api/auth/session", json={"token": TOKEN})
    old_session = client.cookies.get("lexvault_session")
    _, query = start(client, provider)
    with db.transaction() as conn:
        conn.execute("CREATE TRIGGER reject_oidc BEFORE INSERT ON auth_sessions WHEN NEW.kind='oidc' BEGIN SELECT RAISE(ABORT, 'fail'); END")
    response = callback(client, query)
    assert response.status_code == 503
    assert security.session_principal(old_session) is not None
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT attempts FROM auth_login_limits").fetchone()[0] == 1


def test_late_success_does_not_refund_a_new_window(enterprise, provider):
    client, _ = enterprise
    _, query = start(client, provider)
    with db.transaction() as conn:
        conn.execute("UPDATE auth_login_limits SET attempts=7, expires_at=expires_at+100")
    assert callback(client, query).headers["location"] == "/"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT attempts FROM auth_login_limits").fetchone()[0] == 7
