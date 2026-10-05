"""MCP server for Yandex Lavka grocery ordering."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("yandex-lavka-mcp")
except PackageNotFoundError:  # a source checkout run without installing
    __version__ = "0+unknown"
