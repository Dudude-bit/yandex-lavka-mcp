"""Low-level async client for the Lavka internal web API.

Request bodies and response shapes below were captured live from
``lavka.yandex.ru`` (logged-in session, 2026-07-19). Every public method returns
a small trimmed dict — never the raw Lavka payload, which is large.

Key facts baked in:
- Location is ``[lon, lat]`` (note the order).
- Search returns products under ``cacheProducts``; each product's ``id`` is the
  hash used to add it to the cart, and ``deepLink`` is the slug used for the
  product-detail call.
- Cart writes need the current ``cartId`` + ``cartVersion`` (read them from the
  cart first) plus a fresh ``idempotencyToken``.
- Order totals live in the cart response (``totalItemsPrice`` /
  ``totalPriceValue``), not in a dedicated checkout endpoint.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config
from .endpoints import resolve_base_url, resolve_endpoint
from .errors import LavkaApiError, LavkaAuthError, LavkaConfigError

_DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Origin": "https://lavka.yandex.ru",
    "Referer": "https://lavka.yandex.ru/",
    "X-Requested-With": "XMLHttpRequest",
}

_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 0.5
_PRICE_TOLERANCE = 0.01  # RUB; drift beyond this between preview and submit aborts the order
_CART_CONFLICT_RETRIES = 3  # re-read + retry on cartVersion 409 (shared-cart concurrency)

# The Lavka cart is ONE shared server-side resource per account. Serialize this
# process's own cart writes so parallel tool calls don't self-collide on the
# optimistic-concurrency version; cross-process collisions are handled by the
# 409 re-read/retry in _cart_mutate.
_CART_WRITE_LOCK = asyncio.Lock()

# The anti-CSRF token Lavka's API requires is embedded in the homepage HTML:
#   <script id="__page_props__-data" ...>{"csrfToken":"...", ...}</script>
_CSRF_RE = re.compile(r'"csrfToken"\s*:\s*"([^"]+)"')


def _pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _to_amount(value: Any) -> float | None:
    """Normalize a Lavka price value (str/int/float/None) to a float."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _new_idempotency_token() -> str:
    return uuid.uuid4().hex


class LavkaClient:
    """Async client. Use as an async context manager."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._base_url = resolve_base_url(config.base_url)
        self._client: httpx.AsyncClient | None = None
        self._csrf_token: str | None = None

    async def __aenter__(self) -> "LavkaClient":
        headers = {**_DEFAULT_HEADERS, **self._config.headers}
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            cookies=self._config.cookies,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- context / request plumbing ---------------------------------------

    def _position(self) -> dict[str, Any]:
        loc = self._config.location
        if loc.lat is not None and loc.lon is not None:
            return {"location": [loc.lon, loc.lat]}  # [lon, lat]
        return {}

    def _ctx(self, key: str, default: Any = None) -> Any:
        return self._config.context.get(key, default)

    def _base_body(self) -> dict[str, Any]:
        """Fields Lavka wants on nearly every call."""
        body: dict[str, Any] = {
            "depotType": self._ctx("depotType", "regular"),
            "currencySign": self._ctx("currencySign", "₽"),
        }
        pos = self._position()
        if pos:
            body["position"] = pos
        ad = self._ctx("additionalData")
        if ad:
            body["additionalData"] = ad
        return body

    def _lavka_headers(self) -> dict[str, str]:
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "X-Lavka-Web-Locale": str(self._ctx("locale", "ru-RU")),
            "X-Lavka-Web-City": str(self._ctx("webCity", "213")),
            "X-Captcha-Service": "lavka",
            "X-Captcha-Language": "ru",
        }
        if self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token
        return headers

    async def _ensure_csrf(self, *, force: bool = False) -> None:
        """Fetch the anti-CSRF token from the Lavka homepage HTML if we lack one."""
        if self._csrf_token and not force:
            return
        assert self._client is not None
        resp = await self._client.get("/")
        if resp.status_code in (401, 403):
            raise LavkaAuthError(
                f"Lavka session is not authorized (HTTP {resp.status_code}). "
                "Re-capture your Yandex cookies."
            )
        match = _CSRF_RE.search(resp.text)
        if match:
            self._csrf_token = match.group(1)

    async def _call(
        self, name: str, payload: dict[str, Any] | None = None, *, retry: bool = True
    ) -> Any:
        if self._client is None:
            raise LavkaConfigError("Client used outside of an async context.")
        spec = resolve_endpoint(name, self._config.endpoints)
        method = spec["method"].upper()
        kwargs: dict[str, Any] = {}
        if payload is not None and method != "GET":
            kwargs["json"] = payload

        # Non-idempotent writes (placing an order) must NOT be retried: a lost
        # response on a retried request could submit the order twice.
        max_retries = _MAX_RETRIES if retry else 0
        await self._ensure_csrf()
        csrf_refreshed = False
        for attempt in range(max_retries + 1):
            try:
                resp = await self._client.request(
                    method, spec["path"], headers=self._lavka_headers(), **kwargs
                )
            except httpx.HTTPError as exc:
                if attempt < max_retries:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise LavkaApiError(f"Network error calling {name}: {exc}") from exc

            if resp.status_code in (401, 403):
                # A stale CSRF token also shows up as 401 — refresh once and retry
                # before concluding the session itself is dead. (Safe even for
                # non-retry calls: the request never reached a success.)
                if not csrf_refreshed:
                    csrf_refreshed = True
                    await self._ensure_csrf(force=True)
                    continue
                raise LavkaAuthError(
                    f"Lavka session is not authorized (HTTP {resp.status_code}). "
                    "Re-capture your Yandex cookies."
                )
            if resp.status_code >= 500 and attempt < max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            if resp.status_code >= 400:
                # Keep the upstream body server-side; don't echo it to the caller.
                raise LavkaApiError(
                    f"Lavka API error on {name}: HTTP {resp.status_code}",
                    status=resp.status_code,
                )
            try:
                return resp.json()
            except ValueError as exc:
                raise LavkaApiError(f"Non-JSON response from {name}.") from exc

        raise LavkaApiError(f"Failed to call {name}: {last_exc}")

    # -- trimming ----------------------------------------------------------

    @staticmethod
    def _trim_product(item: dict[str, Any]) -> dict[str, Any]:
        return {
            # `id` (a hash) is what add_to_cart needs; `slug` (deepLink) is what
            # get_product needs.
            "id": _pick(item, "id", "product_id"),
            "slug": _pick(item, "deepLink", "slug", "productId"),
            "title": _pick(item, "title", "name", default=""),
            "price": _to_amount(_pick(item, "currentPrice", "price", "pricePerItem")),
            "old_price": _to_amount(_pick(item, "oldPrice", "old_price")),
            "quantity_label": _pick(item, "amount", "quantity", "weight", default=""),
            "in_stock": _pick(item, "available", "in_stock", "inStock", default=True),
        }

    @staticmethod
    def _trim_cart_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": _pick(item, "id", "product_id"),
            "title": _pick(item, "title", "name", default=""),
            "quantity": _pick(item, "quantity", "count", "qty", default=1),
            "price": _to_amount(_pick(item, "currentPrice", "price")),
            "quantity_label": _pick(item, "amount", "weight", default=""),
            # True = in the cart but NOT orderable from the current depot (it's
            # only stocked in "Большая Лавка"/supermarket, or sold out here).
            "unavailable_on_depot": bool(_pick(item, "isUnavailableOnDepot", default=False)),
        }

    def _normalize_cart(self, raw: Any) -> dict[str, Any]:
        data = raw.get("cart") if isinstance(raw, dict) and "cart" in raw else raw
        data = data if isinstance(data, dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        items = [i for i in items if isinstance(i, dict)]
        if not items:
            return {
                "cart_id": _pick(data, "cartId"),
                "cart_version": _pick(data, "cartVersion"),
                "items": [],
                "item_count": 0,
                "subtotal": 0.0,
                "discount": 0.0,
                "delivery_fee": 0.0,
                "total": 0.0,
                "eta": None,
                "available_for_checkout": None,
                "checkout_blocked_reason": None,
                "unavailable_items": [],
                "warning": None,
            }
        # Real breakdown from explicit cart fields (not inferred):
        #   total = subtotal - discount + delivery
        subtotal = _to_amount(_pick(data, "totalItemsPrice", "totalItemsPriceValue"))
        total = _to_amount(_pick(data, "totalPriceValue", "totalPrice"))
        discount = _to_amount(_pick(data, "totalDiscountValue")) or 0.0
        order_conditions = data.get("orderConditions") if isinstance(data.get("orderConditions"), dict) else {}
        delivery = _to_amount(_pick(order_conditions, "deliveryCost", "fullDeliveryCost"))
        if delivery is None and subtotal is not None and total is not None:
            # Fallback if the explicit field is missing.
            delivery = max(0.0, round(total - subtotal + discount, 2))
        trimmed = [self._trim_cart_item(it) for it in items]
        unavailable = [i["title"] for i in trimmed if i.get("unavailable_on_depot")]
        available_for_checkout = _pick(data, "availableForCheckout")
        blocked_reason = _pick(data, "checkoutUnavailableReason")
        # A plain-language warning the model reads and acts on (not just a flag).
        warning = None
        if unavailable:
            warning = (
                "These items are in the cart but CANNOT be ordered from the current "
                "store — they exist only in «Большая Лавка» (the big store) or are "
                f"sold out here: {', '.join(unavailable)}. Remove or replace them "
                "(update_cart_item(..., 0)) before checkout, or the order will fail."
            )
        elif available_for_checkout is False:
            warning = (
                "The cart cannot be checked out right now"
                + (f" (reason: {blocked_reason})" if blocked_reason else "")
                + ". Fix the flagged items before ordering."
            )
        return {
            "cart_id": _pick(data, "cartId"),
            "cart_version": _pick(data, "cartVersion"),
            "items": trimmed,
            "item_count": _pick(data, "totalItemsCount", default=len(items)),
            "subtotal": subtotal,
            "discount": discount,
            "delivery_fee": delivery,
            "total": total,
            "eta": _pick(order_conditions, "eta"),
            # Ordering-readiness: available_for_checkout False => don't try to order.
            "available_for_checkout": available_for_checkout,
            "checkout_blocked_reason": blocked_reason,
            "unavailable_items": unavailable,
            "warning": warning,
        }

    @staticmethod
    def _payment_method(raw: dict[str, Any]) -> dict[str, Any] | None:
        pm = raw.get("paymentMethod") if isinstance(raw.get("paymentMethod"), dict) else None
        if not pm:
            return None
        card = ((pm.get("meta") or {}).get("card")) or {}
        return {
            "type": pm.get("type"),
            "id": pm.get("id"),
            "system": card.get("system"),
            "bank": card.get("cardBank"),
        }

    # -- catalog -----------------------------------------------------------

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        body = {
            **self._base_body(),
            "text": query,
            "productsLimit": limit,
            "subcategoriesLimit": 0,
            "useRetail": self._ctx("useRetail", True),
            "source": "manual_input",
        }
        raw = await self._call("search", body)
        products = raw.get("cacheProducts") if isinstance(raw, dict) else None
        products = products if isinstance(products, list) else []
        return [self._trim_product(p) for p in products if isinstance(p, dict)][:limit]

    async def get_product(self, slug: str) -> dict[str, Any]:
        body = {
            **self._base_body(),
            "productId": slug,
            "needCatalogPaths": True,
            "isEcomboReward": False,
            "rewardPriceTemplate": "",
            "enableUnavailable": True,
        }
        raw = await self._call("product", body)
        product = raw.get("product") if isinstance(raw, dict) else None
        product = product if isinstance(product, dict) else {}
        detail = self._trim_product(product)
        detail["description"] = _pick(product, "description", "longTitle", default="")
        detail["brand"] = _pick(product, "brand", default="")
        return detail

    # -- cart --------------------------------------------------------------

    async def _get_cart_raw(self) -> dict[str, Any]:
        raw = await self._call("cart_get", self._base_body())
        data = raw.get("cart") if isinstance(raw, dict) and "cart" in raw else raw
        return data if isinstance(data, dict) else {}

    async def get_cart(self) -> dict[str, Any]:
        return self._normalize_cart(await self._get_cart_raw())

    def _cart_write_body(self, items: list[dict[str, Any]], cart: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": items,
            "deliveryTimeInfo": self._ctx("deliveryTimeInfo"),
            "deliveryType": self._ctx("deliveryType"),
            "position": self._position(),
            "additionalData": self._ctx("additionalData") or {},
            "cartId": cart.get("cartId"),
            "cartVersion": cart.get("cartVersion"),
            "idempotencyToken": _new_idempotency_token(),
            "isUserOrderEdit": False,
            "depotType": self._ctx("depotType", "regular"),
        }

    @staticmethod
    def _item_id(it: dict[str, Any]) -> Any:
        return _pick(it, "id", "product_id")

    @classmethod
    def _current_qty(cls, cart: dict[str, Any], product_id: str) -> int:
        for it in cart.get("items") or []:
            if isinstance(it, dict) and cls._item_id(it) == product_id:
                try:
                    return int(float(it.get("quantity") or it.get("count") or 0))
                except (TypeError, ValueError):
                    return 0
        return 0

    @classmethod
    def _price_of(cls, cart: dict[str, Any], product_id: str) -> Any:
        """Lavka validates a numeric `price` on every cart write, including
        removals — reuse the price already in the cart for this item."""
        for it in cart.get("items") or []:
            if isinstance(it, dict) and cls._item_id(it) == product_id:
                return _pick(it, "price", "currentPrice")
        return None

    def _cart_item_body(self, product_id: str, quantity: int, price: Any) -> dict[str, Any]:
        return {
            "id": product_id,
            "quantity": str(quantity),
            "pricePerCount": "1",
            "quantityType": "unit",
            "price": ("" if price is None else str(price)),
            "title": "",
            "currency": "RUB",
        }

    async def _cart_mutate(self, build_items) -> dict[str, Any]:
        """Read-modify-write a cart update, robust to the cart being a single
        shared server-side resource under optimistic concurrency.

        The cart is guarded by `cartVersion`; a concurrent writer (parallel tool
        calls, a second session, or the Lavka app) makes our write 409. We
        serialize our own writes with a process lock, and on a 409 re-read the
        fresh cart and rebuild the update — so concurrent adds queue up instead
        of erroring. ``build_items(cart_raw)`` returns the item bodies to send,
        recomputed from the just-read cart each attempt (None/[] = nothing to do).
        """
        async with _CART_WRITE_LOCK:
            for attempt in range(_CART_CONFLICT_RETRIES + 1):
                cart = await self._get_cart_raw()
                items = build_items(cart)
                if not items:
                    return self._normalize_cart(cart)
                try:
                    raw = await self._call("cart_update", self._cart_write_body(items, cart))
                    return self._normalize_cart(raw)
                except LavkaApiError as exc:
                    if exc.status == 409 and attempt < _CART_CONFLICT_RETRIES:
                        await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    raise
        raise LavkaApiError("cart update failed after conflict retries", status=409)

    async def add_to_cart(
        self, product_id: str, quantity: int = 1, *, price: float | None = None
    ) -> dict[str, Any]:
        # cart_update sets an ABSOLUTE quantity, so add = current + delta.
        def build(cart: dict[str, Any]) -> list[dict[str, Any]]:
            target = self._current_qty(cart, product_id) + quantity
            p = price if price is not None else self._price_of(cart, product_id)
            return [self._cart_item_body(product_id, target, p)]

        result = await self._cart_mutate(build)
        # Verify the item actually landed. Lavka silently drops items that turn
        # out to be unavailable at the current store — the caller must know.
        if not any(i.get("id") == product_id for i in result.get("items") or []):
            note = (
                "This item did NOT end up in the cart — Lavka dropped it (sold out "
                "at the current store, or only in «Большая Лавка»). Re-add to retry, "
                "or pick an in-stock alternative; do not assume it was added."
            )
            result["warning"] = f"{result['warning']} {note}".strip() if result.get("warning") else note
        return result

    async def set_cart_item(self, product_id: str, quantity: int) -> dict[str, Any]:
        def build(cart: dict[str, Any]) -> list[dict[str, Any]]:
            return [self._cart_item_body(product_id, quantity, self._price_of(cart, product_id))]

        return await self._cart_mutate(build)

    async def clear_cart(self) -> dict[str, Any]:
        def build(cart: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                self._cart_item_body(self._item_id(it), 0, _pick(it, "price", "currentPrice"))
                for it in (cart.get("items") or [])
                if isinstance(it, dict) and self._item_id(it)
            ]

        return await self._cart_mutate(build)

    # -- checkout / orders -------------------------------------------------

    async def checkout_preview(self) -> dict[str, Any]:
        """Order summary. Charges nothing — totals come straight from the cart."""
        cart = await self._get_cart_raw()
        summary = self._normalize_cart(cart)
        summary["address"] = self._ctx("additionalData") or None
        summary["payment_method"] = self._payment_method(cart)
        cashback = cart.get("cashback") if isinstance(cart.get("cashback"), dict) else {}
        summary["cashback_available"] = cashback.get("availableForPayment")
        return summary

    async def _service_info_depot_id(self) -> str | None:
        """Resolve the numeric depot id for the current position (needed by submit)."""
        if self._client is None:
            raise LavkaConfigError("Client used outside of an async context.")
        await self._ensure_csrf()
        pos = self._position().get("location")
        if not pos:
            return None
        params = {
            "position[location][0]": pos[0],
            "position[location][1]": pos[1],
            "fallbackCurrencySign": self._ctx("currencySign", "₽"),
            "depotType": self._ctx("depotType", "regular"),
        }
        resp = await self._client.get(
            "/api/v1/providers/v2/service-info", params=params, headers=self._lavka_headers()
        )
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        return _pick(data, "depotId")

    def _submit_position(self, depot_id: str | None) -> dict[str, Any]:
        ad = self._ctx("additionalData") or {}
        loc = self._position().get("location") or []
        return {
            "city": ad.get("city", ""),
            "street": ad.get("street", ""),
            "house": str(ad.get("house", "")),
            "flat": str(ad.get("flat", "")),
            "entrance": str(ad.get("entrance", "")),
            "floor": str(ad.get("floor", "")),
            "comment": ad.get("comment", ""),
            "doorcode": ad.get("doorcode", ""),
            "doorbellName": ad.get("doorbellName", ""),
            "buildingName": "",
            "country": self._ctx("country", "Россия"),
            "placeId": self._ctx("placeId", ""),
            "depotId": depot_id or "",
            "location": loc,
            "leftAtDoor": False,
            "meetOutside": False,
            "noDoorCall": False,
        }

    async def place_order(
        self,
        *,
        confirmed_total: float | None = None,
        expected_cart_version: int | None = None,
        poll: int = 6,
    ) -> dict[str, Any]:
        """Submit the order (real charge on the on-file card) and poll payment.

        Re-reads the cart at submit time and ABORTS if it drifted from what was
        previewed/confirmed — so the card is never charged a total (or a cart
        version) the user did not approve. Returns the order id and payment
        status; on 3-D Secure the status is ``wait_user_action`` with a
        ``redirect_url`` to finish paying. Body shape captured live 2026-07-19.
        """
        cart = await self._get_cart_raw()
        summary = self._normalize_cart(cart)
        live_version = cart.get("cartVersion")
        live_total = summary.get("total")

        # Fail closed on any drift between preview/confirm and now.
        if expected_cart_version is not None and live_version != expected_cart_version:
            raise LavkaApiError(
                "Cart changed since the preview (version "
                f"{expected_cart_version} → {live_version}). Re-run checkout_preview "
                "and confirm the new total before ordering."
            )
        if confirmed_total is not None and (
            live_total is None or abs(float(live_total) - float(confirmed_total)) > _PRICE_TOLERANCE
        ):
            raise LavkaApiError(
                f"Cart total changed since you confirmed ({confirmed_total} → {live_total}). "
                "Re-run checkout_preview and confirm the new total before ordering."
            )
        # Don't charge for a cart Lavka won't check out (e.g. items only in
        # «Большая Лавка», sold out, or over a quantity limit).
        if cart.get("availableForCheckout") is False:
            reason = summary.get("checkout_blocked_reason") or "unknown"
            bad = summary.get("unavailable_items") or []
            detail = f" Unavailable items: {', '.join(bad)}." if bad else ""
            raise LavkaApiError(
                f"Cart is not available for checkout (reason: {reason}).{detail} "
                "Fix the cart (remove/replace those items) and preview again."
            )

        payment = self._payment_method(cart) or {}
        cashback = cart.get("cashback") if isinstance(cart.get("cashback"), dict) else {}
        depot_id = await self._service_info_depot_id()
        body = {
            "cartId": cart.get("cartId"),
            "cartVersion": live_version,
            "flowVersion": _pick(cart, "orderFlowVersion", default="grocery_flow_v1"),
            "position": self._submit_position(depot_id),
            "paymentMethodType": payment.get("type") or "card",
            "paymentMethodId": payment.get("id"),
            "cashback": {"walletId": cashback.get("walletId")} if cashback.get("walletId") else {},
            "useRover": False,
            "depotOrderContext": {
                "depotType": self._ctx("depotType", "regular"),
                "position": self._position().get("location") or [],
            },
        }
        # Never retry the submit — a lost response could place a second order.
        raw = await self._call("order_create", body, retry=False)
        data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
        order_id = _pick(data or {}, "orderId", "order_id", "id")
        if not order_id:
            raise LavkaApiError(
                "Order submit returned no order id — the order may not have been "
                "placed. Check the Lavka app before retrying."
            )
        result: dict[str, Any] = {"order_id": order_id, "payment_status": None, "redirect_url": None}
        if poll:
            payment_state = await self._poll_payment(order_id, attempts=poll)
            result["payment_status"] = payment_state.get("status")
            result["redirect_url"] = payment_state.get("redirect_url")
        return result

    async def _poll_payment(self, order_id: str, *, attempts: int = 6) -> dict[str, Any]:
        """Poll payment status; stop on a terminal/actionable state."""
        status = None
        redirect = None
        for i in range(attempts):
            raw = await self._call("payment_status", {"orderId": order_id, "paymentType": "card"})
            data = raw if isinstance(raw, dict) else {}
            status = _pick(data, "status")
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            redirect = payload.get("redirectUrl")
            if status in ("success", "paid", "hold", "wait_user_action", "failed", "rejected"):
                break
            if i < attempts - 1:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * 2)
        return {"status": status, "redirect_url": redirect}

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an order by id."""
        if self._client is None:
            raise LavkaConfigError("Client used outside of an async context.")
        await self._ensure_csrf()
        safe_id = quote(str(order_id), safe="")
        resp = await self._client.post(
            f"/api/v1/orders/{safe_id}/cancel", json={}, headers=self._lavka_headers()
        )
        if resp.status_code in (401, 403):
            raise LavkaAuthError(
                f"Lavka session is not authorized (HTTP {resp.status_code}). "
                "Re-capture your Yandex cookies."
            )
        return {"order_id": order_id, "cancelled": resp.status_code < 400, "status_code": resp.status_code}

    # -- addresses / geo ---------------------------------------------------

    async def list_addresses(self) -> list[dict[str, Any]]:
        """The user's saved Lavka delivery addresses."""
        raw = await self._call("favorite_addresses", {})
        arr = raw if isinstance(raw, list) else (
            list(raw.values()) if isinstance(raw, dict) else []
        )
        out = []
        for a in arr:
            if not isinstance(a, dict):
                continue
            ad = a.get("address") or {}
            loc = ad.get("location") or []
            lon, lat = (loc[0], loc[1]) if isinstance(loc, list) and len(loc) >= 2 else (None, None)
            out.append(
                {
                    "address_id": a.get("addressId"),
                    "label": _pick(ad, "label", "shortAddress", "fullAddress"),
                    "city": ad.get("city"),
                    "street": ad.get("street"),
                    "house": ad.get("house"),
                    "short_address": ad.get("shortAddress"),
                    "full_address": ad.get("fullAddress"),
                    "place_id": ad.get("placeId"),
                    "country": ad.get("country"),
                    "flat": ad.get("flat"),
                    "entrance": ad.get("entrance"),
                    "floor": ad.get("floor"),
                    "comment": ad.get("comment"),
                    "lat": lat,
                    "lon": lon,
                }
            )
        return out

    async def resolve_address(self, query: str) -> dict[str, Any]:
        """Resolve free-text address → coords + structured city/street/house.

        Works for any city: geo-suggest picks the best match, reverse-geocode
        fills in the structured fields.
        """
        sbody: dict[str, Any] = {"query": query, "action": "user_input", "lang": "ru"}
        pos = self._position()
        if pos.get("location"):
            lon, lat = pos["location"][0], pos["location"][1]
            sbody["location"] = {"lat": lat, "lon": lon}
        sres = await self._call("geo_suggest", sbody)
        suggestions = sres if isinstance(sres, list) else next(
            (v for v in (sres.values() if isinstance(sres, dict) else []) if isinstance(v, list)),
            [],
        )
        first = next(
            (s for s in suggestions if isinstance(s, dict) and isinstance(s.get("position"), list)),
            None,
        )
        if not first:
            raise LavkaApiError(f"No address match for {query!r}.")
        lon, lat = first["position"][0], first["position"][1]
        gres = await self._call(
            "geo_geocode",
            {"point": {"lon": lon, "lat": lat}, "lang": "ru", "suppressError": True, "action": "pin_drop"},
        )
        g = gres if isinstance(gres, dict) else {}
        return {
            "lat": _to_amount(_pick(g, "lat")) or lat,
            "lon": _to_amount(_pick(g, "lon")) or lon,
            "city": g.get("city"),
            "street": g.get("street"),
            "house": g.get("house"),
            "entrance": g.get("entrance"),
            "place_id": _pick(g, "uri") or _pick(first, "uri"),
            "country": g.get("country"),
            "text": _pick(g, "text") or _pick(first, "full", "label", "title"),
        }

    async def tracked_orders(self) -> list[dict[str, Any]]:
        raw = await self._call("tracked_orders")
        orders = raw.get("orders") if isinstance(raw, dict) else raw
        orders = orders if isinstance(orders, list) else []
        trimmed = []
        for o in orders:
            if not isinstance(o, dict):
                continue
            trimmed.append(
                {
                    "order_id": _pick(o, "orderId", "order_id", "id", "shortOrderId"),
                    "status": _pick(o, "status", "state"),
                    "eta_minutes": _pick(o, "eta", "etaMinutes"),
                    "title": _pick(o, "title", "statusTitle"),
                }
            )
        return trimmed
