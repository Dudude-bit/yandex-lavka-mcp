# Generic image for running the MCP server over streamable-http (remote deploy).
# Secrets (Yandex cookies, OAuth config) are injected at runtime via env vars —
# never baked into the image. See README "Remote deploy".
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install deps first for better layer caching.
#
# From the lock, not from the version ranges. `uv pip install .` resolves
# afresh every build and ignores uv.lock entirely, so a build months after the
# last one silently picks up a new major of a dependency — mcp 2.x here, which
# starts and exits. The lock exists to say which versions this was tested with;
# this is what makes the image obey it.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv export --frozen --no-dev --extra server --format requirements-txt -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps . \
 && rm -f /tmp/requirements.txt

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app
USER app

# Defaults to stdio, so a bare `docker run -i` (and MCP introspection tools) get
# a working server. For a remote deploy, set
# YANDEX_LAVKA_MCP_TRANSPORT=streamable-http (+ OAuth env) — see README.
ENV YANDEX_LAVKA_MCP_HOST=0.0.0.0 \
    YANDEX_LAVKA_MCP_PORT=8000

EXPOSE 8000
CMD ["yandex-lavka-mcp"]
