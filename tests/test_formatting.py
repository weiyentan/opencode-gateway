"""Tests for app.core.formatting.format_model_output.

Covers the Session Model descriptor formatting contract: a JSON
descriptor with both ``providerID`` and ``id`` formats as
``{providerID} / {id}``, single-key descriptors return that value,
non-JSON input passes through, and null/empty input yields ``None``.
"""

from __future__ import annotations

from app.core.formatting import format_model_output


class TestFormatModelOutput:
    """Unit tests for the format_model_output helper."""

    def test_full_json_descriptor_formats_provider_and_model(self):
        """JSON with both providerID and id renders as 'providerID / id'."""
        raw = '{"providerID": "opencode-go", "id": "deepseek-v4-flash"}'
        assert format_model_output(raw) == "opencode-go / deepseek-v4-flash"

    def test_whitespace_stripped_from_both_parts(self):
        """Surrounding whitespace is stripped from providerID and id."""
        raw = '{"providerID": "  opencode-go  ", "id": "  deepseek-v4-flash  "}'
        assert format_model_output(raw) == "opencode-go / deepseek-v4-flash"

    def test_json_with_only_id(self):
        """JSON with only id returns that single value."""
        raw = '{"id": "deepseek-v4-flash"}'
        assert format_model_output(raw) == "deepseek-v4-flash"

    def test_json_with_only_provider_id(self):
        """JSON with only providerID returns that single value."""
        raw = '{"providerID": "opencode-go"}'
        assert format_model_output(raw) == "opencode-go"

    def test_plain_string_passthrough(self):
        """Non-JSON input passes through as a plain string."""
        assert format_model_output("claude-sonnet-4-20250514") == "claude-sonnet-4-20250514"

    def test_null_returns_none(self):
        """Null input yields null."""
        assert format_model_output(None) is None

    def test_empty_string_returns_none(self):
        """Empty string input yields null."""
        assert format_model_output("") is None

    def test_malformed_json_passthrough(self):
        """Malformed JSON passes through as a plain string."""
        raw = '{"providerID": "opencode-go", "id": "deepseek-v4-flash"'
        assert format_model_output(raw) == raw

