# opencode-gateway-mcp — Container & Publishing

This document covers the production container for `opencode-gateway-mcp` (issues
#758–#766, ADR 0031) and its v1 read-only surface. The MCP is a read-only semantic
adapter that talks only to the published OpenCode Gateway HTTP API — it never connects
to Postgres, Kafka, AWX, or collector databases. Full tool semantics, time/pagination
rules, and non-goals live in [CONTEXT.md](CONTEXT.md).

## Available tools (v1)

The published container ships exactly the nine approved read-only tools. Each tool
calls only its documented Gateway GET endpoint via the authenticated HTTP client
(`OPENCODE_GATEWAY_URL` + `OPENCODE_GATEWAY_API_KEY`):

| Tool | Backed by |
|------|-----------|
| `get_gateway_health` | `GET /health` |
| `get_afk_activity_summary` | `GET /api/v1/afk/dashboard/summary` |
| `list_afk_runs` | `GET /api/v1/afk-outcomes/runs` |
| `get_model_usage` | `GET /api/v1/usage/aggregates?group_by=model` |
| `get_agent_usage` | `GET /api/v1/usage/aggregates?group_by=agent` |
| `get_afk_run_story` | `GET /api/v1/afk-outcomes/runs/{afk_run_id}` + `GET /api/v1/afk/executions/runs/{afk_run_id}` (only approved composite) |
| `list_change_requests` | `GET /api/v1/afk-outcomes/change-requests` |
| `get_change_request_story` | `GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}` |
| `get_correlation_issues` | `GET /api/v1/afk-outcomes/correlations` |

No write/admin, reconcile, ingest, provisioning, generic passthrough, silent crawling,
or direct Postgres/Kafka/AWX/collector access is exposed. See [CONTEXT.md](CONTEXT.md)
for input shapes, UTC/pagination/null-preservation rules, and parked gaps.

## Image name

Default registry is **GHCR** (`ghcr.io`) per issue #758.

```
ghcr.io/<owner>/opencode-gateway-mcp
```

For the `weiyentan/opencode-gateway` repository the image is:

```
ghcr.io/weiyentan/opencode-gateway-mcp
```

When the dedicated `opencode-gateway-mcp` repository is created, `docs/mcp/CONTEXT.md`
and this file should be copied there and the image name updated to the new repository
owner/name if it changes.

## Supported tags

The CI workflow `.github/workflows/mcp-publish.yml` publishes immutable and mutable
tags via `docker/metadata-action`:

| Tag | Example | Immutable | When |
|-----|---------|-----------|------|
| Short SHA | `abc1231` | yes | every push |
| Full SHA | `abc1231...` (40 hex) | yes | every push |
| Branch | `master`, `weiyentan-patch-1` | no | branch pushes |
| Semver | `1.2.3`, `1.2` | yes | `v1.2.3` tags |
| `latest` | `latest` | no | default branch `master` |

Immutable tags (`short SHA`, `full SHA`, semver) never move; branch/`latest` tags
move with new pushes. Consumers that need reproducibility should pin the short or
full SHA or a semver tag.

## Runtime environment variables

The container **fails closed** if either required Gateway variable is missing. Values
are supplied at runtime and are never baked into image layers or emitted in build logs.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENCODE_GATEWAY_URL` | yes | — | Gateway base URL, e.g. `https://gateway.example.com` or `http://host.docker.internal:8000`. Must be `http(s)://`. |
| `OPENCODE_GATEWAY_API_KEY` | yes | — | Gateway API key (bearer token for `Authorization: Bearer …`). Never logged or returned through MCP tool results. |
| `OPENCODE_MCP_TRANSPORT` | no | `stdio` | Use `stdio` for local/CLI clients or `streamable-http` for remote/tunnel deployments. |
| `OPENCODE_MCP_HOST` | no | `0.0.0.0` | Bind address when `OPENCODE_MCP_TRANSPORT=streamable-http`. |
| `OPENCODE_MCP_PORT` | no | `8000` | Listen port when `OPENCODE_MCP_TRANSPORT=streamable-http`. |

The Streamable HTTP endpoint is `/mcp`. It runs stateless with JSON responses, which
matches the OpenAI Secure MCP Tunnel deployment pattern while keeping stdio as the
backward-compatible default.

## Local container startup

Build the image locally (no secrets needed at build time):

```bash
docker build -f opencode-gateway-mcp/Dockerfile -t opencode-gateway-mcp:local .
```

Run with externally supplied config (same shape CI smoke uses; stdio needs stdin attached):

```bash
docker run --rm -i \
  -e OPENCODE_GATEWAY_URL=https://gateway.example.com \
  -e OPENCODE_GATEWAY_API_KEY="$OPENCODE_GATEWAY_API_KEY" \
  ghcr.io/weiyentan/opencode-gateway-mcp:master
# or locally built tag:
# docker run --rm -i -e OPENCODE_GATEWAY_URL=... -e OPENCODE_GATEWAY_API_KEY=... opencode-gateway-mcp:local
```

The container validates `OPENCODE_GATEWAY_URL` and `OPENCODE_GATEWAY_API_KEY`
on startup. With the default `stdio` transport, stdout remains the MCP protocol
channel. For Kubernetes/OpenAI Secure MCP Tunnel deployments, set
`OPENCODE_MCP_TRANSPORT=streamable-http`; the server then listens on
`0.0.0.0:8000/mcp` by default. Missing or malformed configuration exits non-zero
without logging the Gateway API key.

Example Streamable HTTP startup:

```bash
docker run --rm -p 8000:8000 \
  -e OPENCODE_GATEWAY_URL=https://gateway.example.com \
  -e OPENCODE_GATEWAY_API_KEY="$OPENCODE_GATEWAY_API_KEY" \
  -e OPENCODE_MCP_TRANSPORT=streamable-http \
  ghcr.io/weiyentan/opencode-gateway-mcp:master
```

Smoke validation (CI equivalent) — must fail without env, pass with env:

```bash
# should fail closed (exit 1)
docker run --rm opencode-gateway-mcp:local; echo $?

# should start and speak MCP over stdio (stdin attached; the piped initialize
# request gets a JSON-RPC response on stdout, then EOF ends the session)
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1.0.0"}}}' \
  | docker run --rm -i \
      -e OPENCODE_GATEWAY_URL=http://example.invalid \
      -e OPENCODE_GATEWAY_API_KEY=dummy \
      opencode-gateway-mcp:local
```

## Architecture notes

- Multi-stage build (`python:3.12-slim` builder → runtime), lean venv, no build tools in runtime.
- Runs as non-root user `mcp` (`USER mcp`).
- No Postgres/Kafka/AWX/collector code or dependencies are included.
- Supports ADR 0031 (as amended v1.1): read-only HTTP adapter with exactly the nine v1 tools above; tool results are structured Gateway facts passed through without reinterpretation or invented correlation.
- Build logs never contain `OPENCODE_GATEWAY_API_KEY`; runtime logs never echo it; tool results and errors never expose the key.

## CI publishing flow

`mcp-publish.yml` runs `validate` (ruff + forbidden-import check) before `build-and-smoke`.
The image is built with `load: true` to a smoke tag, smoke-tested (fail-closed + success with env,
MCP smoke proves the completed server exposes exactly the nine v1 tools, no secret in logs), and
only then rebuilt and pushed to GHCR with the immutable+branch/release tags. Pull-request builds
are validated and smoke-tested but never pushed.

See `.github/workflows/mcp-publish.yml` for the exact tag matrix and smoke logic.
