#!/usr/bin/env python3
"""Extract Yandex/Lavka cookies from the local Chrome profile (macOS).

This reads Chrome's cookie store, decrypts it with the key from your macOS
Keychain, and writes the Yandex session cookies into the Lavka MCP config so the
server can authenticate as you. You may get one Keychain prompt — click Allow.

    python scripts/extract_chrome_cookies.py            # auto-pick profile
    python scripts/extract_chrome_cookies.py --profile "Profile 1"

Requires: pip install 'yandex-lavka-mcp[browser]'  (adds `cryptography`).
Falls back to nothing destructive — only reads cookies and updates config.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yandex_lavka_mcp.config import load_config, save_config  # noqa: E402

CHROME_DIR = Path.home() / "Library/Application Support/Google/Chrome"
WANTED_HOSTS = ("yandex.ru", "lavka.yandex.ru", ".yandex.ru")
# Cookies that matter for an authenticated Yandex session.
KEY_COOKIES = ("Session_id", "Session_id2", "yandexuid", "yandex_login", "L", "i", "sessar")


def _keychain_password() -> bytes:
    out = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(
            "Could not read the Chrome Safe Storage key from Keychain: "
            + (out.stderr or "").strip()
        )
    return out.stdout.strip().encode("utf-8")


def _derive_key(password: bytes) -> bytes:
    from cryptography.hazmat.primitives.hashes import SHA1
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=SHA1(), length=16, salt=b"saltysalt", iterations=1003)
    return kdf.derive(password)


def _decrypt(encrypted: bytes, key: bytes) -> str:
    if not encrypted:
        return ""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if encrypted[:3] not in (b"v10", b"v11"):
        # Not encrypted (rare) — return as-is.
        try:
            return encrypted.decode("utf-8", "ignore")
        except Exception:
            return ""
    iv = b" " * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    data = dec.update(encrypted[3:]) + dec.finalize()
    # strip PKCS7 padding
    if data:
        pad = data[-1]
        if 0 < pad <= 16:
            data = data[:-pad]
    # Newer Chrome prepends a 32-byte SHA256 domain hash — drop non-UTF8 prefix.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data[32:].decode("utf-8", "ignore")


def _profiles(explicit: str | None) -> list[Path]:
    if explicit:
        return [CHROME_DIR / explicit]
    found = [CHROME_DIR / "Default"]
    found += [Path(p) for p in glob.glob(str(CHROME_DIR / "Profile *"))]
    return [p for p in found if (p / "Cookies").exists()]


def _read_cookies(cookies_db: Path, key: bytes) -> dict[str, str]:
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    shutil.copy2(cookies_db, tmp)  # copy to dodge Chrome's lock
    try:
        con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
        rows = con.execute(
            "SELECT host_key, name, encrypted_value, value FROM cookies"
        ).fetchall()
        con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    out: dict[str, str] = {}
    for host, name, enc, plain in rows:
        if not any(host.endswith(h) or host == h for h in WANTED_HOSTS):
            continue
        value = plain or _decrypt(enc, key)
        if value:
            out[name] = value
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help='Chrome profile dir, e.g. "Default" or "Profile 1"')
    args = parser.parse_args()

    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("Missing dependency. Run:  pip install 'yandex-lavka-mcp[browser]'", file=sys.stderr)
        return 2

    if not CHROME_DIR.exists():
        print(f"Chrome dir not found at {CHROME_DIR}", file=sys.stderr)
        return 1

    try:
        key = _derive_key(_keychain_password())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    best: dict[str, str] = {}
    best_profile = None
    for profile in _profiles(args.profile):
        cookies = _read_cookies(profile / "Cookies", key)
        has_session = "Session_id" in cookies or "Session_id2" in cookies
        if has_session and len(cookies) > len(best):
            best, best_profile = cookies, profile.name

    if not best:
        print(
            "No Yandex session cookies found. Make sure you are logged into "
            "lavka.yandex.ru in Chrome, then retry (or pass --profile).",
            file=sys.stderr,
        )
        return 1

    keep = {k: v for k, v in best.items() if k in KEY_COOKIES} or best
    config = load_config()
    config.cookies.update(keep)
    path = save_config(config)
    print(f"Profile: {best_profile}")
    print(f"Saved {len(keep)} cookies to {path}")
    print(f"Session cookie present: {config.is_authenticated()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
