from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class LedgerEntry:
    result: str  # "pass" | "fail" | "error" | "waived"
    at: str  # ISO-8601 timestamp
    tier: str  # "cheap" | "live"
    fingerprint: str | None = None
    waiver: dict | None = None  # {"reason": str, "until": isostr}
    detail: str | None = None  # trimmed check output for a fail/error
    files: dict | None = None  # {rel: hash} per-dep proof, for code freshness
    duration: float | None = None  # wall-clock seconds the check last took


def load_ledger(path: str | Path) -> dict[str, LedgerEntry]:
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text() or "{}")
    return {k: LedgerEntry(**v) for k, v in raw.items()}


def save_ledger(path: str | Path, entries: dict[str, LedgerEntry]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: asdict(v) for k, v in entries.items()}
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    # Atomic write (temp + rename in the same dir) so a concurrent verify can't
    # read a half-written ledger. Proven necessary: multiple verifies do overlap.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
