#!/usr/bin/env python3
"""Get Yandex's captcha pass (`spravka` cookie) for a server whose IP is blocked.

When Lavka answers a server with a captcha (the MCP says "Yandex anti-bot
returned a captcha"), it is the server's IP that is flagged, not the session.
Solve the captcha once *through that IP* and give the server the resulting
`spravka` cookie — requests from there pass again (the cookie lives ~30 days).

Usage:
    ssh -f -N -D 1080 <your-server>        # SOCKS proxy through the server's IP
    uv run --with playwright python scripts/solve_captcha.py --proxy socks5://127.0.0.1:1080

A Chrome window opens on the captcha (a throwaway profile, not your browser);
solve it there. The script waits until Lavka really answers with data, then
prints the `spravka` value to stdout (everything else goes to stderr), so it
can be piped straight into the server's YANDEX_LAVKA_MCP_SPRAVKA secret.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

LAVKA = "https://lavka.yandex.ru"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def search(ctx) -> dict:
    """One real Lavka API call with the browser's cookies (doesn't touch the page)."""
    html = ctx.request.get(LAVKA + "/").text()
    m = re.search(r'"csrfToken":"([^"]+)"', html)
    r = ctx.request.post(
        LAVKA + "/api/v1/providers/search/v3/lavka",
        data={"text": "молоко"},
        headers={
            "x-csrf-token": m.group(1) if m else "",
            "x-requested-with": "XMLHttpRequest",
            "x-captcha-service": "lavka",
        },
    )
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", required=True, help="proxy through the blocked IP, e.g. socks5://127.0.0.1:1080")
    ap.add_argument("--timeout", type=int, default=900, help="seconds to wait for the solve (default 900)")
    args = ap.parse_args()

    profile = tempfile.mkdtemp(prefix="lavka-captcha-")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                profile, channel="chrome", headless=False, proxy={"server": args.proxy}
            )
            log("egress IP: " + ctx.request.get("https://ipinfo.io/ip").text().strip())
            first = search(ctx)
            if first.get("type") != "captcha":
                log("No captcha from this IP — nothing to solve.")
                return 1
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(first["captcha"]["captcha-page"], wait_until="domcontentloaded")
            log("Solve the captcha in the Chrome window (don't close it)...")
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                time.sleep(3)
                if search(ctx).get("type") != "captcha":
                    break
            else:
                log("Timed out: captcha not solved.")
                return 1
            spravka = next((c["value"] for c in ctx.cookies() if c["name"] == "spravka"), None)
            ctx.close()
        if not spravka:
            log("Lavka passes, but no `spravka` cookie was set.")
            return 1
        log("Solved: Lavka answers with data now.")
        print(spravka)
        return 0
    finally:
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
