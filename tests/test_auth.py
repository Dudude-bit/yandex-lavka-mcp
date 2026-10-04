"""Tests for the provider-agnostic OAuth JWT verifier."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from yandex_lavka_mcp.auth import JwksTokenVerifier, build_token_verifier

ISSUER = "https://auth.example.com"


@pytest.fixture()
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _verifier(public_key, *, audience=None, required_scopes=None, allowed_subjects=None):
    v = JwksTokenVerifier(
        issuer=ISSUER,
        jwks_url="https://auth.example.com/jwks",
        resource_url="https://lavka.example.com/mcp",
        audience=audience,
        required_scopes=required_scopes or [],
        allowed_subjects=allowed_subjects,
    )

    class _Key:
        key = public_key

    class _Jwks:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    # Avoid any network: hand back our public key for every token.
    v._jwk_client = _Jwks()
    return v


def _token(private_key, **claims):
    payload = {"iss": ISSUER, "sub": "user-1", "exp": int(time.time()) + 300, **claims}
    if payload.get("exp") is None:
        payload.pop("exp", None)
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


async def test_token_without_exp_rejected(keypair):
    private, public = keypair
    v = _verifier(public)
    assert await v.verify_token(_token(private, exp=None)) is None


async def test_subject_allowlist(keypair):
    private, public = keypair
    v = _verifier(public, allowed_subjects=["allowed-sub"])
    assert await v.verify_token(_token(private, sub="allowed-sub")) is not None
    assert await v.verify_token(_token(private, sub="someone-else")) is None


@respx.mock
async def test_jwks_discovery_retried_after_provider_was_down():
    # The server booted before its auth provider: discovery failed, and a guessed
    # JWKS URL used to be cached for good, rejecting every token afterwards.
    discovery = ISSUER + "/.well-known/openid-configuration"
    keys = "http://127.0.0.1:9/keys"  # refuses fast, no real network
    v = JwksTokenVerifier(issuer=ISSUER, jwks_url=None, resource_url=None, audience=None, required_scopes=[])

    respx.get(discovery).mock(return_value=httpx.Response(502))
    assert await v.verify_token("a.b.c") is None
    assert v._jwk_client is None  # nothing guessed and kept

    respx.get(discovery).mock(return_value=httpx.Response(200, json={"jwks_uri": keys}))
    assert await v.verify_token("a.b.c") is None
    assert v._jwk_client.uri == keys
