# ---- Builder Stage: install MCP runtime dependencies ----
# Lean multi-stage build: builder creates isolated venv with only Gateway-API
# dependencies (mcp SDK + httpx as declared in opencode-gateway-mcp/pyproject.toml).
# No Postgres, Kafka, AWX, or collector libs.
FROM python:3.12-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir \
        "mcp>=2.0,<3" \
        "httpx>=0.27,<1.0" \
        "pydantic>=2.0,<3.0"

# ---- Runtime Stage: minimal production image ----
FROM python:3.12-slim AS runtime

# Create a dedicated non-root user (ADR 0031 read-only adapter — no host coupling).
RUN groupadd --system mcp && \
    useradd --system --no-log-init --gid mcp --create-home mcp

# Copy the virtual environment from the builder stage.
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy the completed MCP adapter source (the full read-only tool surface, ADR 0031).
# The server validates OPENCODE_GATEWAY_URL/OPENCODE_GATEWAY_API_KEY at startup and
# fails closed when either is missing; neither value is baked into image layers.
COPY opencode-gateway-mcp/src/opencode_gateway_mcp /app/opencode_gateway_mcp

# Switch to the non-root mcp user — runtime must never run as root.
USER mcp

# Build-time sanity check: the completed server imports and its module entrypoint
# is intact. Dummy env is supplied only to this check and never baked as ENV.
RUN OPENCODE_GATEWAY_URL=http://example.invalid OPENCODE_GATEWAY_API_KEY=dummy python -c \
    "import opencode_gateway_mcp.server; print(\"mcp server import ok\")"

# MCP servers speak stdio; no HTTP port is exposed. The container is read-only
# with respect to Gateway data — it only calls the published Gateway HTTP API.
CMD ["python", "-m", "opencode_gateway_mcp.server"]
