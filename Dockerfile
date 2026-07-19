# Generic image for running the MCP server over streamable-http (remote deploy).
# Secrets (Yandex cookies, OAuth config) are injected at runtime via env vars —
# never baked into the image. See README "Remote deploy".
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system --no-cache ".[server]"

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app
USER app

ENV YANDEX_LAVKA_MCP_TRANSPORT=streamable-http \
    YANDEX_LAVKA_MCP_HOST=0.0.0.0 \
    YANDEX_LAVKA_MCP_PORT=8000

EXPOSE 8000
CMD ["yandex-lavka-mcp"]
