"""Tests for response trimming/normalization — no network."""

from __future__ import annotations

from yandex_lavka_mcp.client import LavkaClient, _to_amount


def test_to_amount_variants():
    assert _to_amount(129) == 129.0
    assert _to_amount("129") == 129.0
    assert _to_amount("1 299,50") == 1299.50
    assert _to_amount(None) is None
    assert _to_amount("n/a") is None
    assert _to_amount(True) is None


def test_trim_product_from_search_item():
    trimmed = LavkaClient._trim_product(
        {
            "id": "35a38bb8b9de48de860751e80af22349000100020000",
            "deepLink": "hleb-seryj-s-otrubyami-bratya-karavaevy-280-gram",
            "title": "Хлеб серый с отрубями",
            "currentPrice": 129,
            "amount": "280 г",
            "available": True,
        }
    )
    assert trimmed == {
        "id": "35a38bb8b9de48de860751e80af22349000100020000",
        "slug": "hleb-seryj-s-otrubyami-bratya-karavaevy-280-gram",
        "title": "Хлеб серый с отрубями",
        "price": 129.0,
        "old_price": None,
        "quantity_label": "280 г",
        "in_stock": True,
    }


def test_normalize_cart_totals_and_delivery():
    client = LavkaClient.__new__(LavkaClient)  # no __init__/network needed
    cart = client._normalize_cart(
        {
            "cartId": "abc",
            "cartVersion": 3,
            "totalItemsPrice": "129",
            "totalPriceValue": "458",
            "totalItemsCount": 1,
            "items": [
                {"id": "x", "title": "Хлеб", "quantity": "1", "currentPrice": 129, "amount": "280 г"}
            ],
        }
    )
    assert cart["cart_id"] == "abc"
    assert cart["cart_version"] == 3
    assert cart["subtotal"] == 129.0
    assert cart["total"] == 458.0
    assert cart["delivery_fee"] == 329.0
    assert cart["items"][0]["title"] == "Хлеб"
    assert cart["items"][0]["quantity_label"] == "280 г"


def test_normalize_cart_free_delivery_never_negative():
    client = LavkaClient.__new__(LavkaClient)
    # No explicit orderConditions -> inferred delivery, clamped to >= 0.
    cart = client._normalize_cart(
        {
            "totalItemsPrice": "1424",
            "totalPriceValue": "1423",
            "items": [{"id": "x", "title": "Перец", "quantity": "1", "currentPrice": 299}],
        }
    )
    assert cart["subtotal"] == 1424.0
    assert cart["total"] == 1423.0
    assert cart["delivery_fee"] == 0.0  # clamped, not -1.0


def test_trim_cart_item_flags_depot_unavailability():
    trimmed = LavkaClient._trim_cart_item(
        {"id": "x", "title": "Творог", "quantity": "1", "currentPrice": 118, "isUnavailableOnDepot": True}
    )
    assert trimmed["unavailable_on_depot"] is True


def test_normalize_cart_surfaces_checkout_availability():
    client = LavkaClient.__new__(LavkaClient)
    cart = client._normalize_cart(
        {
            "totalItemsPrice": "100",
            "totalPriceValue": "100",
            "totalDiscountValue": "0",
            "availableForCheckout": False,
            "checkoutUnavailableReason": "quantity-over-limit",
            "orderConditions": {"deliveryCost": "0"},
            "items": [
                {"id": "a", "title": "Огурцы", "quantity": "1", "currentPrice": 100, "isUnavailableOnDepot": False},
                {"id": "b", "title": "Голубика", "quantity": "2", "currentPrice": 169, "isUnavailableOnDepot": True},
            ],
        }
    )
    assert cart["available_for_checkout"] is False
    assert cart["checkout_blocked_reason"] == "quantity-over-limit"
    assert cart["items"][1]["unavailable_on_depot"] is True
    assert "Голубика" in cart["unavailable_items"]


def test_normalize_cart_real_breakdown_from_explicit_fields():
    client = LavkaClient.__new__(LavkaClient)
    # Real Lavka shape: товары 1424 − скидка 120 + доставка 119 = 1423.
    cart = client._normalize_cart(
        {
            "totalItemsPrice": "1424",
            "totalDiscountValue": "120",
            "totalPriceValue": "1423",
            "orderConditions": {"deliveryCost": "119", "eta": "5–10 мин"},
            "items": [{"id": "x", "title": "Перец", "quantity": "1", "currentPrice": 299}],
        }
    )
    assert cart["subtotal"] == 1424.0
    assert cart["discount"] == 120.0
    assert cart["delivery_fee"] == 119.0  # real cost, not inferred
    assert cart["total"] == 1423.0
    assert cart["eta"] == "5–10 мин"
