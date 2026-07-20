"""Tests for the Aurora Glass frontend and the same-origin local stack.

Verifies that:
- Frontend files exist for the nginx container build
- The nginx proxy configuration correctly routes API calls to the Gateway
- The docker-compose same-origin stack is properly configured
- The frontend nginx container is the sole browser entrypoint
"""

from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
REPO_DIR = Path(__file__).resolve().parent.parent


class TestFrontendFilesExist:
    """The frontend/ directory must contain all files needed by the nginx container."""

    def test_frontend_directory_exists(self):
        """The frontend/ directory must exist for the nginx build."""
        assert FRONTEND_DIR.is_dir(), "frontend/ directory missing"
        assert (FRONTEND_DIR / "index.html").exists(), "frontend/index.html missing"
        assert (FRONTEND_DIR / "style.css").exists(), "frontend/style.css missing"
        assert (FRONTEND_DIR / "app.js").exists(), "frontend/app.js missing"

    def test_nginx_config_exists(self):
        """The nginx.conf configuration file must exist."""
        assert (FRONTEND_DIR / "nginx.conf").exists(), "frontend/nginx.conf missing"

    def test_frontend_dockerfile_exists(self):
        """The frontend Dockerfile must exist for the nginx container build."""
        assert (FRONTEND_DIR / "Dockerfile").exists(), "frontend/Dockerfile missing"

    def test_frontend_js_tests_exist(self):
        """The JS pure-function tests must exist for frontend validation."""
        test_file = FRONTEND_DIR / "tests" / "test_pure_functions.js"
        assert test_file.exists(), "frontend/tests/test_pure_functions.js missing"

    def test_index_html_contains_dashboard_title(self):
        """The index.html page should contain the dashboard title."""
        content = (FRONTEND_DIR / "index.html").read_text()
        assert "Aurora Glass" in content

    def test_index_html_contains_correct_subtitle(self):
        """The subtitle should read 'OpenCode Gateway Observability'."""
        content = (FRONTEND_DIR / "index.html").read_text()
        assert "OpenCode Gateway Observability" in content


class TestNginxProxyConfiguration:
    """The frontend nginx.conf must proxy API paths to the Gateway backend.

    In the same-origin local stack, the frontend nginx container is the
    sole browser entrypoint.  API, health, and admin requests are proxied
    to the internal Gateway service at http://gateway:8000.
    """

    NGINX_CONF = FRONTEND_DIR / "nginx.conf"

    @pytest.fixture(autouse=True)
    def _load_nginx_config(self):
        self.config = self.NGINX_CONF.read_text()

    # ── Proxy location blocks ──────────────────────────────────────────

    def test_proxies_api_to_gateway(self):
        """/api/ requests must be proxied to http://gateway:8000."""
        assert self._has_proxy_pass("/api/", "gateway:8000"), (
            "nginx.conf must proxy /api/ to gateway:8000"
        )

    def test_proxies_health_to_gateway(self):
        """/health requests must be proxied to http://gateway:8000."""
        assert self._has_proxy_pass("/health", "gateway:8000"), (
            "nginx.conf must proxy /health to gateway:8000"
        )

    def test_proxies_admin_to_gateway(self):
        """/admin/ requests must be proxied to http://gateway:8000."""
        assert self._has_proxy_pass("/admin/", "gateway:8000"), (
            "nginx.conf must proxy /admin/ to gateway:8000"
        )

    def test_proxies_openapi_to_gateway(self):
        """/openapi.json requests must be proxied to http://gateway:8000."""
        assert self._has_proxy_pass("/openapi.json", "gateway:8000"), (
            "nginx.conf must proxy /openapi.json to gateway:8000"
        )

    def test_proxies_docs_to_gateway(self):
        """/docs requests must be proxied to http://gateway:8000."""
        assert self._has_proxy_pass("/docs", "gateway:8000"), (
            "nginx.conf must proxy /docs to gateway:8000"
        )

    # ── Static file serving ────────────────────────────────────────────

    def test_serves_static_files_at_root(self):
        """The root location must serve static files with index.html fallback."""
        assert "try_files $uri $uri/ /index.html;" in self.config, (
            "nginx.conf root location must have SPA fallback"
        )

    # ── Security headers ───────────────────────────────────────────────

    def test_has_security_headers(self):
        """nginx.conf must include security headers."""
        assert "X-Content-Type-Options" in self.config
        assert "X-Frame-Options" in self.config
        assert "X-XSS-Protection" in self.config

    # ── Helper ─────────────────────────────────────────────────────────

    def _has_proxy_pass(self, location: str, upstream: str) -> bool:
        """Check if a location block proxies to the given upstream."""
        import re
        # Match: location <location> { ... proxy_pass http://<upstream>; ... }
        pattern = re.compile(
            r"location\s+" + re.escape(location) +
            r"\s*\{(?:[^}]*?)proxy_pass\s+http://" + re.escape(upstream) + r"\s*;",
            re.DOTALL,
        )
        return bool(pattern.search(self.config))


class TestDockerComposeSameOriginStack:
    """The docker-compose.yaml must implement the same-origin local stack.

    Gateway must NOT expose host ports (only internal 'expose').  The
    frontend nginx container must be the sole entrypoint from the host.
    """

    COMPOSE_FILE = REPO_DIR / "docker-compose.yaml"

    @pytest.fixture(autouse=True)
    def _load_compose(self):
        import yaml
        with open(self.COMPOSE_FILE) as f:
            self.compose = yaml.safe_load(f)

    def test_gateway_has_no_host_ports(self):
        """The gateway service must NOT expose ports to the host."""
        gateway = self.compose["services"]["gateway"]
        assert "ports" not in gateway, (
            "Gateway must not expose ports to the host "
            "(it is only reachable internally via Docker DNS)"
        )

    def test_gateway_exposes_internal_port(self):
        """The gateway service should expose port 8000 internally."""
        gateway = self.compose["services"]["gateway"]
        expose = gateway.get("expose", [])
        assert "8000" in expose, (
            "Gateway must expose port 8000 for internal Docker DNS access"
        )

    def test_gateway_has_static_dir_disabled(self):
        """The gateway must have GATEWAY_STATIC_DIR set to a non-existent path."""
        env = self.compose["services"]["gateway"]["environment"]
        assert env.get("GATEWAY_STATIC_DIR") == "/nonexistent", (
            "Gateway must have GATEWAY_STATIC_DIR=/nonexistent "
            "to prevent accidental frontend serving"
        )

    def test_frontend_is_sole_entrypoint(self):
        """Only the frontend service should expose ports to the host."""
        frontend = self.compose["services"]["frontend"]
        assert "ports" in frontend, "Frontend must expose ports to the host"

        # Check no other service (except postgres for DB tooling) has ports
        for name, svc in self.compose["services"].items():
            if name in ("frontend", "postgres"):
                continue
            assert "ports" not in svc, (
                f"Service '{name}' must not expose ports to the host"
            )

    def test_frontend_depends_on_gateway(self):
        """The frontend service must depend on the gateway."""
        deps = self.compose["services"]["frontend"].get("depends_on", [])
        assert "gateway" in deps, "Frontend must depend_on gateway"

    def test_frontend_builds_from_frontend_dir(self):
        """The frontend service must build from ./frontend."""
        build = self.compose["services"]["frontend"].get("build")
        assert build in ("./frontend", {"context": "./frontend"}), (
            "Frontend service must build from the ./frontend directory"
        )
