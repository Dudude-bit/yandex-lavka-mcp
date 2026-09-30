"""Tests for the category endpoints (tree, group, category products)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from yandex_lavka_mcp.client import LavkaClient
from yandex_lavka_mcp.config import Config, Location
from yandex_lavka_mcp.errors import LavkaApiError


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


_LAYOUT_RESPONSE = {
    "layoutId": "layout-1",
    "layoutTitle": "Каталог",
    "sections": [
        {
            "type": "category_group_with_data",
            "categoryGroup": {
                "categoryGroupInfo": {
                    "type": "category_group",
                    "id": "grp-1",
                    "title": "Овощной прилавок",
                    "deepLink": "vegetables",
                }
            },
            "categories": [
                {
                    "categoryInfo": {
                        "type": "category",
                        "id": "cat-1",
                        "deepLink": "ovoshi",
                        "title": "Овощи, грибы и зелень",
                        "available": True,
                    },
                    "meta": {},
                },
                {
                    "categoryInfo": {
                        "type": "category",
                        "id": "cat-2",
                        "deepLink": "frukty",
                        "title": "Фрукты и ягоды",
                        "available": False,
                    },
                    "meta": {},
                },
            ],
        }
    ],
}

_GROUP_RESPONSE = {
    "items": [{"type": "category_group", "id": "grp-1", "items": []}],
    "products": [
        {"id": "grp-1", "title": "Овощной прилавок", "type": "category_group"},
        {
            "id": "cat-1",
            "deepLink": "ovoshi",
            "title": "Овощи, грибы и зелень",
            "available": True,
            "type": "category",
        },
        {
            "id": "cat-2",
            "deepLink": "frukty",
            "title": "Фрукты и ягоды",
            "available": True,
            "type": "category",
        },
    ],
}


def _category_response() -> dict:
    def _good(pid: str) -> dict:
        return {"value": {"type": "good", "id": pid}}

    return {
        "categoryGroup": {"id": "grp-1", "title": "Овощной прилавок"},
        "categories": [
            {
                "id": "node-cat-1",
                "value": {
                    "type": "category",
                    "id": "cat-1",
                    "title": "Овощи, грибы и зелень",
                    "available": True,
                },
                "items": [
                    {
                        "value": {"type": "subcategory", "id": "sub-tom", "title": "Помидоры"},
                        "items": [_good("p1"), _good("p2")],
                    },
                    {
                        "value": {"type": "subcategory", "id": "sub-cuc", "title": "Огурцы"},
                        "items": [_good("p3")],
                    },
                    _good("p4"),
                ],
            }
        ],
        "products": [
            {
                "id": "p1",
                "title": "Томаты черри",
                "deepLink": "tomaty-cherri",
                "currentPrice": 199,
                "oldPrice": 249,
                "amount": "250 г",
                "available": True,
            },
            {"id": "p2", "title": "Помидоры розовые", "deepLink": "rozovye", "currentPrice": 149},
            {"id": "p3", "title": "Огурец гладкий", "deepLink": "ogurec", "currentPrice": 89},
            {"id": "p4", "title": "Авокадо", "deepLink": "avokado", "currentPrice": 129},
        ],
    }


@respx.mock
async def test_category_tree_trims_groups_and_categories():
    _mock_homepage()
    route = respx.post("https://lavka.yandex.ru/api/v1/providers/v1/layout").mock(
        return_value=httpx.Response(200, json=_LAYOUT_RESPONSE)
    )
    async with LavkaClient(_config()) as client:
        tree = await client.get_category_tree()

    assert tree["group_count"] == 1
    assert tree["category_count"] == 2
    assert tree["groups"] == [
        {
            "id": "grp-1",
            "title": "Овощной прилавок",
            "slug": "vegetables",
            "categories": [
                {"id": "cat-1", "title": "Овощи, грибы и зелень", "slug": "ovoshi", "available": True},
                {"id": "cat-2", "title": "Фрукты и ягоды", "slug": "frukty", "available": False},
            ],
        }
    ]
    body = json.loads(route.calls.last.request.content)
    assert body["layoutSlug"] == "grocery"
    assert body["position"]["location"] == [37.0, 55.0]


@respx.mock
async def test_category_group_trims_group_and_categories():
    _mock_homepage()
    route = respx.post("https://lavka.yandex.ru/api/v1/providers/v1/category-group").mock(
        return_value=httpx.Response(200, json=_GROUP_RESPONSE)
    )
    async with LavkaClient(_config()) as client:
        result = await client.get_category_group("grp-1")

    assert result["group"] == {"id": "grp-1", "title": "Овощной прилавок"}
    assert [c["id"] for c in result["categories"]] == ["cat-1", "cat-2"]
    body = json.loads(route.calls.last.request.content)
    assert body["groupId"] == "grp-1"
    assert body["layoutSlug"] == "grocery"


@respx.mock
async def test_get_category_resolves_parent_group_and_trims():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/v1/layout").mock(
        return_value=httpx.Response(200, json=_LAYOUT_RESPONSE)
    )
    route = respx.post("https://lavka.yandex.ru/api/v1/providers/v2/category").mock(
        return_value=httpx.Response(200, json=_category_response())
    )
    async with LavkaClient(_config()) as client:
        result = await client.get_category("cat-1")

    # The parent group was resolved from the tree and sent as the slug path.
    body = json.loads(route.calls.last.request.content)
    assert body["categoryId"] == "cat-1"
    assert body["modes"] == ["grocery"]
    assert body["categorySlugPath"] == {"layoutSlug": "grocery", "groupId": "grp-1"}

    assert result["category"]["id"] == "cat-1"
    assert result["group"] == {"id": "grp-1", "title": "Овощной прилавок"}
    assert result["total_products"] == 4
    assert [s for s in result["subcategories"]] == [
        {"id": "sub-tom", "title": "Помидоры", "product_count": 2},
        {"id": "sub-cuc", "title": "Огурцы", "product_count": 1},
    ]
    assert [p["id"] for p in result["products"]] == ["p1", "p2", "p3", "p4"]
    assert result["products"][0] == {
        "id": "p1",
        "slug": "tomaty-cherri",
        "title": "Томаты черри",
        "price": 199.0,
        "old_price": 249.0,
        "quantity_label": "250 г",
        "in_stock": True,
    }


@respx.mock
async def test_get_category_explicit_group_id_skips_tree_lookup():
    _mock_homepage()
    layout = respx.post("https://lavka.yandex.ru/api/v1/providers/v1/layout").mock(
        return_value=httpx.Response(500, json={})
    )
    route = respx.post("https://lavka.yandex.ru/api/v1/providers/v2/category").mock(
        return_value=httpx.Response(200, json=_category_response())
    )
    async with LavkaClient(_config()) as client:
        result = await client.get_category("cat-1", group_id="grp-9")

    assert not layout.called
    body = json.loads(route.calls.last.request.content)
    assert body["categorySlugPath"]["groupId"] == "grp-9"
    assert result["group"]["id"] == "grp-9"  # falls back to the response's categoryGroup


@respx.mock
async def test_get_category_subcategory_filter_by_title_and_limit():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/v1/layout").mock(
        return_value=httpx.Response(200, json=_LAYOUT_RESPONSE)
    )
    respx.post("https://lavka.yandex.ru/api/v1/providers/v2/category").mock(
        return_value=httpx.Response(200, json=_category_response())
    )
    async with LavkaClient(_config()) as client:
        by_title = await client.get_category("cat-1", subcategory="помидоры")
        by_id = await client.get_category("cat-1", subcategory="sub-cuc")
        limited = await client.get_category("cat-1", limit=2)

    assert [p["id"] for p in by_title["products"]] == ["p1", "p2"]
    assert by_title["total_products"] == 2
    assert [p["id"] for p in by_id["products"]] == ["p3"]
    assert len(limited["products"]) == 2
    assert limited["total_products"] == 4  # limit trims the output, not the count


@respx.mock
async def test_get_category_unknown_subcategory_lists_options():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/v1/layout").mock(
        return_value=httpx.Response(200, json=_LAYOUT_RESPONSE)
    )
    respx.post("https://lavka.yandex.ru/api/v1/providers/v2/category").mock(
        return_value=httpx.Response(200, json=_category_response())
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(LavkaApiError) as excinfo:
            await client.get_category("cat-1", subcategory="капуста")
    assert "Помидоры" in str(excinfo.value)


@respx.mock
async def test_get_category_unknown_category_error_mentions_groups():
    _mock_homepage()
    respx.post("https://lavka.yandex.ru/api/v1/providers/v1/layout").mock(
        return_value=httpx.Response(200, json=_LAYOUT_RESPONSE)
    )
    async with LavkaClient(_config()) as client:
        with pytest.raises(LavkaApiError) as excinfo:
            await client.get_category("nope")
    assert "Овощной прилавок" in str(excinfo.value)
