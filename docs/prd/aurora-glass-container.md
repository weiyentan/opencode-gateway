## Problem Statement

Aurora Glass currently lives inside the OpenCode Gateway repository as a browser-based telemetry dashboard, but its delivery model is still coupled to the Gateway service. The current pipeline builds and publishes one Gateway image, while Aurora Glass is treated as static files served by the Gateway process. This blurs the boundary between the Gateway as an API service and Aurora Glass as a frontend, makes the deployable units unclear, and prevents the pipeline from publishing Aurora Glass as its own container artifact.

The team wants Aurora Glass to be built as part of the pipeline as a separate frontend container, while keeping the Gateway focused on serving the API.

## Solution

Publish Aurora Glass and the Gateway as two separate container images from the same repository and the same CI workflow. Keep the Gateway API-only. Package Aurora Glass as its own frontend container, serve it behind the same public origin as the Gateway, and preserve relative API paths so the browser continues to talk to the Gateway without introducing CORS as part of this change.

Update local development wiring so it mirrors the intended production shape: separate frontend and API containers behind one local entrypoint. The repository's validation boundary should prove that both images build, the local same-origin stack starts, and Aurora Glass can reach the Gateway API through that entrypoint.

## User Stories

1. As a platform engineer, I want Aurora Glass published as its own container image, so that I can deploy the frontend separately from the Gateway API.
2. As a platform engineer, I want the Gateway published as its own API image, so that the service boundary is explicit in deployment.
3. As a maintainer, I want both images built from the same repository revision, so that I can reason about frontend and API compatibility.
4. As a maintainer, I want both images to share the same version tags, so that releases remain easy to coordinate.
5. As a maintainer, I want one CI workflow to build and publish both images, so that delivery remains centralized in this repository.
6. As a maintainer, I want Aurora Glass to remain a static frontend artifact, so that this delivery split does not also force a frontend toolchain migration.
7. As a maintainer, I want the Gateway to stop serving Aurora Glass directly, so that the Gateway remains focused on API responsibilities.
8. As a developer, I want local development to run the frontend and API as separate containers, so that local behavior matches the intended deployment shape.
9. As a developer, I want local browser access to use one origin, so that Aurora Glass can keep using relative API paths.
10. As a developer, I want the local stack to route frontend and API traffic through one entrypoint, so that I do not need special browser configuration or CORS exceptions.
11. As a browser client, I want Aurora Glass to keep calling relative API paths, so that the frontend does not need environment-specific API host configuration for this slice.
12. As an operator, I want Aurora Glass and the Gateway to share one public origin in deployment, so that browser access behaves consistently across environments.
13. As an operator, I want this repository to validate the local same-origin behavior, so that delivery regressions are caught before deployment.
14. As a maintainer, I want the CI workflow to verify the frontend can reach the Gateway API through the local entrypoint, so that the split is tested as an end-to-end contract.
15. As a maintainer, I want the Aurora Glass container to avoid holding infrastructure or database secrets, so that the frontend remains low-trust and only talks to the Gateway API.
16. As a maintainer, I want the Gateway container to keep owning API concerns only, so that the system boundary stays clear for future work.
17. As a future contributor, I want the architectural split between Aurora Glass and the Gateway captured in project documentation, so that the reasoning does not have to be rediscovered.
18. As a future contributor, I want the repository to keep local development runnable after the split, so that the delivery architecture does not become harder to work on.
19. As a release manager, I want shared image tags for frontend and API artifacts, so that deployment rollouts can pin a single repository version.
20. As a release manager, I want the repo pipeline to stop implying that the Gateway image also owns frontend delivery, so that published artifacts match the intended architecture.

## Implementation Decisions

- Aurora Glass is a separate frontend deployable, not a Gateway service layer.
- The Gateway remains an API service and no longer owns frontend serving as part of the target architecture.
- The repository publishes two container artifacts from one CI workflow: one for the Gateway and one for Aurora Glass.
- Both artifacts share the same version tags because they are built from the same repository revision.
- Aurora Glass remains a static frontend for this slice. The split does not introduce a new frontend compile toolchain unless a later change explicitly chooses to do so.
- Aurora Glass is served from a small static web server container. The exact server implementation is not a product requirement as long as it cleanly serves the SPA.
- Aurora Glass continues to use relative API paths. Routing to the Gateway API is owned by reverse proxy or ingress, not by frontend runtime configuration.
- The intended deployment shape is same-origin: Aurora Glass and the Gateway are separate containers that share one public origin.
- Local development should mirror the same-origin production shape instead of relying on separate ports or CORS-specific behavior.
- The local stack includes a frontend-facing routing layer that sends browser API requests to the Gateway and frontend asset requests to Aurora Glass.
- The Gateway's temporary static-serving configuration is removed or retired as part of reaching the API-only target state.
- This repository owns build and local wiring for the split, but it does not assume ownership of external deployment manifests that may live elsewhere.
- Aurora Glass must not gain direct access to Postgres, collector credentials, or infrastructure secrets. It remains a browser-facing consumer of the Gateway API.
- The resulting architecture should preserve the existing domain vocabulary: Gateway for the API service, Aurora Glass for the browser dashboard, and same-origin delivery for the browser contract.

## Testing Decisions

- Good tests should validate external behavior and delivery contracts, not internal implementation details of the chosen web server or proxy.
- The repository should test the artifact boundary by proving both images build successfully from CI.
- The repository should test the runtime boundary by proving the local same-origin stack starts successfully.
- The repository should test the browser-to-API contract by proving Aurora Glass can fetch Gateway API responses through the local entrypoint using the intended routing model.
- The repository should test that the Gateway remains reachable as an API service after static serving is removed.
- The repository should not treat external cluster ingress or deployment-manifest validation as part of this PRD's required test boundary unless those deployment assets are moved into this repository.
- Tests should focus on observable outcomes such as image build success, HTTP reachability, and expected routing behavior.
- Prior art should come from the repository's existing application-factory tests and container-based local stack patterns, extended into a smoke-test shape for the separate frontend and same-origin routing contract.

## Out of Scope

- Rewriting Aurora Glass into a new frontend framework or adding a full Node-based build toolchain.
- Changing the Gateway's API surface, response envelope, or observability domain behavior.
- Introducing separate-origin browser access with CORS as part of this slice.
- Owning or changing external deployment manifests that are not stored in this repository.
- Creating implementation issues or execution plans beyond this destination document.
- Adding new product features to Aurora Glass unrelated to the delivery split.

## Further Notes

- This PRD follows the domain decision that Aurora Glass is separate from the Gateway service and the ADR that records the frontend/container split.
- The most important constraint is to keep the browser contract simple: same-origin delivery, relative API paths, and no new secret-bearing responsibilities for Aurora Glass.
- If the team later wants independent frontend release cadence, a frontend toolchain, or separate-origin hosting, those should be handled as follow-up decisions rather than folded into this slice.
