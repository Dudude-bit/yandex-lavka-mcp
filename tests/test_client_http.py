"""Tests for the HTTP client against a mocked transport."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from yandex_lavka_mcp.client import LavkaClient
from fakes import config as _config
from fakes import mock_homepage as _mock_homepage
from yandex_lavka_mcp.errors import LavkaApiError, LavkaAuthError




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
async def test_list_payment_methods_trims_and_flags_default():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/payments/v1/methods").mock(
        return_value=httpx.Response(
            200,
            json={
                "methods": [
                    {"id": "card-1", "type": "card", "displayName": ["MIR", "1384"], "cardBank": "TINKOFF", "availability": {"available": True}},
                    {"id": "card-2", "type": "card", "displayName": ["MIR", "7482"], "cardBank": "VTB", "availability": {"available": True}},
                    # Lavka also lists every SBP bank (225 live); checkout only pays by card.
                    {"id": "sbp-1", "type": "sbp_bind_token", "name": "Сбербанк", "availability": {"available": True}},
                ],
                "defaultMethod": {"id": "card-1"},
            },
        )
    )
    async with LavkaClient(_config()) as client:
        info = await client.list_payment_methods()
    assert info["default_id"] == "card-1"
    assert info["methods"][0]["label"] == "MIR 1384"
    assert info["methods"][0]["is_default"] is True
    assert info["methods"][1]["is_default"] is False
    assert [m["id"] for m in info["methods"]] == ["card-1", "card-2"]


@respx.mock
async def test_place_order_uses_account_default_card_when_cart_has_none():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(
            200,
            json={
                "cartId": "c1", "cartVersion": 3, "orderFlowVersion": "grocery_flow_v1",
                "totalItemsPrice": "100", "totalPriceValue": "219", "availableForCheckout": True,
                "items": [{"id": "p1", "title": "X", "quantity": "1", "currentPrice": 100}],
            },  # note: no paymentMethod on the cart
        )
    )
    respx.post("https://lavka.yandex.ru/api/v1/providers/payments/v1/methods").mock(
        return_value=httpx.Response(200, json={"methods": [{"id": "card-default", "type": "card", "displayName": ["MIR", "1384"], "availability": {"available": True}}], "defaultMethod": {"id": "card-default"}})
    )
    respx.get("https://lavka.yandex.ru/api/v1/providers/v2/service-info").mock(
        return_value=httpx.Response(200, json={"depotId": "1"})
    )
    submit = respx.post("https://lavka.yandex.ru/api/v1/orders/submit").mock(
        return_value=httpx.Response(200, json={"data": {"orderId": "ord"}})
    )
    async with LavkaClient(_config()) as client:
        result = await client.place_order(confirmed_total=219, expected_cart_version=3, poll=0)
    assert result["order_id"] == "ord"
    body = json.loads(submit.calls.last.request.content)
    assert body["paymentMethodId"] == "card-default"  # auto-resolved from the account default


@respx.mock
async def test_place_order_aborts_when_no_payment_method():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        return_value=httpx.Response(
            200,
            json={
                "cartId": "c1", "cartVersion": 3, "totalItemsPrice": "100", "totalPriceValue": "100",
                "availableForCheckout": True,
                "items": [{"id": "p1", "title": "X", "quantity": "1", "currentPrice": 100}],
            },
        )
    )
    respx.post("https://lavka.yandex.ru/api/v1/providers/payments/v1/methods").mock(
        return_value=httpx.Response(200, json={"methods": [], "defaultMethod": {}})
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(Exception) as ei:
            await client.place_order(confirmed_total=100, expected_cart_version=3, poll=0)
    assert "payment method" in str(ei.value).lower()


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


@respx.mock
async def test_captcha_is_an_error_not_empty_data():
    # Yandex anti-bot answers HTTP 200 with a captcha instead of data; it used to
    # reach the model as "nothing found" / "cart is empty".
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/search/v3/lavka").mock(
        return_value=httpx.Response(200, json={"type": "captcha", "captcha": {"key": "k"}})
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(LavkaApiError, match="captcha"):
            await client.search("молоко")


@respx.mock
async def test_stale_csrf_is_refreshed_even_without_retries():
    # A 401 on the last (here: only) attempt must still refresh the CSRF token
    # and resend — it used to crash with NameError instead.
    _mock_homepage()
    route = respx.post("https://lavka.yandex.ru/api/v1/providers/cart/v1/retrieve").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": 1})]
    )
    async with LavkaClient(_config()) as client:
        assert await client._call("cart_get", {}, retry=False) == {"ok": 1}
    assert route.call_count == 2


@respx.mock
async def test_get_product_by_share_link_returns_nutrition():
    _mock_homepage()
    route = respx.post("https://lavka.yandex.ru/api/v1/providers/v1/product").mock(
        return_value=httpx.Response(200, json={"product": {
            "id": "c236b75cff42468388777bdbdf523d0f000200020000",
            "deepLink": "ogurcy-korotkoplodnye-hrustyashie-iz-lavki-300-gram",
            "title": "Огур\xadцы хру\xadстя\xadщие <notr>Из Лавки</notr>",
            "amount": "300 г",
            "options": {"ingredients": {"pfcTraits": [
                {"id": "calories", "measures": {"per100g": "15", "perPortion": "4,5"}},
                {"id": "protein", "measures": {"per100g": "0,8", "perPortion": "0,2"}},
                {"id": "fat", "measures": {"per100g": "0,1", "perPortion": "0"}},
                {"id": "carbohydrate", "measures": {"per100g": "2,8", "perPortion": "0,8"}},
            ], "pfcSettings": {"orderPfcBlocks": ["per100g", "per_portion"], "per100gTitle": "На 100 г", "perPortionTitle": "На 300 г"}}},
        }})
    )
    link = "https://lavka.yandex.ru/external?service=grocery&href=?item=c236b75cff42468388777bdbdf523d0f000200020000:st-md"
    async with LavkaClient(_config()) as client:
        product = await client.get_product(link)
    assert json.loads(route.calls.last.request.content)["productId"] == "c236b75cff42468388777bdbdf523d0f000200020000"
    assert product["title"] == "Огурцы хрустящие Из Лавки"
    assert product["nutrition"]["per_100g"] == {"kcal": 15.0, "protein": 0.8, "fat": 0.1, "carbs": 2.8}
    assert product["nutrition"]["default_basis"] is None  # two tabs, no flag


def _history_order(order_id: str, **extra) -> dict:
    """Shaped like the live orders/v1/history responses (2026-10-05)."""
    return {
        "deliveryInfo": {"orderId": order_id, "shortOrderId": "123-456", "status": "closed", "isCanceled": False,
                         "isFailed": False, "date": "2026-10-01", "createdAt": "2026-10-01T10:00:00+03:00",
                         "address": "Тверская, 1"},
        "calculation": {"finalCost": "1 423", "deliveryCost": "119", "discount": "120", "currencyCode": "RUB"},
        "productsPrice": "1424",
        "positions": [{"id": "p1", "title": "Огур\xadцы <notr>Из Лавки</notr>", "count": 2, "price": "239", "totalPrice": "478", "type": "product"}],
        **extra,
    }


@respx.mock
async def test_order_history_pages_and_trims():
    _mock_homepage()
    route = respx.get("https://lavka.yandex.ru/api/v1/orders/v1/history/list").mock(
        return_value=httpx.Response(200, json={"data": {"orders": [_history_order("ord-1"), _history_order("ord-2")]}})
    )
    async with LavkaClient(_config()) as client:
        orders = await client.order_history(limit=2, last_order_id="ord-0")
    assert dict(route.calls.last.request.url.params) == {"count": "2", "lastOrderId": "ord-0"}
    assert [o["order_id"] for o in orders] == ["ord-1", "ord-2"]
    assert orders[0] == {
        "order_id": "ord-1", "short_order_id": "123-456", "status": "closed", "is_canceled": False,
        "is_failed": False, "date": "2026-10-01", "created_at": "2026-10-01T10:00:00+03:00",
        "address": "Тверская, 1", "items_count": 1, "total": 1423.0, "products_price": 1424.0,
        "delivery_cost": 119.0, "discount": 120.0,
    }


@respx.mock
async def test_get_order_quotes_the_id_and_shares_the_summary():
    _mock_homepage()
    route = respx.get("https://lavka.yandex.ru/api/v1/orders/v1/history/ord%2F1").mock(
        return_value=httpx.Response(200, json={"data": _history_order("ord/1", productsPriceInitial="1544")})
    )
    async with LavkaClient(_config()) as client:
        order = await client.get_order("ord/1")
    assert route.called  # the id is path-escaped, not spliced in raw
    assert order["total"] == 1423.0 and order["currency"] == "RUB" and order["products_price_initial"] == 1544.0
    assert order["items"] == [{"id": "p1", "title": "Огурцы Из Лавки", "quantity": 2, "price": 239.0, "total": 478.0, "type": "product"}]


@respx.mock
async def test_cancel_order_refreshes_stale_csrf_and_is_not_retried():
    home = _mock_homepage()
    route = respx.post("https://lavka.yandex.ru/api/v1/orders/ord-9-grocery/cancel").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={})]
    )
    async with LavkaClient(_config()) as client:
        result = await client.cancel_order("ord-9-grocery")
    assert result == {"order_id": "ord-9-grocery", "cancelled": True}
    assert route.call_count == 2 and home.call_count == 2  # CSRF re-fetched once


@respx.mock
async def test_cancel_order_failure_is_an_error():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/orders/ord-9/cancel").mock(return_value=httpx.Response(409, json={}))
    async with LavkaClient(_config()) as client:
        with pytest.raises(LavkaApiError, match="409"):
            await client.cancel_order("ord-9")


def test_base_body_has_no_is_supermarket():
    # Measured live: the flag changed nothing (depotType decides the store).
    from yandex_lavka_mcp.config import Config
    body = LavkaClient(Config(context={"depotType": "supermarket"}))._base_body()
    assert body["depotType"] == "supermarket" and "is_supermarket" not in body
