"""Tests for the HTTP client against a mocked transport."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from yandex_lavka_mcp.client import LavkaClient
from yandex_lavka_mcp.config import Config, Location
from yandex_lavka_mcp.errors import LavkaAuthError


def _config() -> Config:
    return Config(
        cookies={"Session_id": "fake"},
        location=Location(lat=55.0, lon=37.0),
    )


_HOMEPAGE_HTML = (
    '<html><script id="__page_props__-data" type="application/json">'
    '{"csrfToken":"tok-123","x":1}</script></html>'
)


def _mock_homepage():
    return respx.get("https://lavka.yandex.ru/").mock(
        return_value=httpx.Response(200, text=_HOMEPAGE_HTML)
    )


@respx.mock
async def test_search_trims_and_sends_real_body():
    _mock_homepage()
    route = respx.post("https://lavka.yandex.ru/api/v1/providers/search/v3/lavka").mock(
        return_value=httpx.Response(
            200,
            json={
                "cacheProducts": [
                    {
                        "id": "hash1",
                        "deepLink": "moloko-slug",
                        "title": "Молоко",
                        "currentPrice": 89,
                        "amount": "1 л",
                        "available": True,
                    }
                ]
            },
        )
    )
    async with LavkaClient(_config()) as client:
        products = await client.search("молоко", limit=5)

    assert products == [
        {
            "id": "hash1",
            "slug": "moloko-slug",
            "title": "Молоко",
            "price": 89.0,
            "old_price": None,
            "quantity_label": "1 л",
            "in_stock": True,
        }
    ]
    body = json.loads(route.calls.last.request.content)
    assert body["text"] == "молоко"
    assert body["position"]["location"] == [37.0, 55.0]  # [lon, lat]
    assert body["productsLimit"] == 5
    assert body["depotType"] == "regular"
    # CSRF token pulled from the homepage and sent as a header
    assert route.calls.last.request.headers["x-csrf-token"] == "tok-123"
    assert route.calls.last.request.headers["x-lavka-web-city"] == "213"


@respx.mock
async def test_add_to_cart_reads_version_then_updates():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(
            200, json={"cartId": "cart-1", "cartVersion": 7, "items": []}
        )
    )
    update = respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/update").mock(
        return_value=httpx.Response(
            200,
            json={
                "cartId": "cart-1",
                "cartVersion": 8,
                "totalItemsPrice": "129",
                "totalPriceValue": "458",
                "items": [{"id": "hash1", "title": "Хлеб", "quantity": "1", "currentPrice": 129}],
            },
        )
    )
    async with LavkaClient(_config()) as client:
        cart = await client.add_to_cart("hash1", 1, price=129)

    assert cart["cart_version"] == 8
    assert cart["total"] == 458.0
    body = json.loads(update.calls.last.request.content)
    assert body["cartId"] == "cart-1"
    assert body["cartVersion"] == 7  # version read from the prior retrieve
    assert body["items"][0]["id"] == "hash1"
    assert body["items"][0]["quantity"] == "1"
    assert body["idempotencyToken"]  # a token was generated


@respx.mock
async def test_list_addresses_trims_array():
    _mock_homepage()
    respx.post(
        "https://lavka.yandex.ru/api/v1/providers/address/v1/get-favorite-addresses"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "addressId": "a1",
                    "address": {
                        "label": "Home",
                        "city": "Testville",
                        "street": "Test Street",
                        "house": "1",
                        "shortAddress": "Test Street, 1",
                        "location": [37.6, 55.7],  # [lon, lat]
                    },
                }
            ],
        )
    )
    async with LavkaClient(_config()) as client:
        addrs = await client.list_addresses()
    assert addrs[0]["label"] == "Home"
    assert addrs[0]["city"] == "Testville"
    assert addrs[0]["lon"] == 37.6
    assert addrs[0]["lat"] == 55.7


@respx.mock
async def test_resolve_address_suggest_then_geocode():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/geo/v1/suggest").mock(
        return_value=httpx.Response(
            200,
            json=[{"title": "улица Баумана, 1", "full": "Казань, улица Баумана, 1",
                   "position": [49.108, 55.795]}],
        )
    )
    geocode = respx.post("https://lavka.yandex.ru/api/v1/providers/geo/v1/geocode").mock(
        return_value=httpx.Response(
            200,
            json={"city": "Казань", "street": "улица Баумана", "house": "1",
                  "lat": 55.795, "lon": 49.108, "text": "Казань, улица Баумана, 1"},
        )
    )
    async with LavkaClient(_config()) as client:
        resolved = await client.resolve_address("Казань Баумана 1")
    assert resolved["city"] == "Казань"
    assert resolved["street"] == "улица Баумана"
    assert resolved["house"] == "1"
    assert resolved["lat"] == 55.795
    # reverse-geocode was called with the suggested point [lon, lat]
    body = json.loads(geocode.calls.last.request.content)
    assert body["point"] == {"lon": 49.108, "lat": 55.795}


@respx.mock
async def test_place_order_builds_submit_body_and_polls_payment():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(
            200,
            json={
                "cartId": "c1",
                "cartVersion": 3,
                "orderFlowVersion": "grocery_flow_v1",
                "paymentMethod": {"type": "card", "id": "card-test", "meta": {"card": {"system": "MIR"}}},
                "cashback": {"walletId": "w/abc"},
                "items": [{"id": "p1", "title": "X", "quantity": "1", "currentPrice": 100}],
                "totalItemsPrice": "100",
                "totalPriceValue": "219",
                "orderConditions": {"deliveryCost": "119"},
            },
        )
    )
    respx.get("https://lavka.yandex.ru/api/v1/providers/v2/service-info").mock(
        return_value=httpx.Response(200, json={"depotId": "1000000001"})
    )
    submit = respx.post("https://lavka.yandex.ru/api/v1/orders/submit").mock(
        return_value=httpx.Response(200, json={"data": {"orderId": "ord-1-grocery"}})
    )
    respx.post("https://lavka.yandex.ru/api/v1/providers/payments/v1/status").mock(
        return_value=httpx.Response(
            200, json={"status": "wait_user_action", "payload": {"redirectUrl": "https://3ds/x"}}
        )
    )

    cfg = _config()
    cfg.context["placeId"] = "ymapsbm1://geo?data=Z"
    cfg.context["country"] = "Россия"
    cfg.context["additionalData"] = {"city": "Testville", "street": "Test Street", "house": "1", "flat": "10"}
    async with LavkaClient(cfg) as client:
        result = await client.place_order(poll=1)

    assert result["order_id"] == "ord-1-grocery"
    assert result["payment_status"] == "wait_user_action"
    assert result["redirect_url"] == "https://3ds/x"
    body = json.loads(submit.calls.last.request.content)
    assert body["cartId"] == "c1"
    assert body["cartVersion"] == 3
    assert body["paymentMethodId"] == "card-test"
    assert body["cashback"] == {"walletId": "w/abc"}
    assert body["position"]["depotId"] == "1000000001"
    assert body["position"]["placeId"] == "ymapsbm1://geo?data=Z"
    assert body["position"]["flat"] == "10"
    assert body["depotOrderContext"]["depotType"] == "regular"


def _order_cart_route(*, version=3, subtotal="100", total="219"):
    return respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(
            200,
            json={
                "cartId": "c1",
                "cartVersion": version,
                "orderFlowVersion": "grocery_flow_v1",
                "paymentMethod": {"type": "card", "id": "card-test"},
                "cashback": {"walletId": "w/abc"},
                "items": [{"id": "p1", "title": "X", "quantity": "1", "currentPrice": 100}],
                "totalItemsPrice": subtotal,
                "totalPriceValue": total,
                "orderConditions": {"deliveryCost": "119"},
            },
        )
    )


@respx.mock
async def test_place_order_aborts_on_cart_version_drift():
    # No submit/service-info routes are mocked: the abort must happen BEFORE them.
    # (If the code wrongly proceeded, respx would raise "not mocked" and the
    # message assertion below would fail — so this proves nothing was charged.)
    _mock_homepage()
    _order_cart_route(version=5)  # live version differs from the previewed 3
    async with LavkaClient(_config()) as client:
        with pytest.raises(Exception) as ei:
            await client.place_order(confirmed_total=219, expected_cart_version=3, poll=0)
    assert "changed" in str(ei.value).lower()


@respx.mock
async def test_place_order_aborts_on_total_drift():
    _mock_homepage()
    _order_cart_route(version=3, total="999")  # live total differs from confirmed 219
    async with LavkaClient(_config()) as client:
        with pytest.raises(Exception) as ei:
            await client.place_order(confirmed_total=219, expected_cart_version=3, poll=0)
    assert "total changed" in str(ei.value).lower()


@respx.mock
async def test_place_order_aborts_when_cart_not_checkoutable():
    # No submit/service-info mocked: the abort must happen before them.
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(
            200,
            json={
                "cartId": "c1",
                "cartVersion": 3,
                "totalItemsPrice": "100",
                "totalPriceValue": "100",
                "availableForCheckout": False,
                "checkoutUnavailableReason": "quantity-over-limit",
                "items": [
                    {"id": "b", "title": "Голубика", "quantity": "2", "currentPrice": 169, "isUnavailableOnDepot": True}
                ],
            },
        )
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(Exception) as ei:
            await client.place_order(confirmed_total=100, expected_cart_version=3, poll=0)
    assert "checkout" in str(ei.value).lower()


@respx.mock
async def test_place_order_errors_when_no_order_id():
    _mock_homepage()
    _order_cart_route(version=3, total="219")
    respx.get("https://lavka.yandex.ru/api/v1/providers/v2/service-info").mock(
        return_value=httpx.Response(200, json={"depotId": "1"})
    )
    respx.post("https://lavka.yandex.ru/api/v1/orders/submit").mock(
        return_value=httpx.Response(200, json={"data": {}})  # 200 but no orderId
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(Exception) as ei:
            await client.place_order(confirmed_total=219, expected_cart_version=3, poll=0)
    assert "order id" in str(ei.value).lower()


@respx.mock
async def test_order_submit_is_not_retried():
    _mock_homepage()
    _order_cart_route(version=3, total="219")
    respx.get("https://lavka.yandex.ru/api/v1/providers/v2/service-info").mock(
        return_value=httpx.Response(200, json={"depotId": "1"})
    )
    submit = respx.post("https://lavka.yandex.ru/api/v1/orders/submit").mock(
        side_effect=httpx.ConnectError("boom")
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(Exception):
            await client.place_order(confirmed_total=219, expected_cart_version=3, poll=0)
    assert submit.call_count == 1  # submit must NOT be retried (double-charge risk)


@respx.mock
async def test_add_to_cart_warns_when_item_dropped():
    # Lavka accepts the write but the item isn't in the resulting cart (dropped
    # as unavailable) — add_to_cart must warn instead of implying success.
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"cartId": "c1", "cartVersion": 3, "items": []})
    )
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/update").mock(
        return_value=httpx.Response(
            200, json={"cartId": "c1", "cartVersion": 4, "totalItemsPrice": "0", "totalPriceValue": "0", "items": []}
        )
    )
    async with LavkaClient(_config()) as client:
        cart = await client.add_to_cart("ghost-id", 1, price=100)
    assert cart["warning"] and "did not end up" in cart["warning"].lower()


@respx.mock
async def test_add_to_cart_retries_on_409_conflict():
    # A concurrent writer bumped the cart, so our write hits 409; the client must
    # re-read the fresh version and retry rather than surfacing an error.
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        side_effect=[
            httpx.Response(200, json={"cartId": "c1", "cartVersion": 3, "items": []}),
            httpx.Response(200, json={"cartId": "c1", "cartVersion": 5, "items": []}),
        ]
    )
    update = respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/update").mock(
        side_effect=[
            httpx.Response(409, json={}),  # conflict — cart changed under us
            httpx.Response(
                200,
                json={
                    "cartId": "c1",
                    "cartVersion": 6,
                    "totalItemsPrice": "100",
                    "totalPriceValue": "100",
                    "items": [{"id": "p1", "title": "X", "quantity": "1", "currentPrice": 100}],
                },
            ),
        ]
    )
    async with LavkaClient(_config()) as client:
        cart = await client.add_to_cart("p1", 1, price=100)
    assert cart["cart_version"] == 6
    assert update.call_count == 2  # retried after the 409
    # the retry used the freshly-read version (5), not the stale 3
    body2 = json.loads(update.calls[1].request.content)
    assert body2["cartVersion"] == 5


@respx.mock
async def test_cancel_order_posts_to_dynamic_path():
    _mock_homepage()
    route = respx.post("https://lavka.yandex.ru/api/v1/orders/ord-9-grocery/cancel").mock(
        return_value=httpx.Response(200, json={})
    )
    async with LavkaClient(_config()) as client:
        result = await client.cancel_order("ord-9-grocery")
    assert result["cancelled"] is True
    assert route.called


@respx.mock
async def test_auth_error_maps_to_lavka_auth_error():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(LavkaAuthError):
            await client.get_cart()
