"""Project setup diagnosis for caps.

`caps doctor` answers "is this project actually wired to enforce its claims?"
before you trust a green run — catching the silent setup gaps (an unparseable
manifest, a check file that doesn't exist, the Stop hook never installed) that
otherwise look fine until the gate quietly does nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .hookinstall import HOOK_TAG
from .ledger import load_ledger
from .manifest import ManifestError, load_manifest
from .project import LEDGER_REL, MANIFEST_NAME
from .state import BLOCK_STATES, capability_state

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Finding:
    level: str  # OK | WARN | FAIL
    message: str


def _hook_finding(settings_path: Path) -> Finding:
    """Is the Stop-hook gate registered, so enforcement runs in-band?"""
    if not settings_path.exists():
        return Finding(
            WARN,
            f"stop-hook: {settings_path} not found — gate not "
            f"enforced in-band (run: python -m caps install-hook)",
        )
    try:
        data = json.loads(settings_path.read_text() or "{}")
    except json.JSONDecodeError as e:
        return Finding(WARN, f"stop-hook: {settings_path} is not valid JSON ({e})")
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    if not isinstance(hooks, dict):
        return Finding(
            WARN, f"stop-hook: {settings_path} has an invalid 'hooks' shape (expected object)"
        )
    stops = hooks.get("Stop", [])
    if not isinstance(stops, list):
        return Finding(
            WARN, f"stop-hook: {settings_path} has an invalid 'hooks.Stop' shape (expected list)"
        )
    if any(isinstance(h, dict) and h.get("_caps") == HOOK_TAG for h in stops):
        return Finding(OK, f"stop-hook: installed in {settings_path}")
    return Finding(
        WARN,
        "stop-hook: not installed — the gate won't run in-band (run: python -m caps install-hook)",
    )


def diagnose(
    root: str | Path,
    now: datetime,
    settings_path: str | Path | None = None,
) -> list[Finding]:
    root = Path(root)
    findings: list[Finding] = []

    # 1. Manifest parses & validates — without this nothing else is meaningful.
    try:
        caps = load_manifest(root / MANIFEST_NAME)
    except ManifestError as e:
        return [Finding(FAIL, f"manifest: invalid — {e}")]
    n = len(caps)
    findings.append(
        Finding(
            OK if caps else WARN,
            f"manifest: {n} capabilit{'y' if n == 1 else 'ies'}, valid"
            + ("" if caps else " (none declared yet — add one with: python -m caps add ...)"),
        )
    )

    # 2. Every pytest check file actually exists (a missing one can never prove).
    missing = [
        (c.id, c.check_target.split("::", 1)[0])
        for c in caps
        if c.check_kind == "pytest" and not (root / c.check_target.split("::", 1)[0]).is_file()
    ]
    if missing:
        for cid, f in missing:
            findings.append(Finding(FAIL, f"check missing: {f} — capability {cid!r} can't run"))
    elif caps:
        findings.append(Finding(OK, "checks: all pytest check files present"))

    # 3. Capabilities with no declared deps (code-freshness only sees the check).
    no_deps = [c.id for c in caps if any("deps not declared" in w for w in c.warnings)]
    if no_deps:
        findings.append(
            Finding(
                WARN,
                f"deps: not declared for {', '.join(no_deps)} "
                f"(code-freshness covers only the check file)",
            )
        )

    # 4. Ledger + current proof state.
    ledger = load_ledger(root / LEDGER_REL)
    if not (root / LEDGER_REL).exists():
        if caps:
            findings.append(Finding(WARN, "ledger: none yet — run: python -m caps verify"))
    else:
        findings.append(Finding(OK, f"ledger: {LEDGER_REL} ({len(ledger)} entries)"))

    states: dict = {}
    for cap in caps:
        st = capability_state(cap, ledger.get(cap.id), root, now)
        states[st] = states.get(st, 0) + 1
    blocking = {k: v for k, v in states.items() if k in BLOCK_STATES}
    if blocking:
        parts = ", ".join(f"{v} {k}" for k, v in sorted(blocking.items()))
        findings.append(Finding(WARN, f"proof state: {parts} — run: python -m caps verify --stale"))
    elif caps:
        findings.append(Finding(OK, "proof state: all proven & fresh (or waived)"))

    # 5. Is the gate wired up to run in-band?
    sp = Path(settings_path) if settings_path else (Path.home() / ".claude" / "settings.json")
    findings.append(_hook_finding(sp))
    return findings


def exit_code(findings: list[Finding]) -> int:
    """Hard problems (FAIL) fail the command; warnings do not."""
    return 1 if any(f.level == FAIL for f in findings) else 0
