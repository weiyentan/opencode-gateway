"""Versioned canonical serialization and injectable ULID generation.

Canonical JSON is defined here as:

* a versioned envelope ``{"schema_version": 1, "data": {...}}``;
* object keys sorted lexicographically at every nesting level;
* no insignificant whitespace (``separators=(",", ":")``);
* all datetimes normalised to UTC and rendered in ISO 8601 with a ``Z``
  suffix;
* enums rendered as their string value.

ULID generation is injectable so fixtures and tests can produce
deterministic ``afk_run_id`` values from a fixed timestamp.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from afk_outcomes.models import AFKRun

CANONICAL_SCHEMA_VERSION = 1

# Crockford base32 alphabet (excludes I, L, O, U) — the ULID standard.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_ULID_TIMESTAMP_BITS = 48
_ULID_RANDOM_BITS = 80


def _encode_bits(value: int, length: int) -> str:
    """Encode ``value`` as ``length`` big-endian Crockford base32 characters."""
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def make_ulid(timestamp_ms: int, randomness: int) -> str:
    """Build a 26-character ULID from a 48-bit millisecond timestamp and 80 random bits."""
    if not 0 <= timestamp_ms < (1 << _ULID_TIMESTAMP_BITS):
        raise ValueError("ULID timestamp must fit in 48 bits")
    if not 0 <= randomness < (1 << _ULID_RANDOM_BITS):
        raise ValueError("ULID randomness must fit in 80 bits")
    return _encode_bits(timestamp_ms, 10) + _encode_bits(randomness, 16)


@runtime_checkable
class ULIDSource(Protocol):
    """A source of ULIDs; injectable for deterministic fixtures."""

    def next_ulid(self) -> str: ...


class MonotonicULID:
    """Default ULID source: millisecond timestamp + 80 bits of randomness.

    Guarantees monotonicity within a process: repeated calls in the same
    millisecond increment the random component rather than colliding.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], int] | None = None,
        randbytes: Callable[[int], bytes] | None = None,
    ) -> None:
        self._clock = clock if clock is not None else lambda: int(time.time() * 1000)
        self._randbytes = randbytes if randbytes is not None else os.urandom
        self._last_ms: int = -1
        self._last_random: int = 0

    def next_ulid(self) -> str:
        ms = self._clock()
        if ms > self._last_ms:
            self._last_random = int.from_bytes(self._randbytes(10), "big")
        else:
            ms = max(ms, self._last_ms)
            if ms == self._last_ms:
                self._last_random = (self._last_random + 1) & ((1 << _ULID_RANDOM_BITS) - 1)
                if self._last_random == 0:
                    ms += 1
        self._last_ms = ms
        return make_ulid(ms, self._last_random)


class SequenceULID:
    """Deterministic ULID source: a fixed timestamp with an incrementing random part."""

    def __init__(self, timestamp_ms: int, start: int = 0) -> None:
        self._timestamp_ms = timestamp_ms
        self._counter = start

    def next_ulid(self) -> str:
        ulid = make_ulid(self._timestamp_ms, self._counter)
        self._counter += 1
        return ulid


def _utc_iso(value: datetime) -> str:
    """Render a datetime as a canonical UTC ISO 8601 string with a ``Z`` suffix."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    else:
        value = value.astimezone(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    return value.isoformat().replace("+00:00", "Z")


def _canonicalize(value: object) -> object:
    """Recursively reduce a model graph to plain JSON-ready Python values."""
    if isinstance(value, BaseModel):
        return {key: _canonicalize(val) for key, val in value.model_dump().items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, dict):
        return {key: _canonicalize(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return value


def canonical_payload(model: BaseModel) -> dict[str, object]:
    """Return the versioned envelope dict for ``model`` (pre-serialization)."""
    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "data": _canonicalize(model),
    }


def dumps_canonical(model: BaseModel) -> str:
    """Serialize ``model`` to stable canonical sorted-key JSON (no whitespace)."""
    return json.dumps(canonical_payload(model), sort_keys=True, separators=(",", ":"))


def loads_canonical(serialized: str) -> AFKRun:
    """Reconstruct an :class:`AFKRun` from canonical JSON."""
    envelope = json.loads(serialized)
    return AFKRun.model_validate(envelope["data"])
