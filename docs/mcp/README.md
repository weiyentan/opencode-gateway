# opencode-gateway-mcp — Container & Publishing

This document covers the production container for `opencode-gateway-mcp` (issue #758,
ADR 0031). The MCP is a read-only semantic adapter that talks only to the published
OpenCode Gateway HTTP API — it never connects to Postgres, Kafka, AWX, or collector
databases.

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

## Required runtime environment variables

The container **fails closed** if either variable is missing. They are supplied at
runtime and are never baked into image layers or emitted in build logs.

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENCODE_GATEWAY_URL` | yes | Gateway base URL, e.g. `https://gateway.example.com` or `http://host.docker.internal:8000`. Must be `http(s)://`. |
| `OPENCODE_GATEWAY_API_KEY` | yes | Gateway API key (bearer token for `Authorization: Bearer …`). Never logged or returned through MCP tool results. |

No other configuration is required for the container contract slice. Future tool
slices may add optional flags, but `OPENCODE_GATEWAY_URL` and
`OPENCODE_GATEWAY_API_KEY` remain the only required runtime inputs.

## Local container startup

Build the image locally (no secrets needed at build time):

```bash
docker build -f opencode-gateway-mcp/Dockerfile -t opencode-gateway-mcp:local .
```

Run with externally supplied config (same shape CI smoke uses):

```bash
docker run --rm \
  -e OPENCODE_GATEWAY_URL=https://gateway.example.com \
  -e OPENCODE_GATEWAY_API_KEY="$OPENCODE_GATEWAY_API_KEY" \
  ghcr.io/weiyentan/opencode-gateway-mcp:master
# or locally built tag:
# docker run --rm -e OPENCODE_GATEWAY_URL=... -e OPENCODE_GATEWAY_API_KEY=... opencode-gateway-mcp:local
```

The container validates env on startup, prints `opencode-gateway-mcp ready` and
`ready` to stderr/stdout without logging the API key, and stays alive speaking MCP
over stdio. Missing or malformed env causes exit 1 with an error to stderr.

Smoke validation (CI equivalent) — must fail without env, pass with env:

```bash
# should fail closed (exit 1)
docker run --rm opencode-gateway-mcp:local; echo $?

# should start and stay alive
CID=$(docker run -d -e OPENCODE_GATEWAY_URL=http://example.invalid -e OPENCODE_GATEWAY_API_KEY=dummy opencode-gateway-mcp:local)
sleep 2; docker logs "$CID"; docker ps | grep "$CID" && echo "smoke passed"
docker rm -f "$CID"
```

## Architecture notes

- Multi-stage build (`python:3.12-slim` builder → runtime), lean venv, no build tools in runtime.
- Runs as non-root user `mcp` (`USER mcp`).
- No Postgres/Kafka/AWX/collector code or dependencies are included.
- Supports ADR 0031: read-only HTTP adapter, tool results are structured Gateway facts.
- Build logs never contain `OPENCODE_GATEWAY_API_KEY`; runtime logs never echo it.

## CI publishing flow

`mcp-publish.yml` runs `validate` (ruff + forbidden-import check) before `build-and-smoke`.
The image is built with `load: true` to a smoke tag, smoke-tested (fail-closed + success with env,
no secret in logs), and only then rebuilt and pushed to GHCR with the immutable+branch/release tags.
Pull-request builds are validated and smoke-tested but never pushed.

See `.github/workflows/mcp-publish.yml` for the exact tag matrix and smoke logic.
