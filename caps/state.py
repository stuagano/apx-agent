from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .fingerprint import fingerprint
from .freshness import parse_duration, parse_iso, waiver_active
from .ledger import LedgerEntry
from .manifest import Capability

BLOCK_STATES = {"never-proven", "fail", "error", "code-stale"}


def capability_state(
    capability: Capability,
    entry: LedgerEntry | None,
    root: str | Path,
    now: datetime,
) -> str:
    """One word for where a capability stands. Distinguishes code-stale
    (fingerprint changed -> the gate blocks) from time-expired (clock window
    passed -> the gate does NOT block, per the Phase 1 decision)."""
    if waiver_active(entry, now):
        return "waived"
    if entry is None:
        return "never-proven"
    if entry.result == "fail":
        return "fail"
    if entry.result == "error":
        return "error"
    if entry.result == "pass":
        if capability.freshness == "code":
            return "proven" if entry.fingerprint == fingerprint(capability, root) else "code-stale"
        window = parse_duration(capability.freshness)
        within = (now - parse_iso(entry.at)) < window
        return "proven" if within else "time-expired"
    # result == "waived" but expired (waiver_active was False), or unknown: no live proof.
    return "never-proven"
