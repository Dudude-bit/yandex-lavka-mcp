"""Tests for config loading from the environment."""

from __future__ import annotations

import json

from yandex_lavka_mcp.config import load_config


def test_spravka_env_overlays_config_cookies(monkeypatch):
    # The captcha pass lives apart from the session cookies so a cookie refresh
    # (a new CONFIG_JSON) can't drop it.
    cfg = {"cookies": {"Session_id": "s", "spravka": "stale"}}
    monkeypatch.setenv("YANDEX_LAVKA_MCP_CONFIG_JSON", json.dumps(cfg))
    monkeypatch.setenv("YANDEX_LAVKA_MCP_SPRAVKA", " fresh\n")
    assert load_config().cookies == {"Session_id": "s", "spravka": "fresh"}


def test_config_untouched_without_spravka_env(monkeypatch, tmp_path):
    monkeypatch.delenv("YANDEX_LAVKA_MCP_CONFIG_JSON", raising=False)
    monkeypatch.delenv("YANDEX_LAVKA_MCP_SPRAVKA", raising=False)
    monkeypatch.setenv("YANDEX_LAVKA_MCP_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"cookies": {"Session_id": "s"}}))
    assert load_config().cookies == {"Session_id": "s"}
