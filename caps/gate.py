from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .fingerprint import changed_deps
from .ledger import load_ledger
from .manifest import load_manifest
from .project import LEDGER_REL, MANIFEST_NAME, find_root
from .state import BLOCK_STATES, capability_state


@dataclass
class GateDecision:
    block: bool
    reason: str | None = None  # shown to Claude when block is True
    note: str | None = None  # non-blocking additionalContext


def resolve_root(payload: dict) -> Path | None:
    cwd = payload.get("cwd")
    if cwd:
        r = find_root(Path(cwd))
        if r:
            return r
    tp = payload.get("transcript_path")
    if tp:
        r = find_root(Path(tp).parent)
        if r:
            return r
    return None


# How many lines of a recorded failure to echo into the block message. The full
# snippet lives in the ledger; the gate shows just enough to fix without re-running.
DETAIL_LINES = 20
# Cap how many changed deps to name inline before summarizing the rest.
CHANGED_DEPS_SHOWN = 8


def _changed_dep_line(cap, entry, root) -> list:
    """Name the dep(s) that drifted since the last proof, for a code-stale cap."""
    changed = changed_deps(cap, getattr(entry, "files", None), root)
    if not changed:
        return []
    shown = ", ".join(changed[:CHANGED_DEPS_SHOWN])
    if len(changed) > CHANGED_DEPS_SHOWN:
        shown += f" (+{len(changed) - CHANGED_DEPS_SHOWN} more)"
    return [f"      changed since last proof: {shown}"]


def _detail_lines(entry) -> list:
    """The tail of a recorded fail/error detail, indented for the block message."""
    if entry is None or not getattr(entry, "detail", None):
        return []
    tail = entry.detail.splitlines()[-DETAIL_LINES:]
    out = ["    last failure:"]
    out.extend(f"      {ln}" for ln in tail)
    return out


def decide(payload: dict, now: datetime) -> GateDecision:
    if payload.get("stop_hook_active"):
        return GateDecision(block=False)

    root = resolve_root(payload)
    if root is None:
        return GateDecision(block=False)

    caps = load_manifest(root / MANIFEST_NAME)
    ledger = load_ledger(root / LEDGER_REL)

    blocking: list[tuple] = []  # (cap, state, entry)
    expired: list = []  # cap
    for cap in caps:
        entry = ledger.get(cap.id)
        state = capability_state(cap, entry, root, now)
        if state in BLOCK_STATES:
            blocking.append((cap, state, entry))
        elif state == "time-expired":
            expired.append(cap)

    note = None
    if expired:
        ids = ", ".join(c.id for c in expired)
        note = f"live capability time-expired (re-verify when convenient): {ids}"

    if not blocking:
        return GateDecision(block=False, note=note)

    lines = ["✗ Capabilities not proven & fresh — resolve before finishing:"]
    for cap, state, entry in blocking:
        lines.append(f"  • {cap.id} [{state}]: {cap.then}")
        if state == "code-stale":
            lines.extend(_changed_dep_line(cap, entry, root))
        lines.extend(_detail_lines(entry))
    if note:
        lines.append(f"  (note) {note}")
    lines.append("Re-prove all of the above:  python -m caps verify --stale")
    lines.append(
        "Full status: python -m caps status   ·   "
        'can\'t prove now? python -m caps ack <id> --reason "..."'
    )
    return GateDecision(block=True, reason="\n".join(lines), note=note)
