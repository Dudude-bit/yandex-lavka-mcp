# yandex-lavka-mcp

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

## Develop

```bash
uv pip install -e ".[dev]"
pytest
```

## Security & privacy

- Cookies and address live only in `~/.config/yandex-lavka-mcp/config.json`
  (chmod 600), git-ignored. Never commit them.
- The server never adds payment methods or changes account settings.
- Ordering always requires an explicit confirmed total.

## License

[MIT](LICENSE). Unofficial project, not affiliated with Yandex.
