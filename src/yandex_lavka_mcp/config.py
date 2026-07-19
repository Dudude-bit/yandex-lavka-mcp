"""Configuration and session loading for the Lavka MCP.

Config lives at ``~/.config/yandex-lavka-mcp/config.json`` and holds the
captured Yandex session cookies, any pinned headers, and the delivery location.
Nothing here is ever logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    override = os.environ.get("YANDEX_LAVKA_MCP_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base).expanduser() / "yandex-lavka-mcp"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Location:
    """Delivery point. Lavka is location-scoped: catalog and cart depend on it."""

    lat: float | None = None
    lon: float | None = None
    address_id: str | None = None
    label: str | None = None

    def is_set(self) -> bool:
        return (self.lat is not None and self.lon is not None) or bool(self.address_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "address_id": self.address_id,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Location":
        data = data or {}
        return cls(
            lat=data.get("lat"),
            lon=data.get("lon"),
            address_id=data.get("address_id"),
            label=data.get("label"),
        )


# Sensible defaults for the request "context" Lavka expects on every call.
# `additionalData` (the delivery address block) is captured from the live
# session; the rest rarely change.
DEFAULT_CONTEXT: dict[str, Any] = {
    "depotType": "regular",  # "regular" = лавка у дома, "supermarket" = большая лавка
    "currencySign": "₽",
    "useRetail": True,
    "deliveryType": "eats_dispatch",
    "deliveryTimeInfo": {"tariff": "default", "kind": "on_demand"},
    "additionalData": {},
    "placeId": "",  # Yandex maps place id (ymapsbm1://...) for the delivery address
    "country": "Россия",
    "webCity": "213",  # Yandex region id (213 = Москва) → X-Lavka-Web-City header
    "locale": "ru-RU",  # → X-Lavka-Web-Locale header
}


@dataclass
class Config:
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    location: Location = field(default_factory=Location)
    # Delivery/request context Lavka wants on every call (address block, depot
    # type, currency, delivery type). Seeded from DEFAULT_CONTEXT.
    context: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONTEXT))
    # Optional overrides captured from live traffic (see endpoints.py). Empty by
    # default: the code falls back to the seeded defaults.
    base_url: str | None = None
    endpoints: dict[str, Any] = field(default_factory=dict)

    def is_authenticated(self) -> bool:
        # Session_id is the cookie Yandex uses to identify a logged-in user.
        return bool(self.cookies.get("Session_id") or self.cookies.get("Session_id2"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookies": self.cookies,
            "headers": self.headers,
            "location": self.location.to_dict(),
            "context": self.context,
            "base_url": self.base_url,
            "endpoints": self.endpoints,
        }


def _config_from_dict(data: dict[str, Any]) -> Config:
    context = dict(DEFAULT_CONTEXT)
    context.update(data.get("context") or {})
    return Config(
        cookies=dict(data.get("cookies") or {}),
        headers=dict(data.get("headers") or {}),
        location=Location.from_dict(data.get("location")),
        context=context,
        base_url=data.get("base_url"),
        endpoints=dict(data.get("endpoints") or {}),
    )


def load_config() -> Config:
    # In a container, inject the whole config as one env var (a Dokploy/K8s
    # secret) instead of a file on disk.
    inline = os.environ.get("YANDEX_LAVKA_MCP_CONFIG_JSON")
    if inline:
        return _config_from_dict(json.loads(inline))
    path = config_path()
    if not path.exists():
        return Config()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return _config_from_dict(data)


def save_config(config: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(config.to_dict(), fh, ensure_ascii=False, indent=2)
    # Owner-only: the file holds session cookies.
    os.chmod(path, 0o600)
    return path
