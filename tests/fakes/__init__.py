"""Fake client implementations for integration testing.

Provides deterministic, configurable fake versions of the AWX API client
and OpenCode Serve client.  These fakes implement the same interfaces as
their production counterparts but return pre-configured responses instead
of making real HTTP calls.

Usage in test fixtures::

    from tests.fakes import FakeAWXClient, FakeOpenCodeServeClient

    fake_awx = FakeAWXClient(mode="success")
    fake_opencode = FakeOpenCodeServeClient(mode="diff",
        diff_response=SessionDiffResponse(
            session_id="sess-1",
            diff="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
            files_changed=["file.py"],
        ),
    )
"""

from __future__ import annotations

from tests.fakes.fake_awx_client import FakeAWXClient
from tests.fakes.fake_opencode_client import FakeOpenCodeServeClient

__all__ = ["FakeAWXClient", "FakeOpenCodeServeClient"]
