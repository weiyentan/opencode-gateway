## Summary
This automated develop-loop run implemented the following issues:
| Issue | Title |
|-------|-------|
| #758 | Containerize and publish opencode-gateway-mcp through CI |
| #759 | Bootstrap opencode-gateway-mcp with Gateway health tool |
| #760 | Expose AFK activity summaries through MCP |
| #761 | Expose filtered AFK run discovery through MCP |
| #762 | Expose model and agent usage through MCP |
| #763 | Expose complete AFK run story through MCP |
| #764 | Expose PR and MR AFK history through MCP |
| #765 | Expose AFK correlation quality problems through MCP |
| #766 | Prove the complete opencode-gateway-mcp v1 contract |

## Changes
- Containerized MCP adapter with multi-stage Dockerfile (GHCR publish) — `opencode-gateway-mcp/Dockerfile`, `pyproject.toml`, `.github/workflows/mcp-publish.yml`
- Bootstrapped MCP server with `get_gateway_health` (health + version probe)
- Added `get_afk_activity_summary`, `list_afk_runs`, `get_model_usage`/`get_agent_usage`, `get_afk_run_story`, `get_change_request_story`, `get_correlation_issues` (8 tools total — full v1 surface in `opencode-gateway-mcp/src/opencode_gateway_mcp/server.py` + `client.py`)
- Added v1 contract gate with 61 tests (`test_v1_contract.py` + per-tool suites), updated workflow smoke to 8-tool handshake
- Updated `docs/mcp/README.md` with MCP v1 8-tool documentation

## Review
A consolidated diff review is available at .status/handoff/diff-review.md

Closes #758
Closes #759
Closes #760
Closes #761
Closes #762
Closes #763
Closes #764
Closes #765
Closes #766
