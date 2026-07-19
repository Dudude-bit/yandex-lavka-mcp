"""Tests for the provider-agnostic OAuth JWT verifier."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from yandex_lavka_mcp.auth import JwksTokenVerifier, build_token_verifier

ISSUER = "https://auth.example.com"


@pytest.fixture()
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _verifier(public_key, *, audience=None, required_scopes=None):
    v = JwksTokenVerifier(
        issuer=ISSUER,
        jwks_url="https://auth.example.com/jwks",
        resource_url="https://lavka.example.com/mcp",
        audience=audience,
        required_scopes=required_scopes or [],
    )

    class _Key:
        key = public_key

    # Avoid any network: hand back our public key for every token.
    v._jwk_client.get_signing_key_from_jwt = lambda token: _Key()
    return v


def _token(private_key, **claims):
    payload = {"iss": ISSUER, "sub": "user-1", "exp": int(time.time()) + 300, **claims}
    return jwt.encode(payload, private_key, algorithm="RS256")


def test_build_token_verifier_disabled_without_env(monkeypatch):
    monkeypatch.delenv("YANDEX_LAVKA_MCP_OAUTH_ISSUER", raising=False)
    assert build_token_verifier() is None


async def test_valid_token_accepted(keypair):
    private, public = keypair
    v = _verifier(public)
    access = await v.verify_token(_token(private, scope="openid lavka"))
    assert access is not None
    assert access.subject == "user-1"
    assert "lavka" in access.scopes


async def test_wrong_issuer_rejected(keypair):
    private, public = keypair
    v = _verifier(public)
    assert await v.verify_token(_token(private, iss="https://evil.example.com")) is None


async def test_expired_token_rejected(keypair):
    private, public = keypair
    v = _verifier(public)
    assert await v.verify_token(_token(private, exp=int(time.time()) - 10)) is None


async def test_missing_required_scope_rejected(keypair):
    private, public = keypair
    v = _verifier(public, required_scopes=["lavka"])
    assert await v.verify_token(_token(private, scope="openid")) is None


async def test_garbage_token_rejected(keypair):
    _, public = keypair
    v = _verifier(public)
    assert await v.verify_token("not-a-jwt") is None
