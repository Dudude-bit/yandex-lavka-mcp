"""Provider-agnostic OAuth 2.1 / OIDC bearer-token protection for the HTTP server.

Enabled only when ``YANDEX_LAVKA_MCP_OAUTH_ISSUER`` is set — otherwise the server
runs unauthenticated (fine for local stdio, or behind Claude Code's own header
auth). It works with ANY OpenID-Connect provider (Zitadel, Keycloak, Auth0,
Google, …): tokens are validated as JWTs against the provider's JWKS, and the
authorization server is advertised via OAuth protected-resource metadata so a
claude.ai custom connector can discover it and run the login flow.

Configuration (env vars):
  YANDEX_LAVKA_MCP_OAUTH_ISSUER    Authorization server issuer URL (required to enable).
  YANDEX_LAVKA_MCP_SERVER_URL      Public URL of THIS MCP server (the resource).
  YANDEX_LAVKA_MCP_OAUTH_AUDIENCE  Expected token audience (optional; validated if set).
  YANDEX_LAVKA_MCP_OAUTH_SCOPES    Space-separated required scopes (optional).
  YANDEX_LAVKA_MCP_OAUTH_JWKS_URL  Override JWKS URL (optional; else discovered from issuer).
"""

from __future__ import annotations

import os
import sys

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings


def _discover_jwks_url(issuer: str) -> str:
    """Read jwks_uri from the provider's OpenID discovery document.

    Raises on failure — no guessed fallback: a wrong URL would be cached for the
    process lifetime and reject every token (happened when the server booted
    before the auth provider)."""
    well_known = issuer.rstrip("/") + "/.well-known/openid-configuration"
    resp = httpx.get(well_known, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["jwks_uri"]


def _scopes_from_claims(claims: dict) -> list[str]:
    scope = claims.get("scope") or claims.get("scp") or claims.get("scopes")
    if isinstance(scope, str):
        return scope.split()
    if isinstance(scope, list):
        return [str(s) for s in scope]
    return []


class JwksTokenVerifier(TokenVerifier):
    """Validates JWT access tokens against a provider's JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        jwks_url: str | None,
        resource_url: str | None,
        audience: str | None,
        required_scopes: list[str],
        allowed_subjects: list[str] | None = None,
    ) -> None:
        self.issuer = issuer
        self.resource_url = resource_url
        self.audience = audience
        self.required_scopes = required_scopes
        # Optional allow-list of token `sub`s. When set, only these identities may
        # call the server — the last line of defence given every request spends
        # the one deployer's Lavka session.
        self.allowed_subjects = allowed_subjects or []
        self._jwks_url = jwks_url
        self._jwk_client = None
        if jwks_url:
            self._jwks()

    def _jwks(self):
        # Discovered on first use and retried until it succeeds, so a server that
        # starts before its auth provider recovers on its own.
        if self._jwk_client is None:
            # Imported lazily so the base (stdio) install needs no JWT deps.
            from jwt import PyJWKClient

            self._jwk_client = PyJWKClient(self._jwks_url or _discover_jwks_url(self.issuer))
        return self._jwk_client

    async def verify_token(self, token: str) -> AccessToken | None:
        import jwt

        try:
            signing_key = self._jwks().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
                audience=self.audience if self.audience else None,
                issuer=self.issuer,
                # Require an expiry — reject tokens minted without `exp`.
                options={"verify_aud": bool(self.audience), "require": ["exp"]},
            )
        except Exception as e:  # noqa: BLE001 - any decode/verify failure = unauthorized
            print(f"[yandex-lavka-mcp] token rejected: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return None

        subject = claims.get("sub")
        # Set YANDEX_LAVKA_MCP_LOG_AUTH=1 to print accepted subjects to stderr —
        # use it once to discover your `sub`, then pin it in OAUTH_SUBJECTS.
        if os.environ.get("YANDEX_LAVKA_MCP_LOG_AUTH"):
            print(f"[yandex-lavka-mcp] authenticated subject={subject!r}", file=sys.stderr, flush=True)
        if self.allowed_subjects and str(subject) not in self.allowed_subjects:
            return None
        scopes = _scopes_from_claims(claims)
        if self.required_scopes and not set(self.required_scopes).issubset(scopes):
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or claims.get("aud") or ""),
            scopes=scopes,
            expires_at=claims.get("exp"),
            subject=str(claims.get("sub")) if claims.get("sub") else None,
            resource=self.resource_url,
            claims=claims,
        )

    def auth_settings(self) -> AuthSettings:
        kwargs: dict = {"issuer_url": self.issuer}
        if self.resource_url:
            kwargs["resource_server_url"] = self.resource_url
        if self.required_scopes:
            kwargs["required_scopes"] = self.required_scopes
        return AuthSettings(**kwargs)


def build_token_verifier() -> JwksTokenVerifier | None:
    """Construct the verifier from env, or None if OAuth is not configured."""
    issuer = os.environ.get("YANDEX_LAVKA_MCP_OAUTH_ISSUER")
    if not issuer:
        return None
    scopes = [s for s in (os.environ.get("YANDEX_LAVKA_MCP_OAUTH_SCOPES") or "").split() if s]
    subjects = [
        s for s in (os.environ.get("YANDEX_LAVKA_MCP_OAUTH_SUBJECTS") or "").replace(",", " ").split() if s
    ]
    return JwksTokenVerifier(
        issuer=issuer,
        jwks_url=os.environ.get("YANDEX_LAVKA_MCP_OAUTH_JWKS_URL"),
        resource_url=os.environ.get("YANDEX_LAVKA_MCP_SERVER_URL"),
        audience=os.environ.get("YANDEX_LAVKA_MCP_OAUTH_AUDIENCE"),
        required_scopes=scopes,
        allowed_subjects=subjects,
    )
