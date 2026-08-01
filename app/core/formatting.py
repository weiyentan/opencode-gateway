"""Formatting helpers for human-readable display values.

Contains utilities that turn raw stored values (e.g. the JSON model
descriptor in ``opencode_session_contexts.session_model``) into the
display strings surfaced by the Gateway API.
"""

from __future__ import annotations

import json


def format_model_output(raw: str | None) -> str | None:
    """Format a raw Session Model value for display.

    Parses the JSON model descriptor stored in
    ``opencode_session_contexts.session_model``.  When the input is
    valid JSON with both ``providerID`` and ``id`` keys, returns
    ``{providerID} / {id}`` with surrounding whitespace stripped from
    both parts; when only one key is present, returns that single
    value.  Input that is not valid JSON is passed through unchanged,
    and ``None`` or empty input yields ``None``.
    """
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(parsed, dict):
        return raw
    provider_id = parsed.get("providerID")
    model_id = parsed.get("id")
    if provider_id is None and model_id is None:
        return raw
    if provider_id is None:
        return str(model_id).strip()
    if model_id is None:
        return str(provider_id).strip()
    return f"{str(provider_id).strip()} / {str(model_id).strip()}"
