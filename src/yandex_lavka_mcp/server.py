"""FastMCP server exposing Lavka tools to Claude.

Money safety: placing an order is split into two tools. ``checkout_preview``
returns the full summary and charges nothing. ``confirm_order`` performs the
real charge and refuses unless the caller passes back the exact total from a
recent preview AND the live cart still matches that preview (same version and
total) at submit time — so the card is never charged an amount or a cart the
user did not approve. The "user said yes" step is a convention the model is
told to honour; the hard, enforced guarantee is the preview/cart match.
"""

from __future__ import annotations

import os
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import build_token_verifier
from .client import LavkaClient
from .config import Config, Location, load_config, save_config
from .errors import LavkaError

# Host/port only matter for the HTTP transports; harmless for stdio.
_HOST = os.environ.get("YANDEX_LAVKA_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("YANDEX_LAVKA_MCP_PORT", "8000"))
_verifier = build_token_verifier()

mcp = FastMCP(
    "yandex-lavka",
    host=_HOST,
    port=_PORT,
    stateless_http=True,
    token_verifier=_verifier,
    auth=_verifier.auth_settings() if _verifier else None,
)

# In-process record of the last checkout preview. confirm_order checks against
# it so the model cannot place an order it never previewed, and cannot place one
# for a total different from what the user approved.
_LAST_PREVIEW: dict[str, Any] = {}

_PRICE_TOLERANCE = 0.01
_PREVIEW_TTL_SECONDS = 180  # a confirm must follow its preview within this window


def _load() -> Config:
    return load_config()


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc)}


async def _with_client(fn, *, require_location: bool = True):
    config = _load()
    if not config.is_authenticated():
        raise LavkaError(
            "Not signed in. Run the one-time cookie capture (see README) and set "
            "your session with the setup tooling first."
        )
    if require_location and not config.location.is_set():
        raise LavkaError(
            "No delivery location set. Use set_delivery_address(\"город, улица, дом\"), "
            "use_address(name), or set_location(lat, lon) first."
        )
    async with LavkaClient(config) as client:
        return await fn(client)


# -- setup / status --------------------------------------------------------


@mcp.tool()
def lavka_status() -> dict[str, Any]:
    """Show whether the Lavka session and delivery location are configured.

    Read-only. Call this first to check setup before other tools.
    """
    config = _load()
    return {
        "authenticated": config.is_authenticated(),
        "location_set": config.location.is_set(),
        "location": config.location.to_dict() if config.location.is_set() else None,
        "base_url": config.base_url or "default",
    }


@mcp.tool()
def set_location(
    lat: float | None = None,
    lon: float | None = None,
    address_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Set the delivery location (required before catalog/cart calls).

    Provide either lat+lon coordinates or a saved address_id. Persists to config.
    """
    _LAST_PREVIEW.clear()  # location drives catalog/prices; any prior preview is stale
    config = _load()
    config.location = Location(lat=lat, lon=lon, address_id=address_id, label=label)
    if not config.location.is_set():
        return _err(ValueError("Provide lat+lon or address_id."))
    save_config(config)
    return {"ok": True, "location": config.location.to_dict()}


def _apply_address(
    config: Config,
    *,
    lat: float | None,
    lon: float | None,
    city: str | None,
    street: str | None,
    house: str | None,
    label: str | None,
    flat: str | None = None,
    entrance: str | None = None,
    floor: str | None = None,
    comment: str | None = None,
    place_id: str | None = None,
    country: str | None = None,
) -> None:
    """Point the config at a delivery address: coords + additionalData + the
    placeId/country needed to place an order."""
    config.location = Location(lat=lat, lon=lon, label=label)
    address: dict[str, Any] = {}
    if city:
        address["city"] = city
    if street:
        address["street"] = street
    if house:
        address["house"] = str(house)
    if flat:
        address["flat"] = str(flat)
    if entrance:
        address["entrance"] = str(entrance)
    if floor:
        address["floor"] = str(floor)
    if comment:
        address["comment"] = comment
    config.context["additionalData"] = address
    config.context["placeId"] = place_id or ""
    config.context["country"] = country or "Россия"
    save_config(config)


@mcp.tool()
async def list_addresses() -> dict[str, Any]:
    """List your saved Lavka delivery addresses (by name). Read-only.

    Use a returned `label` with use_address to switch delivery to that place.
    """
    try:
        addresses = await _with_client(lambda c: c.list_addresses(), require_location=False)
        return {"ok": True, "addresses": addresses}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def use_address(
    name: str, flat: str | None = None, entrance: str | None = None, comment: str | None = None
) -> dict[str, Any]:
    """Switch delivery to one of your saved addresses, matched by name.

    Catalog, cart and prices are location-scoped, so this re-points everything.
    A saved address carries city/street/house; pass flat/entrance/comment if the
    order needs them.
    """
    _LAST_PREVIEW.clear()
    try:
        addresses = await _with_client(lambda c: c.list_addresses(), require_location=False)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    key = name.strip().lower()
    match = next(
        (a for a in addresses if key in (a.get("label") or "").lower()),
        None,
    ) or next(
        (a for a in addresses if key in (a.get("full_address") or "").lower()),
        None,
    )
    if not match:
        names = [a.get("label") for a in addresses]
        return _err(ValueError(f"No saved address matches {name!r}. You have: {names}"))
    config = _load()
    _apply_address(
        config,
        lat=match.get("lat"),
        lon=match.get("lon"),
        city=match.get("city"),
        street=match.get("street"),
        house=match.get("house"),
        label=match.get("label"),
        flat=flat or match.get("flat"),
        entrance=entrance or match.get("entrance"),
        floor=match.get("floor"),
        comment=comment or match.get("comment"),
        place_id=match.get("place_id"),
        country=match.get("country"),
    )
    return {"ok": True, "location": config.location.to_dict(), "address": config.context["additionalData"]}


@mcp.tool()
async def set_delivery_address(
    query: str, flat: str | None = None, entrance: str | None = None, comment: str | None = None
) -> dict[str, Any]:
    """Set delivery to ANY address by free text — works for a new city.

    Resolves the address to coordinates + city/street/house via Lavka's own geo
    search. Example: set_delivery_address("Казань, улица Баумана, 1", flat="12").
    """
    _LAST_PREVIEW.clear()
    try:
        resolved = await _with_client(lambda c: c.resolve_address(query), require_location=False)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    config = _load()
    _apply_address(
        config,
        lat=resolved.get("lat"),
        lon=resolved.get("lon"),
        city=resolved.get("city"),
        street=resolved.get("street"),
        house=resolved.get("house"),
        label=resolved.get("text"),
        flat=flat,
        entrance=entrance or resolved.get("entrance"),
        comment=comment,
        place_id=resolved.get("place_id"),
        country=resolved.get("country"),
    )
    return {
        "ok": True,
        "resolved": resolved.get("text"),
        "location": config.location.to_dict(),
        "address": config.context["additionalData"],
    }


# -- catalog ---------------------------------------------------------------


@mcp.tool()
async def search_products(query: str, limit: int = 20) -> dict[str, Any]:
    """Search the Lavka catalog at the current delivery location. Read-only.

    Each result has an `id` (use it with add_to_cart) and a `slug` (use it with
    get_product).
    """
    try:
        results = await _with_client(lambda c: c.search(query, limit=limit))
        return {"ok": True, "query": query, "products": results}
    except Exception as exc:  # noqa: BLE001 - surface as tool result
        return _err(exc)


@mcp.tool()
async def get_product(slug: str) -> dict[str, Any]:
    """Get details for one product: price, size, stock, description. Read-only.

    Pass the `slug` from a search result.
    """
    try:
        product = await _with_client(lambda c: c.get_product(slug))
        return {"ok": True, "product": product}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# -- cart ------------------------------------------------------------------


@mcp.tool()
async def view_cart() -> dict[str, Any]:
    """Show the current cart contents and running total. Read-only.

    The cart also reports order-readiness. Watch these:
    - `warning`: a plain-language problem to fix (or null). If set, act on it.
    - each item's `unavailable_on_depot`: true = it's in the cart but CANNOT be
      ordered from the current store (only in «Большая Лавка», or sold out here).
    - `available_for_checkout`: false = the order can't be placed as-is.
    Remove/replace flagged items with update_cart_item(product_id, 0) before
    checkout.
    """
    try:
        cart = await _with_client(lambda c: c.get_cart())
        return {"ok": True, "cart": cart}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def add_to_cart(
    product_id: str, quantity: int = 1, price: float | None = None
) -> dict[str, Any]:
    """Add a product to the cart (increments if already present). No charge.

    `product_id` is the `id` from a search result. Pass `price` from the same
    search result when available (Lavka validates it on cart writes).

    IMPORTANT: search can return items that are only stocked in «Большая Лавка»
    (a different store) — they add fine but can't be ordered from the current
    one. After adding, check the returned cart's `warning` and each item's
    `unavailable_on_depot`; drop any flagged item with update_cart_item(id, 0).
    """
    _LAST_PREVIEW.clear()  # cart changed; any prior preview is stale
    try:
        cart = await _with_client(lambda c: c.add_to_cart(product_id, quantity, price=price))
        return {"ok": True, "cart": cart}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def update_cart_item(product_id: str, quantity: int) -> dict[str, Any]:
    """Set the exact quantity of a cart item. quantity=0 removes it. No charge."""
    _LAST_PREVIEW.clear()
    try:
        cart = await _with_client(lambda c: c.set_cart_item(product_id, quantity))
        return {"ok": True, "cart": cart}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def clear_cart() -> dict[str, Any]:
    """Remove everything from the cart. No charge."""
    _LAST_PREVIEW.clear()
    try:
        cart = await _with_client(lambda c: c.clear_cart())
        return {"ok": True, "cart": cart}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# -- checkout (two-step) ---------------------------------------------------


@mcp.tool()
async def checkout_preview() -> dict[str, Any]:
    """Preview the order: items, delivery fee, ETA, address, payment, TOTAL.

    Charges NOTHING. Show the returned total to the user and ask them to confirm
    it out loud before calling confirm_order. Always run this before confirming.

    If the summary has a `warning`, or `available_for_checkout` is false, or any
    item has `unavailable_on_depot: true`, the order will be REFUSED — fix the
    cart (remove/replace those items) and preview again before confirming.
    """
    try:
        summary = await _with_client(lambda c: c.checkout_preview())
        _LAST_PREVIEW.clear()
        _LAST_PREVIEW.update(summary)
        _LAST_PREVIEW["_ts"] = time.time()
        return {
            "ok": True,
            "summary": summary,
            "next_step": (
                "Show this total to the user and get an explicit yes. Then call "
                "confirm_order(confirmed_total=<the total above>)."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def confirm_order(confirmed_total: float) -> dict[str, Any]:
    """Place the order and CHARGE the card. Irreversible — real money.

    Call ONLY after checkout_preview was run and the user explicitly said yes to
    the previewed total. Pass that exact total as confirmed_total. The order is
    refused unless: a preview was run recently, confirmed_total matches it, and —
    critically — the live cart still matches the previewed cart at submit time
    (same version and total). If anything drifted, nothing is charged and you
    must preview + confirm again.

    Returns the order id and payment_status. "wait_user_action" means the bank
    requires 3-D Secure — give the user redirect_url to finish paying.
    cancel_order cancels.
    """
    if not _LAST_PREVIEW:
        return _err(
            LavkaError(
                "No fresh checkout preview. Run checkout_preview first, show the "
                "user the total, and confirm before ordering."
            )
        )
    age = time.time() - float(_LAST_PREVIEW.get("_ts") or 0)
    if age > _PREVIEW_TTL_SECONDS:
        _LAST_PREVIEW.clear()
        return _err(
            LavkaError(
                "The checkout preview is stale (older than "
                f"{_PREVIEW_TTL_SECONDS}s). Re-run checkout_preview and confirm again."
            )
        )
    preview_total = _LAST_PREVIEW.get("total")
    if preview_total is None:
        return _err(LavkaError("Preview had no total; re-run checkout_preview."))
    if abs(float(confirmed_total) - float(preview_total)) > _PRICE_TOLERANCE:
        return _err(
            LavkaError(
                f"Confirmed total {confirmed_total} does not match the previewed "
                f"total {preview_total}. Re-run checkout_preview and confirm the "
                "current amount."
            )
        )
    expected_version = _LAST_PREVIEW.get("cart_version")
    try:
        # place_order re-reads the cart and aborts (charges nothing) if it drifted
        # from the previewed total/version.
        result = await _with_client(
            lambda c: c.place_order(
                confirmed_total=float(confirmed_total), expected_cart_version=expected_version
            )
        )
        _LAST_PREVIEW.clear()
        status = result.get("payment_status")
        note = None
        if status == "wait_user_action" and result.get("redirect_url"):
            note = (
                "Order created but the bank requires 3-D Secure. Open redirect_url "
                "to finish paying, or cancel_order to cancel."
            )
        elif status not in ("success", "paid", "hold"):
            note = (
                f"Order created (payment status: {status}). Payment may not be "
                "complete — check the Lavka app; cancel_order can cancel."
            )
        return {"ok": True, "order": result, "note": note}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel an order by id (e.g. one returned by confirm_order/active_orders)."""
    try:
        result = await _with_client(lambda c: c.cancel_order(order_id), require_location=False)
        return {"ok": bool(result.get("cancelled")), "result": result}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# -- orders ----------------------------------------------------------------


@mcp.tool()
async def active_orders() -> dict[str, Any]:
    """Currently tracked orders with status and ETA. Read-only.

    Covers in-progress orders (Lavka's order-tracking feed). Full historical
    order history is a separate endpoint not yet wired up.
    """
    try:
        orders = await _with_client(lambda c: c.tracked_orders())
        return {"ok": True, "orders": orders}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


def run() -> None:
    # stdio for local clients; streamable-http for a remote (phone/claude.ai) deploy.
    transport = os.environ.get("YANDEX_LAVKA_MCP_TRANSPORT", "stdio")
    # Fail closed: a network-exposed HTTP transport that can spend real money MUST
    # be authenticated. Refuse to start otherwise (override only with an explicit
    # opt-out if you front it with your own auth).
    exposed = transport != "stdio" and _HOST not in _LOOPBACK_HOSTS
    opted_out = os.environ.get("YANDEX_LAVKA_MCP_ALLOW_INSECURE") == "1"
    if exposed and _verifier is None and not opted_out:
        raise SystemExit(
            "Refusing to start: transport is network-exposed HTTP but no OAuth is "
            "configured (this endpoint places real orders). Set "
            "YANDEX_LAVKA_MCP_OAUTH_ISSUER (see README 'Remote deploy'), or set "
            "YANDEX_LAVKA_MCP_ALLOW_INSECURE=1 if it's fronted by your own auth."
        )
    mcp.run(transport=transport)
