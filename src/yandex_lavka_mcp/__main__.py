"""Entry point: run the Lavka MCP over stdio."""

from __future__ import annotations

from .server import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
