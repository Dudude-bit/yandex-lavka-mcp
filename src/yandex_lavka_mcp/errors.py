"""Typed errors surfaced to MCP tools as friendly messages."""

from __future__ import annotations


class LavkaError(Exception):
    """Base class for all Lavka client errors."""


class LavkaConfigError(LavkaError):
    """Missing or invalid configuration (no cookies, no location)."""


class LavkaAuthError(LavkaError):
    """Session expired or unauthorized (401/403). Re-capture cookies."""


class LavkaApiError(LavkaError):
    """Non-auth API failure (bad status, unexpected body)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
