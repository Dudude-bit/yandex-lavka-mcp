"""Tests for response trimming/normalization — no network."""

from __future__ import annotations

import pytest

from yandex_lavka_mcp.client import LavkaClient, _product_ref, _to_amount


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


def test_titles_drop_soft_hyphens():
    # Lavka hyphenates titles for the browser ("Моло\xadко"); the model needs plain text.
    assert LavkaClient._trim_product({"title": "Моло\xadко 2,5%"})["title"] == "Молоко 2,5%"
    assert LavkaClient._trim_cart_item({"title": "Моло\xadко"})["title"] == "Молоко"


def _pfc(order, portion_title, rows, amount, extras=None):
    """A product shaped like the live /v1/product response (options.ingredients)."""
    ids = ("calories", "protein", "fat", "carbohydrate")
    traits = [{"id": i, "measures": dict(zip(("per100g", "perPortion"), r))} for i, r in zip(ids, rows)]
    ingredients = {"description": "", "pfcTraits": traits,
                   "pfcSettings": {"orderPfcBlocks": order, "per100gTitle": "На 100 г", "perPortionTitle": portion_title}}
    if extras:
        ingredients["pfcTraitsExtras"] = extras
    return {"amount": amount, "options": {"ingredients": ingredients}}


# Live data, 2026-10-04 — numbers as the cards show them.
CUCUMBER = _pfc(["per100g", "per_portion"], "На 300 г", [("15", "4,5"), ("0,8", "0,2"), ("0,1", "0"), ("2,8", "0,8")], "300 г")
CHICKEN = _pfc(["per_portion", "per100g"], "Всё блюдо", [("168,1", "235,3"), ("30,7", "42,9"), ("4,7", "6,5"), ("0,7", "0,9")], "140 г")
EXPONENTA = _pfc(["per_portion", "per100g"], "На упаковку", [("62", "99,2"), ("12,5", "20"), ("0", "0"), ("3", "4,8")], "160 г")
BOMBBAR = _pfc(["per100g", "per_portion"], "На 50 г", [("369", "184,5"), ("25", "12,5"), ("4", "2"), ("55", "27,5")], "50 г",
               extras={"extrasName": "соуса", "pfcOptionsExtras": []})


def test_nutrition_both_tabs_first_in_order_is_default():
    n = LavkaClient._nutrition(CHICKEN)
    assert n == {
        "per_100g": {"kcal": 168.1, "protein": 30.7, "fat": 4.7, "carbs": 0.7},
        "per_portion": {"kcal": 235.3, "protein": 42.9, "fat": 6.5, "carbs": 0.9, "label": "Всё блюдо"},
        "default_basis": "per_portion",
        "portion_grams": 140.0,
    }
    e = LavkaClient._nutrition(EXPONENTA)
    assert e["per_portion"] == {"kcal": 99.2, "protein": 20.0, "fat": 0.0, "carbs": 4.8, "label": "На упаковку"}
    assert (e["default_basis"], e["portion_grams"]) == ("per_portion", 160.0)


def test_nutrition_per100g_first_and_grams_from_label():
    c = LavkaClient._nutrition(CUCUMBER)
    assert c["per_100g"] == {"kcal": 15.0, "protein": 0.8, "fat": 0.1, "carbs": 2.8}
    # Passed through as Lavka shows it, even when it looks off (4,5 kcal for 300 g).
    assert c["per_portion"] == {"kcal": 4.5, "protein": 0.2, "fat": 0.0, "carbs": 0.8, "label": "На 300 г"}
    assert (c["default_basis"], c["portion_grams"]) == ("per_100g", 300.0)
    b = LavkaClient._nutrition(BOMBBAR)
    assert b["per_portion"] == {"kcal": 184.5, "protein": 12.5, "fat": 2.0, "carbs": 27.5, "label": "На 50 г"}
    assert (b["default_basis"], b["portion_grams"]) == ("per_100g", 50.0)


def test_nutrition_single_tab_when_no_portion_values():
    p = _pfc(["per_portion", "per100g"], "На порцию", [("52", ""), ("0,3", ""), ("0,2", ""), ("14", "")], "1 кг")
    assert LavkaClient._nutrition(p) == {
        "per_100g": {"kcal": 52.0, "protein": 0.3, "fat": 0.2, "carbs": 14.0},
        "per_portion": None,
        "default_basis": "per_100g",
        "portion_grams": None,
    }


def test_nutrition_portion_grams_never_guessed():
    # "На порцию" names no weight, and a millilitre pack has no gram weight.
    assert LavkaClient._nutrition(_pfc(["per_portion"], "На порцию", [("1", "2")] * 4, "200 г"))["portion_grams"] is None
    assert LavkaClient._nutrition(_pfc(["per_portion"], "На упаковку", [("1", "2")] * 4, "500 мл"))["portion_grams"] is None
    assert LavkaClient._nutrition(_pfc(["per_portion"], "Всё блюдо", [("1", "2")] * 4, "1,2 кг"))["portion_grams"] == 1200.0


def test_nutrition_null_for_non_food():
    # Live: a kitchen sponge still has pfcSettings, but no traits.
    sponge = {"options": {"ingredients": {"pfcTraits": [], "pfcSettings": {"orderPfcBlocks": ["per100g", "per_portion"], "perPortionTitle": "На 5 шт."}}}}
    assert LavkaClient._nutrition(sponge) is None
    assert LavkaClient._nutrition({}) is None


@pytest.mark.parametrize("ref, expected", [
    ("33cc63b13d194dc4a6b13fe8d92fcba3000200020000", "33cc63b13d194dc4a6b13fe8d92fcba3000200020000"),
    ("grudka-kurinaya-zapechyonnaya-2-sht-iz-lavki-140-gram", "grudka-kurinaya-zapechyonnaya-2-sht-iz-lavki-140-gram"),
    ("https://lavka.yandex.ru/external?service=grocery&href=?item=c236b75cff42468388777bdbdf523d0f000200020000:st-md",
     "c236b75cff42468388777bdbdf523d0f000200020000"),
    ("https://lavka.yandex.ru/external?service=grocery&href=%3Fitem%3Dc236b75cff42468388777bdbdf523d0f000200020000%3Ast-md%26x%3D1",
     "c236b75cff42468388777bdbdf523d0f000200020000"),
    ("https://lavka.yandex.ru/good/ogurcy-korotkoplodnye-hrustyashie-iz-lavki-300-gram?utm=1",
     "ogurcy-korotkoplodnye-hrustyashie-iz-lavki-300-gram"),
    ("  slug-with-spaces  ", "slug-with-spaces"),
])
def test_product_ref_accepts_id_slug_and_links(ref, expected):
    assert _product_ref(ref) == expected


def test_titles_drop_notr_markup():
    assert LavkaClient._trim_product({"title": "Огурцы <notr>Из Лавки</notr>"})["title"] == "Огурцы Из Лавки"
