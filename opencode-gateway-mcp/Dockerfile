# ---- Builder Stage: install MCP runtime dependencies ----
# Lean multi-stage build: builder creates isolated venv with only Gateway-API
# dependencies (httpx + mcp SDK). No Postgres, Kafka, AWX, or collector libs.
FROM python:3.12-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir \
        "mcp>=1.0" \
        "httpx>=0.27,<1.0" \
        "pydantic>=2.0,<3.0" \
        "pydantic-settings>=2.0,<3.0"

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

# Create a minimal MCP entrypoint that validates runtime config and fails closed.
# This stub satisfies the container contract for issue #758; the real MCP server
# (issue #759) will replace the COPY below with the full source tree.
# No secrets are baked or logged — validation uses os.getenv at runtime only.
RUN mkdir -p /app/opencode_gateway_mcp && \
    printf '%s\n' \
    '"""opencode-gateway-mcp container stub — validates runtime config and stays alive for smoke."""' \
    'import os' \
    'import sys' \
    'import time' \
    '' \
    'def _require_env(name: str) -> str:' \
    '    value = os.getenv(name)' \
    '    if not value:' \
    '        print(f"error: {name} is required", file=sys.stderr)' \
    '        sys.exit(1)' \
    '    return value' \
    '' \
    'def main() -> None:' \
    '    url = _require_env("OPENCODE_GATEWAY_URL")' \
    '    _require_env("OPENCODE_GATEWAY_API_KEY")' \
    '    if not (url.startswith("http://") or url.startswith("https://")):' \
    '        print("error: OPENCODE_GATEWAY_URL must be http(s)://", file=sys.stderr)' \
    '        sys.exit(1)' \
    '    # Never log the API key — only the URL host is safe to surface.' \
    '    print(f"opencode-gateway-mcp ready — Gateway: {url}", file=sys.stderr, flush=True)' \
    '    print("ready", flush=True)' \
    '    # Keep the container alive for smoke-start validation (MCP stdio would block here).' \
    '    # Exit only on signal so `docker run --rm` smoke can verify startup then stop the container.' \
    '    try:' \
    '        while True:' \
    '            time.sleep(1)' \
    '    except KeyboardInterrupt:' \
    '        pass' \
    '' \
    'if __name__ == "__main__":' \
    '    main()' \
    > /app/opencode_gateway_mcp/server.py && \
    printf '%s\n' \
    '"""opencode_gateway_mcp package stub."""' \
    > /app/opencode_gateway_mcp/__init__.py

# Switch to the non-root mcp user — runtime must never run as root.
USER mcp

# Build-time sanity check: imports succeed and entrypoint is importable.
# Uses dummy env so the check does not require real credentials.
RUN OPENCODE_GATEWAY_URL=http://example.invalid OPENCODE_GATEWAY_API_KEY=dummy python -c "import opencode_gateway_mcp.server; print(\"mcp stub import ok\")"

# MCP servers speak stdio; no HTTP port is exposed. The container is read-only
# with respect to Gateway data — it only calls the published Gateway HTTP API.
CMD ["python", "-m", "opencode_gateway_mcp.server"]
