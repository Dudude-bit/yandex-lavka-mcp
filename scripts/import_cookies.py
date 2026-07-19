#!/usr/bin/env python3
"""Import a Yandex session into the Lavka MCP config (one-time setup).

Usage:
    # from a raw Cookie header copied from the browser devtools:
    python scripts/import_cookies.py --header "Session_id=...; yandexuid=...; L=..."

    # or from a JSON dump {"Session_id": "...", ...}:
    python scripts/import_cookies.py --json cookies.json

Optionally pin a delivery location:
    ... --lat 55.751244 --lon 37.618423 --label "Home"

Cookies are written to ~/.config/yandex-lavka-mcp/config.json (chmod 600) and
never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yandex_lavka_mcp.config import Location, load_config, save_config  # noqa: E402


def parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--header", help="Raw Cookie header string")
    src.add_argument("--json", help="Path to a JSON file mapping cookie name->value")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--address-id")
    parser.add_argument("--label")
    args = parser.parse_args()

    if args.header:
        cookies = parse_cookie_header(args.header)
    else:
        with open(args.json, encoding="utf-8") as fh:
            cookies = dict(json.load(fh))

    if not cookies:
        print("No cookies parsed.", file=sys.stderr)
        return 1

    config = load_config()
    config.cookies.update(cookies)
    if args.lat is not None or args.lon is not None or args.address_id:
        config.location = Location(
            lat=args.lat, lon=args.lon, address_id=args.address_id, label=args.label
        )

    path = save_config(config)
    have_session = bool(cookies.get("Session_id") or cookies.get("Session_id2"))
    print(f"Saved {len(cookies)} cookies to {path}")
    print(f"Session cookie present: {have_session}")
    if config.location.is_set():
        print(f"Location set: {config.location.label or config.location.to_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
