"""Tests for the Aurora Glass frontend.

Verifies that the frontend files exist for the nginx container build.
The frontend is now served by a separate nginx container (not mounted
via FastAPI StaticFiles in the Gateway process).
"""

from pathlib import Path

import pytest


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


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
