# yandex-lavka-mcp

[![PyPI](https://img.shields.io/pypi/v/yandex-lavka-mcp.svg)](https://pypi.org/project/yandex-lavka-mcp/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-black.svg)](https://modelcontextprotocol.io)

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant order
groceries from **Yandex Lavka** — search products, build a cart, and place a real
order — with an explicit human confirmation before any money is charged.

> [!WARNING]
> **Unofficial.** Yandex Lavka has no public API. This project talks to the same
> private web API that `lavka.yandex.ru` uses, authenticated with **your own**
> Yandex session cookies. It automates your own account, for your own shopping.
>
> - Not affiliated with or endorsed by Yandex. Using it may violate Yandex's
>   Terms of Service, and the private API can change or be blocked at any time.
> - `confirm_order` spends **real money** on your card. Use at your own risk.
> - Provided **as is**, without warranty (see [LICENSE](LICENSE)).

## What it does

| Tool | Charges? | What it does |
|------|:---:|------|
| `lavka_status` | — | Is the session + location set up? |
| `list_addresses` | — | Your saved Lavka addresses, by name. |
| `use_address` | — | Switch delivery to a saved address by name. |
| `set_delivery_address` | — | Set delivery to any address by text (any city). |
| `set_location` | — | Set delivery point by raw lat/lon. |
| `search_products` | — | Search the catalog at the current location. |
| `get_product` | — | Product detail. |
| `view_cart` | — | Show cart + total. |
| `add_to_cart` | — | Add an item. |
| `update_cart_item` | — | Set exact quantity (0 removes). |
| `clear_cart` | — | Empty the cart. |
| `checkout_preview` | **no** | Full summary: items, subtotal, discount, delivery, ETA, payment, total. |
| `confirm_order` | **YES** | Places the order and charges the on-file card. |
| `cancel_order` | — | Cancel an order by id. |
| `active_orders` | — | Currently tracked orders with status/ETA. |

**Money safety.** Placing an order is a deliberate two-step flow: `checkout_preview`
returns the full summary and charges nothing; `confirm_order(confirmed_total)`
refuses unless a preview was just run and you pass back the exact total it showed.
Change the cart and the preview is invalidated — you must preview again.

**3-D Secure.** `confirm_order` submits the order and charges the on-file card,
then polls payment status. If your bank requires 3-D Secure, `payment_status`
comes back `wait_user_action` and a `redirect_url` is returned — open it to
finish paying (a headless charge cannot complete 3DS). `cancel_order(order_id)`
cancels.

**Multiple locations / cities.** Catalog, prices and cart are location-scoped.
`use_address("Дача")` switches to a saved address; `set_delivery_address("Казань,
улица Баумана, 1", flat="12")` works for any address in any city (it geocodes via
Lavka's own address search).

## How it's built

- Python 3.12+ · [FastMCP](https://github.com/modelcontextprotocol/python-sdk) · `httpx`.
- `client.py` — the async API client (session auth, CSRF, request building, trims huge payloads).
- `endpoints.py` — every API path in one place (overridable from config, no code change).
- `server.py` — the MCP tools the assistant sees.

The API sits under `https://lavka.yandex.ru/api/v1/providers/*` (plus
`/api/v1/orders/submit` for placing orders). Requests need the CSRF token from
the homepage HTML plus `X-Lavka-Web-*` headers — the client handles this.

## Setup

### 1. Install

```bash
uv venv && uv pip install -e .
```

### 2. Provide your Yandex session (one time)

Log into Lavka in your browser first, then get the session cookies into
`~/.config/yandex-lavka-mcp/config.json`.

**macOS — pull cookies straight from Chrome** (one Keychain prompt → Allow):

```bash
uv pip install -e '.[browser]'
python scripts/extract_chrome_cookies.py          # auto-detects your profile
```

**Any OS — paste the Cookie header** from DevTools (Network → any
`lavka.yandex.ru` request → Request Headers → Cookie):

```bash
python scripts/import_cookies.py --header "Session_id=...; yandexuid=...; L=..."
```

Session cookies expire — re-run when calls start returning "session expired".

### 3. Set a delivery location

Copy `config.example.json` to `~/.config/yandex-lavka-mcp/config.json` and edit,
or set it from the assistant with `use_address` / `set_delivery_address`. The
catalog only works once a location is set. Smoke-test:

```bash
python scripts/smoke.py "молоко"
```

### 4. Register with your assistant

Claude Code:

```bash
claude mcp add yandex-lavka -- uv run --directory /path/to/yandex-lavka-mcp yandex-lavka-mcp
```

Claude Desktop (`mcpServers`):

```json
{
  "yandex-lavka": {
    "command": "uv",
    "args": ["run", "--directory", "/path/to/yandex-lavka-mcp", "yandex-lavka-mcp"]
  }
}
```

## Remote deploy (order from your phone)

By default the server speaks **stdio** (local clients). Set
`YANDEX_LAVKA_MCP_TRANSPORT=streamable-http` to expose it over HTTP so a hosted
instance can back a [claude.ai custom connector](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
(phone / web).

A prebuilt [`Dockerfile`](Dockerfile) is included. Secrets are injected at
runtime — never baked into the image:

```bash
docker build -t yandex-lavka-mcp .
docker run -p 8000:8000 \
  -e YANDEX_LAVKA_MCP_CONFIG_JSON="$(cat ~/.config/yandex-lavka-mcp/config.json)" \
  yandex-lavka-mcp
```

### Environment variables

| Var | Purpose |
|-----|---------|
| `YANDEX_LAVKA_MCP_TRANSPORT` | `stdio` (default) or `streamable-http`. |
| `YANDEX_LAVKA_MCP_HOST` / `_PORT` | Bind address for HTTP (default `0.0.0.0:8000` in Docker). |
| `YANDEX_LAVKA_MCP_CONFIG_JSON` | The whole `config.json` as one secret (instead of a file). |

### Authentication (any OIDC provider)

A public endpoint spends real money, so **protect it**. `claude.ai`'s custom
connector UI only supports **OAuth** (no static bearer / custom header — that
works only in Claude Code/Desktop). This server is a provider-agnostic OAuth 2.1
resource server: point it at *any* OpenID-Connect provider (Zitadel, Keycloak,
Auth0, Google, …) and it validates JWT access tokens against that provider's
JWKS and advertises it via OAuth protected-resource metadata.

Enable it by installing the `server` extra (`pip install '.[server]'`, already in
the Docker image) and setting:

| Var | Purpose |
|-----|---------|
| `YANDEX_LAVKA_MCP_OAUTH_ISSUER` | Your provider's issuer URL (enables OAuth). |
| `YANDEX_LAVKA_MCP_SERVER_URL` | Public URL of this MCP server (the resource). |
| `YANDEX_LAVKA_MCP_OAUTH_AUDIENCE` | Expected token audience (optional but recommended). |
| `YANDEX_LAVKA_MCP_OAUTH_SCOPES` | Space-separated required scopes (optional). |
| `YANDEX_LAVKA_MCP_OAUTH_SUBJECTS` | Allow-list of token `sub`s that may call the server (optional; strongest lock — every request spends *your* Lavka session). |
| `YANDEX_LAVKA_MCP_OAUTH_JWKS_URL` | Override JWKS URL (optional; else discovered). |

A network-exposed HTTP transport **refuses to start** unless OAuth is configured
(it spends real money). Set `YANDEX_LAVKA_MCP_ALLOW_INSECURE=1` only if you front
it with your own auth. Leaving OAuth unset is allowed for loopback/local use.

> Session cookies expire; when calls start failing, re-capture them and update
> the `YANDEX_LAVKA_MCP_CONFIG_JSON` secret. There is no headless Yandex login.

## Develop

```bash
uv pip install -e ".[dev]"
pytest
```

## One account = one cart

Lavka keeps a single server-side cart per account, guarded by an optimistic
`cartVersion`. This server serializes its own cart writes and retries on version
conflicts, so parallel tool calls in one session are safe. But **don't drive the
same Yandex account from two places at once** (e.g. this server *and* a second
MCP session, *and* the Lavka app): they all write the one shared cart, and you'll
see items from the other writer appear in yours. Use a single client at a time.

## Security & privacy

- Cookies and address live only in `~/.config/yandex-lavka-mcp/config.json`
  (chmod 600), git-ignored. Never commit them.
- The server never adds payment methods or changes account settings.
- Ordering always requires an explicit confirmed total.

## License

[MIT](LICENSE). Unofficial project, not affiliated with Yandex.
