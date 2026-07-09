from __future__ import annotations

import re
import sys
from pathlib import Path

import ctk

from .manifest import Capability

# Reserved shell exit code meaning "could not run / resource unreachable".
ERROR_EXIT = 3

# Checks (esp. live ones that shell out to an LLM) need far more than ctk.run's
# 60s default — a sub-process timeout there gets misclassified as 'error'.
# ponytail: one generous ceiling; make it per-capability only if a check needs more.
CHECK_TIMEOUT = 900.0

# How much of a failing check's output to keep so the gate can show *why* it
# failed without anyone re-running it. Kept modest so the committed ledger stays
# readable and diff-friendly.
SNIPPET_MAX = 1500


def _snippet(r: ctk.RunResult) -> str:
    """The most useful tail of a failed check's output. For pytest the failure
    summary lands near the end, so tailing (not heading) is what you want."""
    parts = [p.strip() for p in (r.stdout, r.stderr) if p and p.strip()]
    body = "\n".join(parts).strip()
    if len(body) > SNIPPET_MAX:
        body = "...[truncated]\n" + body[-SNIPPET_MAX:]
    return body


def run_capability(capability: Capability, root: str | Path) -> tuple[str, str | None, float]:
    """Execute the check and classify the outcome, returning (result, detail,
    duration).

    result is 'pass' | 'fail' | 'error'; detail is a trimmed snippet of the
    check's output on a non-pass outcome (so the gate can show why), or None on
    pass; duration is the wall-clock seconds the check took.

    pytest: exit 0 -> pass, 1 -> fail, anything else (collection/internal error,
            no tests) -> error.
    shell:  exit 0 -> pass, ERROR_EXIT (3) -> error, any other non-zero -> fail.
    """
    root = str(root)
    if capability.check_kind == "pytest":
        r = ctk.run(
            [
                sys.executable,
                "-m",
                "pytest",
                capability.check_target,
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=root,
            timeout=CHECK_TIMEOUT,
        )
        if r.returncode == 0:
            # exit 0 covers both "all passed" and "all skipped". A skip means the
            # check couldn't run (e.g. live CLI unavailable) — that's un-proven,
            # not proven. Treat "nothing actually passed" as error, like ERROR_EXIT.
            if re.search(r"\b\d+ passed\b", r.stdout):
                return "pass", None, r.duration
            return "error", _snippet(r), r.duration
        result = "fail" if r.returncode == 1 else "error"
        return result, _snippet(r), r.duration

    # shell — wrap in /bin/sh so builtins (exit, cd, etc.) work correctly
    r = ctk.run(["/bin/sh", "-c", capability.check_target], cwd=root, timeout=CHECK_TIMEOUT)
    if r.returncode == 0:
        return "pass", None, r.duration
    result = "error" if r.returncode == ERROR_EXIT else "fail"
    return result, _snippet(r), r.duration
