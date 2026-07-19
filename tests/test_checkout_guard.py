"""Tests for the two-step checkout safety guard — no network."""

from __future__ import annotations

import time

import pytest

from yandex_lavka_mcp import server


def _fresh(**kw):
    """A preview dict that passes the freshness (TTL) check."""
    return {"_ts": time.time(), **kw}


def _call(tool):
    """FastMCP may wrap the function; unwrap to the raw coroutine fn."""
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def _clear_preview():
    server._LAST_PREVIEW.clear()
    yield
    server._LAST_PREVIEW.clear()


async def test_confirm_refused_without_preview():
    result = await _call(server.confirm_order)(confirmed_total=100.0)
    assert result["ok"] is False
    assert "preview" in result["error"].lower()


async def test_confirm_refused_on_total_mismatch():
    server._LAST_PREVIEW.update(_fresh(total=178.0))
    result = await _call(server.confirm_order)(confirmed_total=999.0)
    assert result["ok"] is False
    assert "does not match" in result["error"]


async def test_confirm_refused_when_preview_has_no_total():
    server._LAST_PREVIEW.update(_fresh(total=None))
    result = await _call(server.confirm_order)(confirmed_total=178.0)
    assert result["ok"] is False
    assert "no total" in result["error"].lower()


async def test_confirm_refused_when_preview_stale():
    # A preview older than the TTL must not be confirmable.
    server._LAST_PREVIEW.update({"total": 178.0, "_ts": time.time() - server._PREVIEW_TTL_SECONDS - 1})
    result = await _call(server.confirm_order)(confirmed_total=178.0)
    assert result["ok"] is False
    assert "stale" in result["error"].lower()


async def test_cart_change_invalidates_preview():
    server._LAST_PREVIEW.update(_fresh(total=178.0))
    # add_to_cart clears the preview before doing any network work; it will then
    # fail on missing config, but the preview must already be cleared.
    await _call(server.add_to_cart)("some-id", 1)
    assert server._LAST_PREVIEW == {}


async def test_set_location_clears_preview(monkeypatch, tmp_path):
    # Redirect config writes to a temp dir so the real config is untouched.
    monkeypatch.setenv("YANDEX_LAVKA_MCP_CONFIG_DIR", str(tmp_path))
    server._LAST_PREVIEW.update(_fresh(total=500.0))
    _call(server.set_location)(lat=55.0, lon=37.0, label="B")
    assert server._LAST_PREVIEW == {}
