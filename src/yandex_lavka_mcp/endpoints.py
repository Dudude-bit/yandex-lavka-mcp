"""Central registry of Lavka internal-API endpoints.

Lavka has no public API. Every path here was captured live from
``lavka.yandex.ru``'s own frontend traffic on 2026-07-19 (logged-in session),
except ``order_create`` which only fires when you press "Оплатить" and was not
triggered (it would place a real order). All data calls sit under
``/api/v1/providers/*``. Any value can be overridden at runtime from
``config.json`` (``base_url`` / ``endpoints``) without touching code.
"""

from __future__ import annotations

from typing import Any

DEFAULT_BASE_URL = "https://lavka.yandex.ru"

DEFAULT_ENDPOINTS: dict[str, dict[str, str]] = {
    # Catalog / search  — CONFIRMED
    "search": {"method": "POST", "path": "/api/v1/providers/search/v3/lavka"},
    "product": {"method": "POST", "path": "/api/v1/providers/v1/product"},
    # Cart — CONFIRMED
    "cart_get": {"method": "POST", "path": "/api/v1/providers/cart/v1/retrieve"},
    "cart_update": {"method": "POST", "path": "/api/v1/providers/cart/v1/update"},
    # Checkout — CONFIRMED (page scaffold; order totals come from the cart)
    "checkout_layout": {"method": "POST", "path": "/api/v1/providers/orders/v1/checkout-layout"},
    "set_payment": {"method": "POST", "path": "/api/v1/providers/cart/v1/set-payment"},
    # Addresses / geo — CONFIRMED
    "favorite_addresses": {"method": "POST", "path": "/api/v1/providers/address/v1/get-favorite-addresses"},
    "geo_suggest": {"method": "POST", "path": "/api/v1/providers/geo/v1/suggest"},
    "geo_geocode": {"method": "POST", "path": "/api/v1/providers/geo/v1/geocode"},
    # Orders — CONFIRMED tracking
    "tracked_orders": {"method": "GET", "path": "/api/v1/providers/orders-tracking/v1/tracked-orders"},
    # Place order — CONFIRMED (captured 2026-07-19 from a real "Оплатить"). NOT
    # under /providers/. Submit returns {data:{orderId}} and triggers a charge on
    # the on-file card; payment then progresses via payments/v1/status and may
    # require 3-D Secure (status "wait_user_action" + redirectUrl).
    "order_create": {"method": "POST", "path": "/api/v1/orders/submit"},
    "payment_status": {"method": "POST", "path": "/api/v1/providers/payments/v1/status"},
    "payment_methods": {"method": "POST", "path": "/api/v1/providers/payments/v1/methods"},
    # order_cancel path is dynamic (/api/v1/orders/{orderId}/cancel) — built in
    # client.cancel_order, not via this registry.
}


def resolve_base_url(config_base_url: str | None) -> str:
    return (config_base_url or DEFAULT_BASE_URL).rstrip("/")


def resolve_endpoint(name: str, overrides: dict[str, Any] | None) -> dict[str, str]:
    """Return the endpoint spec for ``name``, applying config overrides."""
    spec = dict(DEFAULT_ENDPOINTS[name])
    if overrides and name in overrides:
        spec.update(overrides[name])
    return spec
