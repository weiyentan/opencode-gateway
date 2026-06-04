# OpenCode Gateway — API Discovery Handoff

## Status

The team ran `opencode serve` locally on port 4899 and found it's a **Vite/React SPA** — not a pure REST API. The web UI consumes an internal API client baked into the JS bundle. We reverse-engineered the following client methods from that bundle:

- `c.client.session.list({directory})` → `Session[]`
- `c.client.session.get({sessionID})` → `Session`
- `c.client.session.update({sessionID, directory, time})` → `void`
- `c.client.project.list()` → `Project[]`
- `c.client.project.update({projectID, directory, name})` → `void`
- `c.client.file.status({directory})` → `FileStatus[]`
- `c.client.worktree.list({directory})` → `Worktree[]`
- `c.client.worktree.create({directory})` → `Worktree`
- `c.client.worktree.remove({directory, worktreeRemoveInput})` → `Worktree`
- `c.client.worktree.reset({directory, worktreeResetInput})` → `Worktree`
- `c.client.instance.dispose({directory})` → `void`

The transport layer is unknown — the server URL defaults to `localhost:4096` in web mode or the page origin. We know method signatures but not the actual HTTP wiring (routes, verbs, bodies, WebSocket vs SSE vs JSON-RPC).

**Open question**: How should the Gateway discover / specify the OpenCode Serve API surface?

## The Decision Tree

Three approaches were considered:

- **(a) Reverse-engineer fully** — Inspect the `opencode` npm package or GitHub source to map every route.
  - *Cost*: High effort, and the internal API could change between versions.
  - *When to revisit*: If the Gateway needs deep session control (messages, diffs, tool calls).

- **(b) Minimal expected contract** — Define a narrow `OpenCodeClient` interface in the Gateway with just the methods the Gateway needs (e.g., `session.list`, `session.get`, `instance.dispose`). Fill in the actual HTTP transport later when the surface area is confirmed.
  - *Recommended*. The Gateway only needs a small subset — create/dispose workspaces, list/get session status.
  - *Risk*: May guess wrong on transport shape, requiring refactors.

- **(c) Proxy & discover** — Run `opencode serve` and proxy its API calls (MITM) to record real HTTP traffic.
  - *Useful for validation* once we have a candidate contract, but heavyweight as a first step.

**Recommendation**: Option (b) — minimal expected contract — **was proposed but not yet resolved**.

## Next Steps

1. **Define the minimal client contract**: Create a Python abstract base class (e.g., `OpenCodeClientProtocol`) in the Gateway with the methods the Gateway will actually call. Based on the job lifecycle, the Gateway likely needs: `session.list`, `session.get`, `instance.dispose`, and possibly `worktree.list` / `worktree.reset`. Everything else can be stubbed.

2. **Figure out the transport**: OpenCode Serve's API client uses an object with method signatures like `{directory}` as first arg. This could be:
   - HTTP REST (POST/GET to routes like `/api/session/list`)
   - JSON-RPC over HTTP
   - WebSocket or SSE for streaming endpoints
   The next session should quickly check the npm source or the network tab while running `opencode serve` to see actual requests.

3. **Validate with one real call**: Once the transport is known, implement one real method (e.g., `session.list`) against a local `opencode serve` instance to confirm the shape works end-to-end.

4. **Document**: Write the confirmed API surface into a doc (e.g., `docs/openode-serve-api.md`) once resolved.

## Key Files to Reference

- `CONTEXT.md` — Domain language glossary (Gateway, Executor Plugin, OpenCode Serve, Runner VM, Job, Workspace)
- `.status/handoff/handoff.md` — Broader planning handoff with all pending decisions
- `.status/handoff/api-discovery-handoff.md` — This file

## Suggested Skills

- **grill-with-docs** — Use this to formally resolve option (b) vs (a)/(c), then walk through the transport question and document the final decision in CONTEXT.md or a new ADR
