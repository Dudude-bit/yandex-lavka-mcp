#!/usr/bin/env python3
"""Manual smoke test against the real Lavka API using saved cookies.

Run after importing cookies + setting a location:
    python scripts/smoke.py "молоко"

Exercises read-only calls only (never places an order).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yandex_lavka_mcp.client import LavkaClient  # noqa: E402
from yandex_lavka_mcp.config import load_config  # noqa: E402


async def main(query: str) -> None:
    config = load_config()
    print("authenticated:", config.is_authenticated())
    print("location set:", config.location.is_set())
    async with LavkaClient(config) as client:
        print(f"\nsearch({query!r}):")
        for p in await client.search(query, limit=5):
            print("  ", p)
        print("\ncart:")
        print("  ", await client.get_cart())


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "молоко"
    asyncio.run(main(q))
