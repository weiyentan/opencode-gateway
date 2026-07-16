"""Loki/Grafana URL link generator.

Builds Grafana Explore URLs with Loki log stream selectors and time
range bounds for drill-down from the usage reporting API.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from urllib.parse import quote


def build_loki_search_url(
    client_id: uuid.UUID,
    source_database_id: uuid.UUID,
    session_id: uuid.UUID | None,
    start_time: datetime,
    end_time: datetime,
    *,
    grafana_base_url: str = "http://localhost:3000",
) -> str:
    """Build a Grafana Explore URL targeting Loki.

    The URL contains a log stream selector with the given identifiers
    and a time range scoped to *start_time* … *end_time*.

    Args:
        client_id: The OpenCode client UUID.
        source_database_id: The source-database UUID.
        session_id: Optional session UUID — included in the stream
            selector when provided.
        start_time: UTC start of the time range (inclusive).
        end_time: UTC end of the time range (inclusive).
        grafana_base_url: Base URL of the Grafana instance (defaults to
            ``http://localhost:3000``).

    Returns:
        A fully-qualified Grafana Explore URL.
    """
    # Build the log stream selector
    stream_selector_parts = [
        f'client_id="{client_id}"',
        f'source_database_id="{source_database_id}"',
    ]
    if session_id is not None:
        stream_selector_parts.append(f'session_id="{session_id}"')

    stream_selector = "{" + ",".join(stream_selector_parts) + "}"

    # Build the explore query object
    left_panel = {
        "datasource": "Loki",
        "queries": [
            {
                "expr": stream_selector,
            }
        ],
        "range": {
            "from": start_time.isoformat(),
            "to": end_time.isoformat(),
        },
    }

    left_json = json.dumps(left_panel, separators=(",", ":"))
    encoded_left = quote(left_json, safe="")

    return f"{grafana_base_url}/explore?orgId=1&left={encoded_left}"
