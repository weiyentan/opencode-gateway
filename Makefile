# ── OpenCode Gateway — Makefile ──────────────────────────────────────────
# Development and build helpers for the Gateway observability service and
# the Aurora Glass frontend container.
# ────────────────────────────────────────────────────────────────────────────

.PHONY: help build-frontend

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Aurora Glass Frontend ─────────────────────────────────────────────────

build-frontend: ## Build the Aurora Glass standalone frontend container image
	docker build -f frontend/Dockerfile frontend/ -t aurora-glass

.PHONY: build-frontend-push
build-frontend-push: build-frontend ## Build and tag for pushing (requires DOCKER_REGISTRY and DOCKER_FRONTEND_IMAGE_NAME env vars)
ifndef DOCKER_REGISTRY
	$(error DOCKER_REGISTRY is not set — set it to your Docker registry, e.g. docker.io/yourname)
endif
ifndef DOCKER_FRONTEND_IMAGE_NAME
	$(error DOCKER_FRONTEND_IMAGE_NAME is not set — set it to the image name, e.g. aurora-glass)
endif
	docker tag aurora-glass $(DOCKER_REGISTRY)/$(DOCKER_FRONTEND_IMAGE_NAME)
	@echo "Tagged as $(DOCKER_REGISTRY)/$(DOCKER_FRONTEND_IMAGE_NAME). Run 'docker push' to publish."
