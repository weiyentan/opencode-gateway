"""Tests for the Aurora Glass telemetry dashboard frontend (Issue #214).

Validates:
- File existence and structure
- HTML5 validity and required sections
- CSS design tokens and theme values
- JavaScript API endpoint references and rendering functions
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"
STYLES_CSS = FRONTEND_DIR / "styles.css"
APP_JS = FRONTEND_DIR / "app.js"

# ---------------------------------------------------------------------------
# Required sections (Issue #214 acceptance criteria)
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = [
    "KPI row",
    "Model Mix",
    "Live Events",
    "Collector Distribution",
    "Collectors table",
    "Agents & LLMs In Use",
    "Recent Sessions",
]

# Section IDs expected in the HTML (kebab-case of section names)
SECTION_IDS = [
    "kpi-row",
    "model-mix",
    "live-events",
    "collector-distribution",
    "collectors-table",
    "agents-llms",
    "recent-sessions",
]

# ---------------------------------------------------------------------------
# Required design tokens (Issue #214 design spec)
# ---------------------------------------------------------------------------

REQUIRED_COLORS = [
    "#08111f",
    "#0d1326",
    "#140e24",
    "rgba(255, 255, 255, 0.08)",
    "#7c8cff",
    "#5ef2ff",
    "#ff75b5",
    "#ffd86f",
]

REQUIRED_CSS_FEATURES = [
    "backdrop-filter",
    "Inter",  # font family
    "22px",   # border-radius
    "999px",  # pill/badge border-radius
]

# ---------------------------------------------------------------------------
# Required API endpoint references in JS
# ---------------------------------------------------------------------------

REQUIRED_API_ENDPOINTS = [
    "/health",
    "/api/v1/usage/aggregates",
    "/api/v1/usage/records",
    "/api/v1/usage/sessions",
]

REQUIRED_API_PARAMS = [
    "group_by",
    "start_date",
    "end_date",
]

# ---------------------------------------------------------------------------
# File existence tests (RED → GREEN)
# ---------------------------------------------------------------------------


class TestFrontendFileExistence:
    """Verify all frontend files exist."""

    def test_index_html_exists(self):
        """Verify frontend/index.html exists."""
        assert INDEX_HTML.exists(), f"{INDEX_HTML} does not exist"
        assert INDEX_HTML.is_file(), f"{INDEX_HTML} is not a file"

    def test_styles_css_exists(self):
        """Verify frontend/styles.css exists."""
        assert STYLES_CSS.exists(), f"{STYLES_CSS} does not exist"
        assert STYLES_CSS.is_file(), f"{STYLES_CSS} is not a file"

    def test_app_js_exists(self):
        """Verify frontend/app.js exists."""
        assert APP_JS.exists(), f"{APP_JS} does not exist"
        assert APP_JS.is_file(), f"{APP_JS} is not a file"


# ---------------------------------------------------------------------------
# HTML structure tests
# ---------------------------------------------------------------------------


class TestHtmlStructure:
    """Verify the dashboard HTML meets all requirements."""

    @pytest.fixture(scope="class")
    def html_content(self) -> str:
        """Read and return the HTML file content."""
        assert INDEX_HTML.exists(), "index.html must exist before running structure tests"
        return INDEX_HTML.read_text(encoding="utf-8")

    def test_html5_doctype(self, html_content: str):
        """Verify HTML5 doctype declaration."""
        assert html_content.strip().startswith("<!DOCTYPE html>"), \
            "Missing HTML5 doctype declaration"

    def test_has_head_and_body(self, html_content: str):
        """Verify <head> and <body> tags exist."""
        assert "<head>" in html_content.lower(), "Missing <head> tag"
        assert "</head>" in html_content.lower(), "Missing </head> tag"
        assert "<body>" in html_content.lower(), "Missing <body> tag"
        assert "</body>" in html_content.lower(), "Missing </body> tag"

    def test_all_sections_present(self, html_content: str):
        """Verify all 7 required layout sections are present."""
        for section_id in SECTION_IDS:
            # Check for id="section-id" or id='section-id'
            pattern = rf'id=["\']{re.escape(section_id)}["\']'
            assert re.search(pattern, html_content), \
                f"Missing section with id '{section_id}'"

    def test_google_fonts_inter_link(self, html_content: str):
        """Verify Inter font is loaded from Google Fonts."""
        assert "fonts.googleapis.com" in html_content and "Inter" in html_content, \
            "Missing Google Fonts link for Inter font"
        assert '<link' in html_content and '.css' in html_content, \
            "Missing stylesheet link for Google Fonts"

    def test_stylesheet_reference(self, html_content: str):
        """Verify styles.css is linked."""
        assert 'styles.css' in html_content, \
            "Missing styles.css reference in HTML"

    def test_script_reference(self, html_content: str):
        """Verify app.js is loaded."""
        assert 'app.js' in html_content, \
            "Missing app.js script reference in HTML"


# ---------------------------------------------------------------------------
# CSS design token tests
# ---------------------------------------------------------------------------


class TestCssDesignTokens:
    """Verify the Aurora Glass theme design tokens are present."""

    @pytest.fixture(scope="class")
    def css_content(self) -> str:
        """Read and return the CSS file content."""
        assert STYLES_CSS.exists(), "styles.css must exist before running CSS tests"
        return STYLES_CSS.read_text(encoding="utf-8")

    def test_required_colors_present(self, css_content: str):
        """Verify all required design-token colors are in the CSS."""
        # Normalise whitespace in both the CSS content and the expected
        # colors so the test tolerates different formatting styles.
        normalised = re.sub(r'\s+', ' ', css_content)
        for color in REQUIRED_COLORS:
            expected = re.sub(r'\s+', ' ', color)
            assert expected in normalised, \
                f"Missing required color '{color}' in styles.css"

    def test_backdrop_filter_present(self, css_content: str):
        """Verify backdrop-filter blur is used for glass effect."""
        assert "backdrop-filter" in css_content, \
            "Missing backdrop-filter for glass effect"
        assert "blur" in css_content, \
            "Missing blur() in backdrop-filter"

    def test_font_family_inter(self, css_content: str):
        """Verify Inter font family is declared."""
        assert "Inter" in css_content, \
            "Missing Inter font family declaration"

    def test_border_radius_values(self, css_content: str):
        """Verify 22px panel radius and 999px pill radius."""
        assert "22px" in css_content, \
            "Missing 22px border-radius for panels"
        assert "999px" in css_content, \
            "Missing 999px border-radius for pills/badges"

    def test_media_query_responsive(self, css_content: str):
        """Verify responsive media query down to 760px."""
        assert "@media" in css_content, \
            "Missing @media query for responsive design"
        # Should have a breakpoint around 760px
        assert "760px" in css_content or "768px" in css_content, \
            "Missing responsive breakpoint (expected 760px or 768px)"


# ---------------------------------------------------------------------------
# JavaScript API endpoint tests
# ---------------------------------------------------------------------------


class TestJavaScriptEndpoints:
    """Verify the JavaScript references the correct API endpoints."""

    @pytest.fixture(scope="class")
    def js_content(self) -> str:
        """Read and return the JS file content."""
        assert APP_JS.exists(), "app.js must exist before running JS tests"
        return APP_JS.read_text(encoding="utf-8")

    def test_api_endpoints_referenced(self, js_content: str):
        """Verify all required API endpoints are referenced."""
        for endpoint in REQUIRED_API_ENDPOINTS:
            assert endpoint in js_content, \
                f"Missing API endpoint reference to '{endpoint}'"

    def test_group_by_param_referenced(self, js_content: str):
        """Verify group_by parameter is used for aggregates."""
        assert "group_by" in js_content, \
            "Missing group_by parameter in API calls"

    def test_date_params_referenced(self, js_content: str):
        """Verify start_date and end_date are used."""
        assert "start_date" in js_content, \
            "Missing start_date parameter in API calls"
        assert "end_date" in js_content, \
            "Missing end_date parameter in API calls"

    def test_fetch_calls_present(self, js_content: str):
        """Verify fetch() is used for API calls."""
        assert "fetch(" in js_content, \
            "Missing fetch() API calls in JavaScript"

    def test_auto_refresh_polling(self, js_content: str):
        """Verify 30-second auto-refresh polling is configured."""
        # Check for setInterval with 30000 (30 seconds) or references to
        # POLL_INTERVAL / refresh / auto-refresh patterns
        patterns = [
            r"30000",
            r"setInterval",
            r"POLL_INTERVAL",
            r"auto.refresh",
        ]
        found = any(re.search(p, js_content) for p in patterns)
        assert found, \
            "Missing 30-second auto-refresh polling (setInterval with 30000 or POLL_INTERVAL)"

    def test_envelope_unwrap(self, js_content: str):
        """Verify the API response envelope (.data) is handled."""
        # The backend wraps responses in {status: "ok", data: ...}
        # Frontend should reference .data somewhere
        patterns = [
            r"\.data\b",
            r"response\.data",
            r"result\.data",
        ]
        found = any(re.search(p, js_content) for p in patterns)
        assert found, \
            "Missing response envelope unwrapping (.data access)"

    def test_syntax_validity(self, js_content: str):
        """Verify JS has no obvious syntax errors (balanced braces)."""
        # Basic check: balanced { } count
        open_braces = js_content.count("{")
        close_braces = js_content.count("}")
        assert open_braces == close_braces, \
            f"Unbalanced braces: {open_braces} open vs {close_braces} close"
