"""OIDC login against a mocked identity provider (real RS256 signatures, discovery, JWKS, PKCE, nonce)."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.oidc import OidcClient, set_oidc_client

ISSUER = "https://idp.example/application/o/app/"


class FakeIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
        self.nonce: str | None = None
        self.claims: dict[str, Any] = {}
        self.token_requests: list[dict[str, list[str]]] = []
        self.override_nonce: str | None = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": "https://idp.example/application/o/authorize/",
                "token_endpoint": "https://idp.example/application/o/token/",
                "userinfo_endpoint": "https://idp.example/application/o/userinfo/",
                "end_session_endpoint": "https://idp.example/application/o/app/end-session/",
                "jwks_uri": "https://idp.example/application/o/app/jwks/",
            })
        if path.endswith("/jwks/"):
            return httpx.Response(200, json=self.jwks)
        if path.endswith("/token/"):
            form = parse_qs(req.read().decode())
            self.token_requests.append(form)
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": "app-client", "sub": "user-123", "iat": now, "exp": now + 300,
                      "nonce": self.override_nonce or self.nonce, "preferred_username": "alice",
                      "email": "alice@example.com", "name": "Alice", **self.claims}
            token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": token, "access_token": "at", "refresh_token": "rt",
                                             "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def idp(settings, monkeypatch: pytest.MonkeyPatch, client):  # noqa: ANN001, ANN201
    fake = FakeIdp()
    for k, v in {"OIDC_ENABLED": True, "OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": "app-client",
                 "OIDC_CLIENT_SECRET": "client-secret", "OIDC_USER_GROUPS": "app-users",
                 "OIDC_ADMIN_GROUPS": "app-admins", "OIDC_DEFAULT_ROLE": "none"}.items():
        monkeypatch.setattr(settings, k, v)
    set_oidc_client(OidcClient(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))))
    client.cookies.clear()
    yield fake
    set_oidc_client(None)
    client.cookies.clear()


def _start_login(client, idp: FakeIdp, return_to: str = "/workspace") -> dict[str, str]:  # noqa: ANN001
    r = client.get("/api/v1/auth/oidc/login", params={"return_to": return_to}, follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert loc.netloc == "idp.example" and q["code_challenge_method"] == "S256" and q["client_id"] == "app-client"
    assert q["redirect_uri"] == "http://testserver/api/v1/auth/oidc/callback"
    idp.nonce = q["nonce"]
    return q


def test_oidc_login_provisions_user_with_mapped_role(client, idp: FakeIdp) -> None:  # noqa: ANN001
    idp.claims = {"groups": ["app-users", "unrelated"]}
    q = _start_login(client, idp)
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "the-code", "state": q["state"]},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "http://testserver/workspace"
    form = idp.token_requests[-1]
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["the-code"] and form["code_verifier"]
    me = client.get("/api/v1/auth/me").json()
    assert me["username"] == "alice" and me["roles"] == ["user"] and me["auth_method"] == "oidc"

    # logout returns the IdP end-session URL with the id_token hint
    out = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]}).json()
    assert out["redirect"].startswith("https://idp.example/application/o/app/end-session/?")
    assert "id_token_hint=" in out["redirect"]
    assert client.get("/api/v1/auth/me").status_code == 401


def test_oidc_rejects_user_without_role(client, idp: FakeIdp) -> None:  # noqa: ANN001
    idp.claims = {"sub": "user-456", "preferred_username": "bob", "groups": ["nobody"]}
    q = _start_login(client, idp)
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"].endswith("/login?error=no_role")


def test_oidc_state_and_nonce_are_enforced(client, idp: FakeIdp) -> None:  # noqa: ANN001
    q = _start_login(client, idp)
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.headers["location"].endswith("error=state_mismatch")
    q = _start_login(client, idp)
    idp.override_nonce = "replayed-nonce"
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"].endswith("error=token_invalid")
    client.cookies.clear()
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"].endswith("error=state_missing")


def test_open_redirect_is_prevented(client, idp: FakeIdp) -> None:  # noqa: ANN001
    idp.claims = {"groups": ["app-admins"], "sub": "user-789", "preferred_username": "carol"}
    q = _start_login(client, idp, return_to="https://evil.example/")
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "http://testserver/"
    assert client.get("/api/v1/auth/me").json()["is_admin"] is True
