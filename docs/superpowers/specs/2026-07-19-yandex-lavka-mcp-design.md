# Yandex Lavka MCP — Design

Date: 2026-07-19
Status: Approved (user delegated all decisions)

## Goal

An MCP server that lets Claude drive the full Yandex Lavka grocery flow on the
user's behalf: search products, manage a cart, and place a real order — with an
explicit human confirmation before any money is charged.

## Key constraints

- **No official API.** Lavka has no public/documented API. The server talks to
  the same internal web API that `lavka.yandex.ru` uses, authenticated with the
  user's own Yandex session cookies.
- **Exact endpoints must be captured from live traffic.** Public reverse-
  engineering of the Lavka app API does not exist. The precise URLs, headers and
  payload shapes are captured once from the user's logged-in browser and pinned
  in a single `endpoints`/config module. Everything else is built to be
  independent of those exact values so a capture only touches one place.
- **Real money.** The final charge step is gated behind a two-step checkout: a
  preview tool returns the full order summary and charges nothing; a separate
  confirm tool performs the charge and is only ever called after the user says
  yes in chat.

## Non-goals

- Multi-user / hosted service. This is a single-user, local stdio MCP.
- Managing payment methods, adding cards, changing account settings.
- Reverse-engineering promo/loyalty internals beyond what the order summary
  naturally returns.

## Stack

- Python 3.12+ (dev machine runs 3.14), `uv` for env/deps.
- `mcp` (FastMCP) official SDK, **stdio** transport.
- `httpx` for the HTTP client. `pydantic` for tool I/O models.
- Architecture kept transport-agnostic so a later move to streamable-HTTP is a
  config change, not a rewrite.

## Architecture

Two layers, cleanly separated:

### 1. `LavkaClient` (`client.py`)

Low-level async httpx wrapper. Owns:

- Session auth: loads Yandex cookies (`Session_id`, `yandexuid`, `L`, etc.) and
  Lavka-specific headers from config; attaches them to every request.
- Location context: Lavka is location-scoped (a delivery point / depot). The
  client holds the current position (lat/lon or a saved address id) and passes
  it on catalog/cart calls.
- One method per API operation: `search`, `get_product`, `get_cart`,
  `add_to_cart`, `update_cart_item`, `clear_cart`, `checkout_preview`,
  `place_order`, `order_status`, `order_history`.
- **Response trimming**: Lavka payloads are huge. Each method returns a small
  normalized dict with only the fields the model needs (id, title, price,
  weight, in-stock, etc.), never the raw payload.
- Retry + clear auth-error surfacing (401/403 → "session expired, re-capture
  cookies" rather than a raw stack trace).

### 2. `server.py` — FastMCP tools

Thin tools over the client. Surface:

| Tool | Charges? | Purpose |
|------|----------|---------|
| `lavka_status` | no | Show whether session/config is set up and current delivery location. |
| `search_products(query)` | no | Search the catalog at the current location. |
| `get_product(product_id)` | no | Product detail: price, weight, description, stock. |
| `view_cart()` | no | Current cart contents + running total. |
| `add_to_cart(product_id, quantity)` | no | Add/increment an item. |
| `update_cart_item(product_id, quantity)` | no | Set exact quantity; 0 removes. |
| `clear_cart()` | no | Empty the cart. |
| `checkout_preview()` | **no** | Full order summary: items, subtotal, delivery fee, tips, ETA, address, payment method, grand total. Charges nothing. |
| `confirm_order()` | **YES** | Places the order and charges the card. Only call after explicit user confirmation in chat. |
| `order_status(order_id?)` | no | Status/ETA of the latest or a given order. |
| `order_history(limit)` | no | Recent orders. |

Each money-touching tool docstring instructs the model: never call
`confirm_order` without the user explicitly confirming the previewed summary.

## Config & auth

- Config file at `~/.config/yandex-lavka-mcp/config.json`:
  - `cookies`: dict of Yandex session cookies.
  - `headers`: any extra pinned headers captured (e.g. app/session tokens).
  - `location`: `{lat, lon}` or saved `address_id` + human label.
- Cookies are captured once by logging into `lavka.yandex.ru` in the user's
  Chrome and reading them out; a documented one-time step in the README.
- Secrets never logged. Config file is user-only readable.

## Endpoint capture module

`endpoints.py` centralizes every URL path, method, and the request-building
logic. Values are seeded with best-effort defaults and clearly marked where a
live capture is required. Correcting the integration after a capture, or after
Lavka changes its API, is edits to this one file.

## Error handling

- Auth failures (401/403): raise `LavkaAuthError` → tool returns a friendly
  "session expired, re-run cookie capture" message.
- Out-of-zone / no depot for location: explicit message, not a crash.
- Item out of stock at add/checkout: surfaced with the offending item.
- Network/5xx: bounded retry, then a clear failure message.

## Testing

- Unit tests for response trimming and the two-step checkout guard (confirm
  cannot run without a prior preview in the session) using recorded/mocked
  httpx responses — no live account needed in CI.
- A manual `scripts/smoke.py` for exercising real calls with real cookies.

## The one manual step

The only thing the user must do: **log into Lavka in Chrome once** so the exact
endpoints/cookies can be captured. Everything else is automated.
