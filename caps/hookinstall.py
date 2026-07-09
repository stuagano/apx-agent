from __future__ import annotations

import json
from pathlib import Path

from .backup import backup_file

HOOK_TAG = "caps-stop-gate"
PONYTAIL_TAG = "caps-ponytail"


def _entry(command: str, tag: str, matcher: str | None) -> dict:
    entry: dict = {"_caps": tag, "hooks": [{"type": "command", "command": command, "timeout": 10}]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def install_hook(
    settings_path: str | Path,
    command: str,
    *,
    event: str = "Stop",
    tag: str = HOOK_TAG,
    matcher: str | None = None,
) -> None:
    settings_path = Path(settings_path)
    data = json.loads(settings_path.read_text() or "{}") if settings_path.exists() else {}
    if settings_path.exists():
        backup_file(settings_path)
    entries = data.setdefault("hooks", {}).setdefault(event, [])
    entries[:] = [h for h in entries if h.get("_caps") != tag]  # idempotent
    entries.append(_entry(command, tag, matcher))
    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def uninstall_hook(settings_path: str | Path, *, event: str = "Stop", tag: str = HOOK_TAG) -> None:
    settings_path = Path(settings_path)
    data = json.loads(settings_path.read_text() or "{}") if settings_path.exists() else {}
    if settings_path.exists():
        backup_file(settings_path)
    entries = data.get("hooks", {}).get(event, [])
    data.setdefault("hooks", {})[event] = [h for h in entries if h.get("_caps") != tag]
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
