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
        content = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        assert "Aurora Glass" in content

    def test_index_html_contains_correct_subtitle(self):
        """The subtitle should read 'OpenCode Gateway Observability'."""
        content = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        assert "OpenCode Gateway Observability" in content


class TestMergedDashboardView:
    """The dashboard renders ONE merged Sessions + Agent Runs table (issue #402).

    The separate Sessions panel/tab was merged into the Agent Runs table,
    which is driven by /api/v1/usage/agent-runs (a superset: session_title,
    model, currentStatus, token breakdown, total_estimated_cost_usd) with
    the /agent-runs/{id} detail overlay.  The frontend sessions fetch and
    SESSION_LIMIT are gone from the dashboard; the backend /sessions
    endpoint remains untouched (presentation-only merge).
    """

    def test_merged_table_exists_in_markup(self):
        """index.html carries the merged table id and tbody."""
        content = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        assert 'id="agent-runs-table"' in content, (
            "merged table must carry id=\"agent-runs-table\""
        )
        assert 'id="agent-runs-tbody"' in content, (
            "merged table must render into #agent-runs-tbody"
        )

    def test_sessions_panel_removed_from_markup(self):
        """The separate Sessions panel/tab/table is removed from the dashboard."""
        content = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        assert "sessions-table" not in content, (
            "#sessions-table must be removed (merged into #agent-runs-table)"
        )
        assert "sessions-tbody" not in content, (
            "#sessions-tbody must be removed"
        )
        assert 'data-tab="sessions"' not in content, (
            "the Sessions top-nav tab must be removed"
        )
        assert 'id="tab-sessions"' not in content, (
            "the #tab-sessions panel must be removed"
        )

    def test_app_js_drops_sessions_fetch_and_session_limit(self):
        """The dashboard no longer fetches /api/v1/usage/sessions."""
        content = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
        # The fetch is gone: no apiFetch call targets the sessions endpoint
        # (comments may still reference the URL to document the removal).
        assert "apiFetch('/api/v1/usage/sessions" not in content, (
            "the frontend sessions fetch must be dropped from the dashboard"
        )
        assert "/api/v1/usage/sessions?" not in content, (
            "the frontend sessions fetch URL must be dropped from the dashboard"
        )
        assert "SESSION_LIMIT" not in content, (
            "SESSION_LIMIT must be removed with the sessions fetch"
        )
        assert "/api/v1/usage/agent-runs" in content, (
            "the merged table stays driven by /api/v1/usage/agent-runs"
        )

    def test_app_js_merged_table_uses_shared_token_breakdown(self):
        """The merged table renders tokens via the shared compact formatter."""
        content = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
        assert "fmtTokenBreakdownCompact" in content, (
            "the shared compact Token Breakdown formatter must remain in use"
        )
        assert "fmtAgentRunTokens" in content, (
            "the agent-runs token cell delegates to the shared formatter"
        )

    def test_app_js_current_status_semantics(self):
        """The status badge uses currentStatus semantics (active-badge gone)."""
        content = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
        assert "currentStatus" in content, (
            "the merged table status column must use currentStatus semantics"
        )
        assert "statusBadgeClass" in content, (
            "statusBadgeClass must remain the badge resolver"
        )
        assert "badge-active" not in content and "data-active" not in content, (
            "the sessions active-badge heuristic must be removed"
        )

    def test_aggregate_kpi_row_shared_across_tabs(self):
        """The date-range bar + KPI row sit ABOVE the tab panels (issue #411).

        The aggregate totals (Active Tokens, Est. Cost, Sessions — read from
        the aggregates total row by renderKPIs) must render on the Agent Runs
        tab with the dashboard date range applied.  They were previously
        scoped inside #tab-overview, so the Agent Runs tab showed no
        aggregate data; the fix moves them above all tab panels.
        """
        content = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        tab_overview = content.index('id="tab-overview"')
        date_bar = content.index('id="date-range-bar"')
        kpi_row = content.index('id="kpi-row"')
        assert date_bar < tab_overview, (
            "the dashboard date-range bar must sit above the tab panels "
            "(shared across tabs, not scoped to #tab-overview)"
        )
        assert kpi_row < tab_overview, (
            "the KPI row (aggregate totals) must sit above the tab panels "
            "so the Agent Runs tab renders tokens/sessions/cost"
        )



class TestNginxProxyConfiguration:
    """The frontend nginx.conf must proxy API paths to the Gateway backend.

    In the same-origin local stack, the frontend nginx container is the
    sole browser entrypoint. API, health, and admin requests are proxied
    to the internal Gateway service.
    """

    NGINX_CONF = FRONTEND_DIR / "nginx.conf"

    @pytest.fixture(autouse=True)
    def _load_nginx_config(self):
        self.config = self.NGINX_CONF.read_text(encoding="utf-8")

    # ── Proxy location blocks ──────────────────────────────────────────

    def test_proxies_api_to_gateway(self):
        """/api/ requests must be proxied through the configured upstream."""
        assert self._has_proxy_pass("/api/", "http://gateway:8000"), (
            "nginx.conf must proxy /api/ through GATEWAY_UPSTREAM"
        )

    def test_proxies_health_to_gateway(self):
        """/health requests must be proxied through the configured upstream."""
        assert self._has_proxy_pass("/health", "http://gateway:8000"), (
            "nginx.conf must proxy /health through GATEWAY_UPSTREAM"
        )

    def test_proxies_admin_to_gateway(self):
        """/admin/ requests must be proxied through the configured upstream."""
        assert self._has_proxy_pass("/admin/", "http://gateway:8000"), (
            "nginx.conf must proxy /admin/ through GATEWAY_UPSTREAM"
        )

    def test_proxies_openapi_to_gateway(self):
        """/openapi.json requests must be proxied through the configured upstream."""
        assert self._has_proxy_pass("/openapi.json", "http://gateway:8000"), (
            "nginx.conf must proxy /openapi.json through GATEWAY_UPSTREAM"
        )

    def test_proxies_docs_to_gateway(self):
        """/docs requests must be proxied through the configured upstream."""
        assert self._has_proxy_pass("/docs", "http://gateway:8000"), (
            "nginx.conf must proxy /docs through GATEWAY_UPSTREAM"
        )

    # ── IPv4 upstream policy (issue #378) ─────────────────────────────
    # The Gateway uvicorn listens on IPv4 127.0.0.1:8000. A proxy_pass
    # targeting `localhost` can resolve to IPv6 [::1]:8000 and fail with
    # Connection Refused — the hot-patch regression this suite locks in.

    def test_no_localhost_in_nginx_config(self):
        """nginx.conf must not reference localhost anywhere.

        `localhost` may resolve to [::1] while uvicorn listens only on
        127.0.0.1 — proxying to it causes Connection Refused errors.
        """
        assert "localhost" not in self.config, (
            "nginx.conf must not reference localhost — it can resolve to "
            "[::1]:8000 while the Gateway listens on 127.0.0.1:8000 "
            "(Connection Refused)"
        )

    def test_all_proxy_pass_directives_use_upstream_template(self):
        """Every proxy_pass must use the ${GATEWAY_UPSTREAM} env-var template.

        No proxy_pass may hard-code a hostname: the upstream host is set at
        container start by docker-entrypoint.sh from an IPv4-safe default.
        """
        import re
        proxy_passes = re.findall(r"proxy_pass\s+([^;]+);", self.config)
        assert proxy_passes, "nginx.conf must contain proxy_pass directives"
        for directive in proxy_passes:
            assert "${GATEWAY_UPSTREAM}" in directive, (
                "proxy_pass must use ${GATEWAY_UPSTREAM} template variable, "
                f"got {directive.strip()!r}"
            )
            assert "localhost" not in directive, (
                f"proxy_pass must not reference localhost, got {directive.strip()!r}"
            )

    def test_substituted_config_upstream_is_ipv4_safe(self):
        """Substituting the entrypoint default upstream must yield IPv4 targets.

        Simulates the envsubst step from docker-entrypoint.sh so the baked
        template and the runtime default are verified together: the rendered
        config must contain no localhost upstream.
        """
        import re
        entrypoint = (FRONTEND_DIR / "docker-entrypoint.sh").read_text(encoding="utf-8")
        default_match = re.search(r'DEFAULT_UPSTREAM="([^"]+)"', entrypoint)
        assert default_match, (
            "docker-entrypoint.sh must define DEFAULT_UPSTREAM"
        )
        default = default_match.group(1)
        assert "localhost" not in default, (
            f"DEFAULT_UPSTREAM must be IPv4-safe, got {default!r}"
        )
        assert default.startswith("http://"), (
            f"DEFAULT_UPSTREAM must be an http:// URL, got {default!r}"
        )

        substituted = self.config.replace("${GATEWAY_UPSTREAM}", default)
        for directive in re.findall(r"proxy_pass\s+([^;]+);", substituted):
            assert "localhost" not in directive, (
                "Rendered proxy_pass must not reference localhost, got "
                f"{directive.strip()!r}"
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
        """Check if a location block proxies to the given upstream.

        Accepts both the literal upstream URL (e.g. http://gateway:8000)
        and the ``${GATEWAY_UPSTREAM}`` env-var form so the same tests
        pass before and after envsubst substitution.
        """
        import re
        pattern = re.compile(
            r"location\s+" + re.escape(location) +
            r"\s*\{(?:[^}]*?)proxy_pass\s+(?:\$\{GATEWAY_UPSTREAM\}|"
            + re.escape(upstream) + r")\s*;",
            re.DOTALL,
        )
        return bool(pattern.search(self.config))


class TestFrontendEntrypointIPv4Upstream:
    """docker-entrypoint.sh must default GATEWAY_UPSTREAM to an IPv4-safe host.

    The entrypoint substitutes GATEWAY_UPSTREAM into the nginx template at
    container start. Its default is the last line of defense against a
    localhost upstream (issue #378 Connection Refused regression).
    """

    ENTRYPOINT = FRONTEND_DIR / "docker-entrypoint.sh"

    @pytest.fixture(autouse=True)
    def _load_entrypoint(self):
        self.content = self.ENTRYPOINT.read_text(encoding="utf-8")

    def test_entrypoint_default_upstream_is_ipv4_safe(self):
        """DEFAULT_UPSTREAM must resolve to IPv4 (gateway DNS or 127.0.0.1)."""
        assert 'DEFAULT_UPSTREAM="http://gateway:8000"' in self.content or \
            'DEFAULT_UPSTREAM="http://127.0.0.1:8000"' in self.content, (
            "DEFAULT_UPSTREAM must resolve to an IPv4 host "
            "(http://gateway:8000 or http://127.0.0.1:8000)"
        )

    def test_entrypoint_has_no_localhost_reference(self):
        """The entrypoint must not reference localhost as an upstream."""
        assert "localhost" not in self.content, (
            "docker-entrypoint.sh must not default the upstream to localhost"
        )

    def test_entrypoint_substitutes_gateway_upstream(self):
        """The entrypoint must envsubst GATEWAY_UPSTREAM into the nginx config."""
        assert "envsubst" in self.content, (
            "docker-entrypoint.sh must run envsubst to render nginx.conf"
        )
        assert "GATEWAY_UPSTREAM" in self.content, (
            "docker-entrypoint.sh must substitute GATEWAY_UPSTREAM"
        )


class TestFrontendDockerfileBakesNginxConfig:
    """The nginx config must be baked into the image, not runtime-patched.

    The issue #378 hot-fix (kubectl exec + sed) was lost on pod restart.
    The fix is permanent only because the Dockerfile copies the IPv4-safe
    template and the envsubst entrypoint into the image at build time.
    """

    DOCKERFILE = FRONTEND_DIR / "Dockerfile"

    @pytest.fixture(autouse=True)
    def _load_dockerfile(self):
        self.content = self.DOCKERFILE.read_text(encoding="utf-8")

    def test_dockerfile_bakes_nginx_config_template(self):
        """The Dockerfile must COPY frontend/nginx.conf as the nginx config."""
        assert "./frontend/nginx.conf" in self.content, (
            "frontend/Dockerfile must COPY ./frontend/nginx.conf into the image"
        )
        assert "/etc/nginx/conf.d/default.conf" in self.content, (
            "frontend/Dockerfile must install nginx.conf as the default vhost"
        )

    def test_dockerfile_installs_envsubst_entrypoint(self):
        """The Dockerfile must install the entrypoint that renders the config."""
        assert "./frontend/docker-entrypoint.sh" in self.content, (
            "frontend/Dockerfile must COPY ./frontend/docker-entrypoint.sh"
        )
        assert 'ENTRYPOINT ["/docker-entrypoint.sh"]' in self.content, (
            "frontend/Dockerfile must set the envsubst entrypoint"
        )

    def test_dockerfile_localhost_is_healthcheck_self_check_not_upstream(self):
        """The only localhost reference must be the HEALTHCHECK self-check.

        ``localhost`` in the frontend Dockerfile is the frontend's OWN nginx:
        the HEALTHCHECK runs ``curl -f http://localhost/`` inside the
        container, hitting the frontend nginx on port 80. It is NOT a gateway
        upstream, so it does not violate the no-localhost-upstream policy of
        issue #378 (the IPv6 [::1] Connection Refused regression). Any
        localhost reference outside the HEALTHCHECK command — or a HEALTHCHECK
        that targets the gateway upstream on port 8000 — must fail this test.
        """
        lines = self.content.splitlines()

        healthcheck_lines = [
            line for line in lines if line.lstrip().startswith("HEALTHCHECK")
        ]
        assert healthcheck_lines, "frontend/Dockerfile must define a HEALTHCHECK"

        localhost_lines = [line for line in lines if "localhost" in line]
        assert localhost_lines, (
            "frontend/Dockerfile must reference localhost in its HEALTHCHECK"
        )
        for line in localhost_lines:
            assert "curl -f http://localhost/" in line, (
                "the only localhost reference must be the HEALTHCHECK "
                f"self-check command, got: {line!r}"
            )
            assert "8000" not in line, (
                "HEALTHCHECK must self-check the frontend's own nginx, not "
                f"the gateway upstream on port 8000, got: {line!r}"
            )
            assert "proxy_pass" not in line and "GATEWAY_UPSTREAM" not in line, (
                "HEALTHCHECK must not target a gateway upstream, "
                f"got: {line!r}"
            )


class TestDockerComposeSameOriginStack:
    """The docker-compose.yaml must implement the same-origin local stack.

    Gateway must NOT expose host ports (only internal 'expose').  The
    frontend nginx container must be the sole entrypoint from the host.
    """

    COMPOSE_FILE = REPO_DIR / "docker-compose.yaml"

    @pytest.fixture(autouse=True)
    def _load_compose(self):
        import yaml
        with open(self.COMPOSE_FILE, encoding="utf-8") as f:
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
        """The compose stack must set GATEWAY_STATIC_DIR to /nonexistent."""
        env = self.compose["services"]["gateway"]["environment"]
        assert env.get("GATEWAY_STATIC_DIR") == "/nonexistent", (
            "Gateway compose config must set GATEWAY_STATIC_DIR=/nonexistent"
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
        """The frontend service must build from the frontend/ directory."""
        build = self.compose["services"]["frontend"].get("build", {})
        dockerfile = build.get("dockerfile", "")
        assert "frontend" in dockerfile, (
            f"Frontend build must reference frontend/ in dockerfile, got {dockerfile!r}"
        )

    def test_frontend_configures_gateway_upstream(self):
        """The frontend service must pass the runtime proxy target."""
        env = self.compose["services"]["frontend"].get("environment", {})
        assert env.get("GATEWAY_UPSTREAM") == "http://gateway:8000"

    def test_frontend_upstream_never_localhost(self):
        """The compose GATEWAY_UPSTREAM must never reference localhost.

        `localhost` can resolve to IPv6 [::1] while the Gateway listens on
        IPv4 127.0.0.1 — proxying to it causes Connection Refused on
        pod restart (issue #378).
        """
        env = self.compose["services"]["frontend"].get("environment", {})
        upstream = env.get("GATEWAY_UPSTREAM", "")
        assert upstream, "frontend service must set GATEWAY_UPSTREAM"
        assert "localhost" not in upstream, (
            f"GATEWAY_UPSTREAM must resolve to IPv4, got {upstream!r}"
        )
        assert upstream.startswith("http://"), (
            f"GATEWAY_UPSTREAM must be an http:// URL, got {upstream!r}"
        )


