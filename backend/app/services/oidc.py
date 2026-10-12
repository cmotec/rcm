"""OpenID Connect relying party (authorization code flow + PKCE).

Tested against Authentik. Works with any provider that publishes discovery
metadata and JWKS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import Settings
from app.errors import Unauthenticated, UpstreamError
from app.logging import get_logger

log = get_logger(__name__)

STATE_COOKIE = "oidc_state"
STATE_TTL_SECONDS = 600
ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


@dataclass
class LoginState:
    state: str
    nonce: str
    verifier: str
    return_to: str
    exp: float

    def dump(self) -> bytes:
        return json.dumps(self.__dict__).encode()

    @classmethod
    def load(cls, raw: bytes) -> LoginState:
        return cls(**json.loads(raw))


@dataclass
class OidcIdentity:
    issuer: str
    subject: str
    username: str
    email: str | None
    display_name: str | None
    groups: list[str]
    id_token: str
    refresh_token: str | None
    claims: dict[str, Any]


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def safe_return_to(value: str | None) -> str:
    """Only allow same-origin relative paths (open-redirect protection)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


class OidcClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        verify: bool | str = settings.OIDC_VERIFY_TLS
        if verify and settings.TLS_CA_BUNDLE:
            verify = settings.TLS_CA_BUNDLE
        self._http = http or httpx.AsyncClient(timeout=10.0, verify=verify)
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- metadata
    async def metadata(self) -> dict[str, Any]:
        if self._metadata is None or time.monotonic() - self._metadata_at > 3600:
            url = self.settings.OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise UpstreamError(f"OIDC discovery failed: {exc}") from exc
            self._metadata = resp.json()
            self._metadata_at = time.monotonic()
        return self._metadata

    async def _jwk_set(self, force: bool = False) -> dict[str, Any]:
        if force or self._jwks is None or time.monotonic() - self._jwks_at > 3600:
            meta = await self.metadata()
            try:
                resp = await self._http.get(meta["jwks_uri"])
                resp.raise_for_status()
            except (httpx.HTTPError, KeyError) as exc:
                raise UpstreamError(f"OIDC JWKS fetch failed: {exc}") from exc
            self._jwks = resp.json()
            self._jwks_at = time.monotonic()
        return self._jwks

    async def _signing_key(self, kid: str | None) -> Any:
        for attempt in (False, True):
            jwks = await self._jwk_set(force=attempt)
            keys = jwks.get("keys", [])
            for k in keys:
                if kid is None or k.get("kid") == kid:
                    if k.get("use", "sig") == "sig":
                        return jwt.PyJWK(k).key
        raise Unauthenticated("ID token signed with unknown key")

    # ---------------------------------------------------------------- flow
    async def authorization_url(self, return_to: str | None) -> tuple[str, LoginState]:
        meta = await self.metadata()
        verifier, challenge = _pkce_pair()
        st = LoginState(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            verifier=verifier,
            return_to=safe_return_to(return_to),
            exp=time.time() + STATE_TTL_SECONDS,
        )
        params = {
            "response_type": "code",
            "client_id": self.settings.OIDC_CLIENT_ID,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "scope": self.settings.OIDC_SCOPES,
            "state": st.state,
            "nonce": st.nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return meta["authorization_endpoint"] + "?" + urlencode(params), st

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        meta = await self.metadata()
        auth = None
        if self.settings.OIDC_CLIENT_SECRET:
            auth = (self.settings.OIDC_CLIENT_ID, self.settings.OIDC_CLIENT_SECRET)
        else:
            data = {**data, "client_id": self.settings.OIDC_CLIENT_ID}
        try:
            resp = await self._http.post(meta["token_endpoint"], data=data, auth=auth)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"OIDC token endpoint unreachable: {exc}") from exc
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", "error")
            except ValueError:
                err = f"http {resp.status_code}"
            raise Unauthenticated(f"OIDC token request rejected: {err}", code="oidc_rejected")
        return resp.json()

    async def exchange_code(self, code: str, st: LoginState) -> OidcIdentity:
        tokens = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.oidc_redirect_uri,
                "code_verifier": st.verifier,
            }
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise Unauthenticated("OIDC provider returned no ID token")
        claims = await self.validate_id_token(id_token, nonce=st.nonce)
        if self.settings.OIDC_GROUPS_CLAIM not in claims or self.settings.OIDC_USERNAME_CLAIM not in claims:
            claims = {**(await self.userinfo(tokens.get("access_token"))), **claims}
        return self._identity(claims, id_token, tokens.get("refresh_token"))

    async def validate_id_token(self, id_token: str, *, nonce: str | None) -> dict[str, Any]:
        meta = await self.metadata()
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise Unauthenticated("malformed ID token") from exc
        if header.get("alg") not in ALLOWED_ALGS:
            raise Unauthenticated("ID token uses a disallowed algorithm")
        key = await self._signing_key(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[header["alg"]],
                audience=self.settings.OIDC_CLIENT_ID,
                issuer=meta["issuer"],
                leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise Unauthenticated(f"ID token invalid: {exc}") from exc
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.settings.OIDC_CLIENT_ID:
            raise Unauthenticated("ID token azp mismatch")
        if nonce is not None and claims.get("nonce") != nonce:
            raise Unauthenticated("ID token nonce mismatch")
        return claims

    async def userinfo(self, access_token: str | None) -> dict[str, Any]:
        meta = await self.metadata()
        if not access_token or "userinfo_endpoint" not in meta:
            return {}
        try:
            resp = await self._http.get(
                meta["userinfo_endpoint"], headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("oidc_userinfo_failed", error=str(exc)[:200])
            return {}

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def end_session_url(self, id_token: str | None) -> str | None:
        meta = await self.metadata()
        endpoint = meta.get("end_session_endpoint")
        if not endpoint:
            return None
        params: dict[str, str] = {
            "post_logout_redirect_uri": self.settings.OIDC_POST_LOGOUT_REDIRECT_URL or self.settings.APP_URL + "/",
            "client_id": self.settings.OIDC_CLIENT_ID,
        }
        if id_token:
            params["id_token_hint"] = id_token
        return endpoint + "?" + urlencode(params)

    def _identity(self, claims: dict[str, Any], id_token: str, refresh_token: str | None) -> OidcIdentity:
        groups_raw = claims.get(self.settings.OIDC_GROUPS_CLAIM) or []
        if isinstance(groups_raw, str):
            groups_raw = [groups_raw]
        groups = [g for g in groups_raw if isinstance(g, str)]
        username = (
            claims.get(self.settings.OIDC_USERNAME_CLAIM)
            or claims.get("preferred_username")
            or claims.get("email")
            or claims["sub"]
        )
        return OidcIdentity(
            issuer=claims["iss"],
            subject=claims["sub"],
            username=str(username),
            email=claims.get("email"),
            display_name=claims.get("name"),
            groups=groups,
            id_token=id_token,
            refresh_token=refresh_token,
            claims=claims,
        )


_client: OidcClient | None = None


def get_oidc_client(settings: Settings) -> OidcClient:
    global _client
    if _client is None:
        _client = OidcClient(settings)
    return _client


def set_oidc_client(client: OidcClient | None) -> None:
    global _client
    _client = client
