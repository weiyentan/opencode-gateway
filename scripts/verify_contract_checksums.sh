#!/usr/bin/env bash
#
# verify_contract_checksums.sh — deterministic parity check for the pinned
# producer-owned normalized-event v1 contract artifacts (issue #503).
#
# Recomputes SHA-256 digests of every pinned artifact (``schema.json`` plus the
# serializer-generated fixtures) under ``docs/contracts/normalized-event-v1/``
# and compares them against the pinned checksums file
# (``docs/contracts/normalized-event-v1/checksums.sha256``).
#
# Usage:
#   scripts/verify_contract_checksums.sh [CONTRACTS_DIR]             # check (default)
#   scripts/verify_contract_checksums.sh --write [CONTRACTS_DIR]     # (re)generate the checksums file
#
# Exit status:
#   0  — every pinned artifact matches the checksums file (no drift).
#   1  — drift detected: an artifact was edited, added, or removed, or the
#        checksums file is missing/stale.
#   2  — usage error (unknown option).
#
# Dependencies: coreutils (``sha256sum``, ``sort``) only — no network, no
# Python, and no access to the producer repository — so it runs unchanged in
# public GitHub CI where the private GitLab producer repository is not
# reachable.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WRITE_MODE=0
CONTRACTS_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --write)
            WRITE_MODE=1
            shift
            ;;
        --)
            shift
            CONTRACTS_DIR="${1:-}"
            break
            ;;
        -*)
            echo "unknown option: $1" >&2
            exit 2
            ;;
        *)
            CONTRACTS_DIR="$1"
            shift
            ;;
    esac
done

CONTRACTS_DIR="${CONTRACTS_DIR:-$REPO_ROOT/docs/contracts/normalized-event-v1}"
CHECKSUMS_FILE="$CONTRACTS_DIR/checksums.sha256"

[[ -d "$CONTRACTS_DIR" ]] || {
    echo "error: contracts directory not found: $CONTRACTS_DIR" >&2
    exit 1
}
[[ -f "$CONTRACTS_DIR/schema.json" ]] || {
    echo "error: schema.json missing under $CONTRACTS_DIR" >&2
    exit 1
}

# ── Recompute the sorted listing of every pinned artifact ────────────────────
# ``sha256sum <files>`` emits ``<hex>  <relative-path>`` per line; sorting makes
# the comparison deterministic and independent of filesystem enumeration order.
cd "$CONTRACTS_DIR"
fresh_listing="$(sha256sum --text schema.json fixtures/*.json | sort)"

if [[ "$WRITE_MODE" -eq 1 ]]; then
    printf '%s\n' "$fresh_listing" > checksums.sha256
    echo "wrote $(wc -l < checksums.sha256) digests to $CONTRACTS_DIR/checksums.sha256"
    exit 0
fi

[[ -f "$CHECKSUMS_FILE" ]] || {
    echo "error: checksums file missing: $CHECKSUMS_FILE" >&2
    echo "hint: run with --write after a contract refresh." >&2
    exit 1
}

pinned_listing="$(sort checksums.sha256)"

# ── Authoritative comparison (exact set + digests) ───────────────────────────
# A string comparison of the two sorted listings detects an edit (changed
# digest), an addition (extra line), a removal (missing line), or a reorder.
if [[ "$fresh_listing" == "$pinned_listing" ]]; then
    echo "OK: $(wc -l < checksums.sha256) contract artifacts match the pinned checksums."
    exit 0
fi

echo "DRIFT DETECTED: pinned contract artifacts differ from checksums.sha256." >&2
# Best-effort human-readable diff (diffutils); the authoritative verdict above
# does not depend on diff being present.
diff <(printf '%s\n' "$pinned_listing") <(printf '%s\n' "$fresh_listing") >&2 || true
exit 1
