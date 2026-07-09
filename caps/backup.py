from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path


def backup_file(path: str | Path) -> Path:
    """Copy `path` to `path.bak.<YYYYMMDD>`, adding a -2, -3, ... suffix if a
    backup for today already exists. Returns the backup path."""
    path = Path(path)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    candidate = path.with_suffix(path.suffix + f".bak.{stamp}")
    n = 2
    while candidate.exists():
        candidate = path.with_suffix(path.suffix + f".bak.{stamp}-{n}")
        n += 1
    shutil.copy2(path, candidate)
    return candidate
