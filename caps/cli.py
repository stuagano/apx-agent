from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .doctor import FAIL, OK, WARN, diagnose, exit_code
from .fingerprint import FILE_MAP_LIMIT, changed_deps, file_fingerprints, fingerprint
from .freshness import FreshnessError, parse_duration, waiver_active
from .gate import decide
from .hookinstall import PONYTAIL_TAG, install_hook, uninstall_hook
from .initializer import init_project, kit_root
from .ledger import LedgerEntry, load_ledger, save_ledger
from .manifest import ManifestError, load_manifest
from .manifest_edit import ManifestEditError, add_capability
from .ponytail import ponytail_instructions
from .project import LEDGER_REL, MANIFEST_NAME, find_root
from .review import review_rubric
from .runner import run_capability
from .state import BLOCK_STATES, capability_state

_DISPLAY = {
    "proven": "proven",
    "never-proven": "never proven",
    "fail": "fail",
    "error": "error",
    "code-stale": "stale",
    "time-expired": "expired",
    "waived": "waived",
}
_GLYPH = {
    "proven": "OK ",
    "never proven": "----",
    "fail": "FAIL",
    "error": "ERR ",
    "stale": "STALE",
    "expired": "EXP ",
    "waived": "WAIV",
}


# A check counts as "slowed down" only when it both at least doubled AND grew by
# a meaningful absolute amount — so sub-second jitter never cries wolf.
SLOW_REGRESSION_FACTOR = 2.0
SLOW_REGRESSION_FLOOR = 0.5  # seconds


def _slowdown_note(cap_id: str, prev: float | None, new: float) -> str | None:
    """Flag a real timing regression against the previously-recorded duration."""
    if (
        prev is not None
        and new >= prev * SLOW_REGRESSION_FACTOR
        and (new - prev) >= SLOW_REGRESSION_FLOOR
    ):
        return f"{cap_id}: slower — {prev:.2f}s -> {new:.2f}s (check timing regressed)"
    return None


def _fmt_duration(seconds: float | None) -> str:
    """Render a recorded check duration as a ` (1.23s)` suffix, or "" if unknown."""
    return "" if seconds is None else f" ({seconds:.2f}s)"


def _print_warnings(caps) -> None:
    for cap in caps:
        for w in cap.warnings:
            print(f"warning: {cap.id}: {w}", file=sys.stderr)


def _capability_report(cap, entry, state, root) -> dict:
    """One capability's machine-readable status: always id/state/tier/then, plus
    the evidence relevant to that state (detail, changed deps, waiver, at)."""
    rep = {"id": cap.id, "state": state, "tier": cap.tier, "then": cap.then}
    if entry is not None:
        rep["at"] = entry.at
        if entry.duration is not None:
            rep["duration"] = entry.duration
    if cap.warnings:
        rep["warnings"] = list(cap.warnings)
    if state in ("fail", "error") and entry is not None and entry.detail:
        rep["detail"] = entry.detail
    if state == "code-stale":
        ch = changed_deps(cap, getattr(entry, "files", None), root)
        if ch:
            rep["changed"] = ch
    if state == "waived" and entry is not None and entry.waiver:
        rep["waiver"] = entry.waiver
    return rep


_EVIDENCE_FIELD_ORDER = [
    "id",
    "description",
    "given",
    "when",
    "then",
    "tier",
    "check",
    "state",
    "at",
    "duration",
    "detail",
    "waiver",
    "changed",
]
_EVIDENCE_LABEL_WIDTH = 13  # len("description:") + 1, the longest label


def _evidence_report(cap, entry, state, root) -> dict:
    """The full citable contract for one capability: `_capability_report`'s
    proof fields plus the manifest fields evidence needs but status never
    printed (description/given/when/check)."""
    rep = _capability_report(cap, entry, state, root)
    rep["description"] = cap.description
    rep["given"] = cap.given
    rep["when"] = cap.when
    rep["check"] = cap.check_target
    return rep


def _format_evidence(rep: dict) -> str:
    lines = []
    for key in _EVIDENCE_FIELD_ORDER:
        if key not in rep:
            continue
        label = f"{key}:".ljust(_EVIDENCE_LABEL_WIDTH)
        if key == "waiver":
            w = rep["waiver"]
            lines.append(f"{label}{w['reason']} (until {w['until']})")
        elif key == "duration":
            lines.append(f"{label}{rep['duration']:.2f}s")
        elif key == "changed":
            lines.append(f"{label}{', '.join(rep['changed'])}")
        else:
            lines.append(f"{label}{rep[key]}")
    return "\n".join(lines)


def _require_capability(caps, cap_id: str):
    """Find one capability by id in an already-loaded manifest list. Prints the
    standard 'no capability' error and returns None if it's absent — shared by
    every command that looks up a single capability (ack, evidence, verify
    --capability)."""
    by_id = {c.id: c for c in caps}
    if cap_id not in by_id:
        print(f"error: no capability with id {cap_id!r}", file=sys.stderr)
        return None
    return by_id[cap_id]


def cmd_status(root: Path, now: datetime, as_json: bool = False, check: bool = False) -> int:
    caps = load_manifest(root / MANIFEST_NAME)
    ledger = load_ledger(root / LEDGER_REL)
    reports = []
    for cap in caps:
        entry = ledger.get(cap.id)
        state = capability_state(cap, entry, root, now)
        reports.append((cap, entry, state, _capability_report(cap, entry, state, root)))

    blocking = [r["id"] for *_, r in reports if r["state"] in BLOCK_STATES]
    rc = 1 if (check and blocking) else 0  # --check turns status into a CI gate

    if as_json:
        summary: dict = {}
        for _, _, state, _ in reports:
            summary[state] = summary.get(state, 0) + 1
        print(
            json.dumps(
                {
                    "root": str(root),
                    "capabilities": [r for *_, r in reports],
                    "summary": summary,
                    "blocking": blocking,
                    "ok": not blocking,
                },
                indent=2,
            )
        )
        return rc

    _print_warnings(caps)
    for cap, _entry, state, rep in reports:
        label = _DISPLAY[state]
        line = (
            f"[{_GLYPH.get(label, '?'):5}] {cap.id:30} "
            f"{label:12}{_fmt_duration(rep.get('duration'))}"
        )
        if "changed" in rep:
            changed = rep["changed"]
            more = f", +{len(changed) - 3}" if len(changed) > 3 else ""
            line += f"  (changed: {', '.join(changed[:3])}{more})"
        print(line.rstrip())
    if check and blocking:
        print(
            f"\nnot proven & fresh: {', '.join(blocking)} ({len(blocking)} blocking) "
            f"— run: python -m caps verify --stale",
            file=sys.stderr,
        )
    return rc


def cmd_verify(root: Path, now: datetime, only: str | None, stale: bool = False) -> int:
    caps = load_manifest(root / MANIFEST_NAME)
    _print_warnings(caps)
    ledger = load_ledger(root / LEDGER_REL)
    if only is not None:
        cap = _require_capability(caps, only)
        if cap is None:
            return 2
        caps = [cap]
    elif stale:
        # Re-prove exactly the set the Stop-hook gate would block on, in one go.
        caps = [c for c in caps if capability_state(c, ledger.get(c.id), root, now) in BLOCK_STATES]
        if not caps:
            print("nothing stale — all capabilities are proven & fresh (or waived)")
            return 0

    worst_ok = True
    updated: dict = {}
    for cap in caps:
        # An active waiver suppresses the check during a bare verify; the
        # existing waived entry is preserved. An explicit --capability overrides.
        if only is None and waiver_active(ledger.get(cap.id), now):
            print(f"{cap.id}: skipped (waived)")
            continue
        prev = ledger.get(cap.id)
        result, detail, duration = run_capability(cap, root)
        fmap = None
        if cap.freshness == "code":
            fmap = file_fingerprints(cap, root)
            if len(fmap) > FILE_MAP_LIMIT:
                fmap = None  # broad glob: keep the ledger lean, skip itemizing
        updated[cap.id] = LedgerEntry(
            result=result,
            at=now.isoformat(),
            tier=cap.tier,
            fingerprint=fingerprint(cap, root) if cap.freshness == "code" else None,
            waiver=None,
            detail=detail if result != "pass" else None,
            files=fmap,
            duration=round(duration, 3),
        )
        print(f"{cap.id}: {result}{_fmt_duration(duration)}")
        slow = _slowdown_note(cap.id, prev.duration if prev else None, duration)
        if slow:
            print(slow, file=sys.stderr)
        if result != "pass":
            worst_ok = False
            if detail:
                print(detail, file=sys.stderr)

    # Re-read at save time and merge only the entries we ran, so a concurrent
    # change (e.g. an `ack` made while a slow live check was running) is not
    # clobbered by our now-stale in-memory copy. An active waiver on disk wins
    # over the result we just produced — a routine verify must never silently
    # delete a human's acknowledgment. An explicit --capability overrides it.
    disk = load_ledger(root / LEDGER_REL)
    for cid, entry in updated.items():
        if only is None and waiver_active(disk.get(cid), now):
            print(f"{cid}: kept waived (waiver set during run)")
            continue
        disk[cid] = entry
    save_ledger(root / LEDGER_REL, disk)
    return 0 if worst_ok else 1


def cmd_ack(root: Path, now: datetime, cap_id: str, reason: str, for_: str) -> int:
    caps = load_manifest(root / MANIFEST_NAME)
    cap = _require_capability(caps, cap_id)
    if cap is None:
        return 2
    until = (now + parse_duration(for_)).isoformat()
    ledger = load_ledger(root / LEDGER_REL)
    ledger[cap_id] = LedgerEntry(
        result="waived",
        at=now.isoformat(),
        tier=cap.tier,
        fingerprint=None,
        waiver={"reason": reason, "until": until},
    )
    save_ledger(root / LEDGER_REL, ledger)
    print(f"{cap_id}: waived until {until} ({reason})")
    return 0


def cmd_evidence(root: Path, now: datetime, cap_id: str, as_json: bool = False) -> int:
    caps = load_manifest(root / MANIFEST_NAME)
    cap = _require_capability(caps, cap_id)
    if cap is None:
        return 2
    ledger = load_ledger(root / LEDGER_REL)
    entry = ledger.get(cap.id)
    state = capability_state(cap, entry, root, now)
    rep = _evidence_report(cap, entry, state, root)
    if as_json:
        print(json.dumps(rep, indent=2))
    else:
        print(_format_evidence(rep))
    return 0


def cmd_gate(stdin_text: str, now: datetime) -> int:
    try:
        payload = json.loads(stdin_text or "{}")
        decision = decide(payload, now)
    except Exception as e:  # fail open, but visibly
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": (
                            f"caps gate failed: {e} — capability enforcement skipped this turn"
                        ),
                    }
                }
            )
        )
        return 0
    if decision.block:
        print(json.dumps({"decision": "block", "reason": decision.reason}))
    elif decision.note:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": decision.note,
                    }
                }
            )
        )
    return 0


_DOCTOR_GLYPH = {OK: " OK ", WARN: "WARN", FAIL: "FAIL"}


def cmd_doctor(root: Path, now: datetime, settings_path, as_json: bool = False) -> int:
    """Render `caps doctor`: print (or JSON-emit) the setup findings and return a
    non-zero exit code if any are hard failures."""
    findings = diagnose(root, now, settings_path)
    if as_json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "findings": [{"level": f.level, "message": f.message} for f in findings],
                    "ok": exit_code(findings) == 0,
                },
                indent=2,
            )
        )
        return exit_code(findings)
    print(f"caps doctor — {root}")
    for f in findings:
        print(f"[{_DOCTOR_GLYPH[f.level]}] {f.message}")
    n_fail = sum(f.level == FAIL for f in findings)
    n_warn = sum(f.level == WARN for f in findings)
    print(f"{n_fail} error(s), {n_warn} warning(s)")
    return exit_code(findings)


def cmd_init(target: str, force: bool, install_deps: bool) -> int:
    try:
        results = init_project(target, kit=kit_root(), force=force, install_deps=install_deps)
    except ValueError as e:
        # e.g. `init --force` aimed at the kit itself — refuse cleanly, not with a traceback.
        print(f"error: {e}", file=sys.stderr)
        return 2
    for r in results:
        print(f"  {r.action:11} {r.detail}")
    print()
    print("Next steps:")
    print("  1. Add a capability:  python -m caps add --id <id> --tier <cheap|live> ...")
    print("  2. Prove it:          python -m caps verify")
    print("  3. (optional) enforce on every turn — the wrapper is vendored at")
    print("     bin/caps-stop-gate.sh, but the hook is NOT installed by init.")
    print("     Once this project has a Python with PyYAML, register it with:")
    print("       CAPS_GATE_PYTHON=/path/to/python python -m caps install-hook")
    return 0


def main(argv=None, cwd: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caps")
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="show capability status (read-only)")
    st.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON (state, detail, changed deps, blocking set)",
    )
    st.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any capability is unproven/failed/stale (CI gate)",
    )
    v = sub.add_parser("verify", help="run checks and record proof")
    vsel = v.add_mutually_exclusive_group()
    vsel.add_argument(
        "--capability", dest="only", default=None, help="verify a single capability by id"
    )
    vsel.add_argument(
        "--stale",
        action="store_true",
        help="re-prove only the capabilities the gate would block on",
    )
    a = sub.add_parser("ack", help="record a time-boxed waiver for a capability")
    a.add_argument("capability", help="capability id to waive")
    a.add_argument("--reason", required=True, help="why it can't be proven now")
    a.add_argument(
        "--for", dest="for_", default="24h", help="waiver duration, e.g. 24h (default), 2d, 30m"
    )
    ev = sub.add_parser(
        "evidence", help="print a capability's contract + proof (for citing in docs)"
    )
    ev.add_argument("capability", help="capability id")
    ev.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub.add_parser("gate", help="Stop-hook gate: read hook JSON on stdin, emit allow/block")
    doc = sub.add_parser("doctor", help="diagnose project setup (manifest, checks, ledger, hook)")
    doc.add_argument(
        "--settings",
        default=None,
        help="settings.json to check for the Stop hook (default: ~/.claude/settings.json)",
    )
    doc.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ih = sub.add_parser("install-hook", help="register the Stop-hook gate in settings.json")
    ih.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    ih.add_argument(
        "--command",
        dest="hook_command",
        default=None,
        help="hook command (defaults to this kit's bin/caps-stop-gate.sh)",
    )
    uh = sub.add_parser("uninstall-hook", help="remove the Stop-hook gate from settings.json")
    uh.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))

    sub.add_parser(
        "ponytail", help="print the 'lazy senior dev' posture (what the SessionStart hook injects)"
    )
    sub.add_parser(
        "review",
        help="print the over-engineering review rubric (apply it to the diff under review)",
    )
    ip = sub.add_parser(
        "install-ponytail", help="register a SessionStart hook that injects the ponytail posture"
    )
    ip.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    ip.add_argument(
        "--command",
        dest="hook_command",
        default=None,
        help="hook command (defaults to this kit's bin/caps-ponytail.sh)",
    )
    up = sub.add_parser("uninstall-ponytail", help="remove the ponytail SessionStart hook")
    up.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))

    ad = sub.add_parser("add", help="add a capability to the manifest (never-proven)")
    ad.add_argument("--id", required=True)
    ad.add_argument("--description", required=True)
    ad.add_argument("--given", required=True)
    ad.add_argument("--when", required=True)
    ad.add_argument("--then", required=True)
    ad.add_argument("--tier", required=True, choices=["cheap", "live"])
    ad.add_argument("--deps", action="append", default=[], help="dep glob (repeat for multiple)")
    grp = ad.add_mutually_exclusive_group(required=True)
    grp.add_argument("--check", help="pytest node, e.g. checks/test_x.py::test_x")
    grp.add_argument("--shell", help="shell command; exit 0 = proven")
    ad.add_argument("--manifest", default=None, help="path to capabilities.yaml")

    ini = sub.add_parser("init", help="vendor the framework into a project (drop-in installer)")
    ini.add_argument("--target", default=None, help="target dir (default: cwd)")
    ini.add_argument(
        "--force", action="store_true", help="re-overwrite vendored ctk/caps/bin (never user files)"
    )
    ini.add_argument(
        "--install-deps",
        dest="install_deps",
        action="store_true",
        help="pip-install PyYAML into the active environment",
    )

    args = parser.parse_args(argv)
    now = datetime.now(UTC)

    if args.command == "gate":
        return cmd_gate(sys.stdin.read(), now)
    if args.command == "install-hook":
        kit = Path(__file__).resolve().parent.parent
        cmd = args.hook_command or str(kit / "bin" / "caps-stop-gate.sh")
        venv_py = kit / ".venv" / "bin" / "python"
        if not venv_py.exists():
            print(
                f"warning: {venv_py} not found — run ./run_tests.sh once so the "
                f"hook has an interpreter (gate will fail open until then)",
                file=sys.stderr,
            )
        install_hook(args.settings, command=cmd)
        print(f"installed Stop-hook gate -> {args.settings}")
        return 0
    if args.command == "uninstall-hook":
        uninstall_hook(args.settings)
        print(f"removed Stop-hook gate from {args.settings}")
        return 0
    if args.command == "ponytail":
        print(ponytail_instructions())
        return 0
    if args.command == "review":
        print(review_rubric())
        return 0
    if args.command == "install-ponytail":
        kit = Path(__file__).resolve().parent.parent
        cmd = args.hook_command or str(kit / "bin" / "caps-ponytail.sh")
        venv_py = kit / ".venv" / "bin" / "python"
        if not venv_py.exists():
            print(
                f"warning: {venv_py} not found — run ./run_tests.sh once so the "
                f"hook has an interpreter (posture stays silent until then)",
                file=sys.stderr,
            )
        install_hook(
            args.settings,
            command=cmd,
            event="SessionStart",
            tag=PONYTAIL_TAG,
            matcher="startup|resume|clear|compact",
        )
        print(f"installed ponytail SessionStart hook -> {args.settings}")
        return 0
    if args.command == "uninstall-ponytail":
        uninstall_hook(args.settings, event="SessionStart", tag=PONYTAIL_TAG)
        print(f"removed ponytail SessionStart hook from {args.settings}")
        return 0

    if args.command == "add":
        if args.manifest:
            manifest_path = Path(args.manifest)
        else:
            start = Path(cwd) if cwd else Path.cwd()
            manifest_path = (find_root(start) or start) / MANIFEST_NAME
        try:
            add_capability(
                manifest_path,
                id=args.id,
                description=args.description,
                given=args.given,
                when=args.when,
                then=args.then,
                tier=args.tier,
                deps=args.deps,
                check=args.check,
                shell=args.shell,
            )
        except (ManifestEditError, ManifestError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"added capability {args.id!r} (never-proven) -> {manifest_path}")
        return 0

    if args.command == "init":
        target = args.target or (cwd if cwd else str(Path.cwd()))
        return cmd_init(target, args.force, args.install_deps)

    start = Path(cwd) if cwd else Path.cwd()
    root = find_root(start)
    if root is None:
        print(f"error: no {MANIFEST_NAME} found from {start}", file=sys.stderr)
        return 2

    try:
        if args.command == "status":
            return cmd_status(root, now, args.json, args.check)
        if args.command == "verify":
            return cmd_verify(root, now, args.only, args.stale)
        if args.command == "ack":
            return cmd_ack(root, now, args.capability, args.reason, args.for_)
        if args.command == "evidence":
            return cmd_evidence(root, now, args.capability, args.json)
        if args.command == "doctor":
            return cmd_doctor(root, now, args.settings, args.json)
    except (ManifestError, FreshnessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
