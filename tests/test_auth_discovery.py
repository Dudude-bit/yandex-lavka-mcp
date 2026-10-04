import asyncio

import httpx
import respx

from yandex_lavka_mcp.auth import JwksTokenVerifier

ISSUER = "https://auth.example.test"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
# Port 9 refuses connections, so the key fetch fails fast without network.
KEYS = "http://127.0.0.1:9/oauth/v2/keys"


@respx.mock
def test_jwks_discovery_retries_after_provider_was_down():
    v = JwksTokenVerifier(issuer=ISSUER, jwks_url=None, resource_url=None, audience=None, required_scopes=[])

    respx.get(DISCOVERY).mock(return_value=httpx.Response(502))
    assert asyncio.run(v.verify_token("a.b.c")) is None
    assert v._jwk_client is None  # nothing guessed and cached

    respx.get(DISCOVERY).mock(return_value=httpx.Response(200, json={"jwks_uri": KEYS}))
    assert asyncio.run(v.verify_token("a.b.c")) is None
    assert v._jwk_client.uri == KEYS
