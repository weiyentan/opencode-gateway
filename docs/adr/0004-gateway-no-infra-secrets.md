# ADR 0004: Gateway Holds No Infrastructure Secrets

## Status

Accepted

## Context

The Gateway needs to perform infrastructure actions on the Runner VM (create workspaces, start/stop services, collect state). These actions require SSH access or equivalent credentials to the VM.

The credentials could be managed in two ways:
1. **Gateway holds secrets** — SSH keys or API tokens stored in Gateway config or secrets store
2. **Executor owns secrets** — AWX holds SSH keys, Gateway talks to AWX API only

## Decision

The Gateway must never hold infrastructure secrets. All infrastructure credentials are owned by the executor plugin (AWX by default). The Gateway communicates only with the executor's API, which has its own authentication and RBAC.

## Rationale

- Security isolation: a compromise of the Gateway does not expose SSH keys or VM access
- Simpler compliance: the Gateway has a smaller audit surface for infrastructure access
- Leverages existing AWX capabilities: credential management, vault encryption, audit trail, RBAC
- The Gateway remains relatively low-trust — it orchestrates work without owning the means to execute it directly
- This pattern extends to non-AWX executors: SSH executor can own its key file, Kubernetes executor uses in-cluster auth

## Consequences

Positive:
- Reduced blast radius if the Gateway is compromised
- Gateway code never needs to handle SSH key material
- Infrastructure credentials stay in the system designed to manage them (AWX, vault, etc.)

Negative:
- Gateway depends on executor API availability for all infrastructure operations
- More complex end-to-end debugging (Gateway → AWX API → AWX execution node → VM)
- Gateway cannot directly intervene on the VM if the executor is unreachable

## Alternatives Considered

**Gateway holds SSH keys directly**: Simpler architecture but creates a high-value target and increases compliance scope.

**Hybrid: Gateway holds a short-lived token minted by a vault system**: Adds complexity without clear benefit at MVP scale.
