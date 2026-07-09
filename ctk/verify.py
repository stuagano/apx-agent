"""
Agent verification: claim vs. reality.

This is the heart of the kit. An agent (or a script) reports "done". That report
is just a claim. This module checks the *actual side effects* — the files that
should exist, their content, the state that should have changed — and fails if
reality doesn't match the claim.

Two main tools:
  * Artifact + verify(...) : declare the concrete outputs that must exist, then check them.
  * claim_vs_reality(...)  : given a claimed success and a verifier, fail if the
                             claim is "success" but reality disagrees (the exact
                             "Claude says done, isn't" bug).
  * Checklist              : run many named checks, get one consolidated report.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


class VerificationError(AssertionError):
    """Raised when reality does not match the claim."""


@dataclass
class Artifact:
    """
    A concrete output the agent/script claims to have produced.

    Verification checks, in order: exists -> size -> contains/not-contains ->
    JSON validity -> required JSON keys.
    """

    path: str
    min_bytes: int = 1
    must_contain: str | None = None
    must_not_contain: str | None = None
    is_json: bool = False
    json_keys: Sequence[str] = field(default_factory=tuple)
    newer_than: float | None = None  # epoch seconds; catches stale/leftover files

    def problems(self) -> list[str]:
        probs: list[str] = []
        if not os.path.exists(self.path):
            return [f"{self.path}: does not exist (claimed produced, but missing)"]

        size = os.path.getsize(self.path)
        if size < self.min_bytes:
            probs.append(
                f"{self.path}: only {size} bytes (< {self.min_bytes}); "
                f"empty/near-empty output is a classic silent failure"
            )

        if self.newer_than is not None:
            mtime = os.path.getmtime(self.path)
            if mtime < self.newer_than:
                probs.append(
                    f"{self.path}: not modified during this run "
                    f"(mtime {mtime:.0f} < expected {self.newer_than:.0f}); "
                    f"this is a stale file from a previous run, not fresh output"
                )

        data = None
        if self.must_contain or self.must_not_contain or self.is_json:
            with open(self.path, errors="replace") as f:
                data = f.read()

        if self.must_contain and self.must_contain not in (data or ""):
            probs.append(f"{self.path}: missing required text {self.must_contain!r}")
        if self.must_not_contain and self.must_not_contain in (data or ""):
            probs.append(f"{self.path}: contains forbidden text {self.must_not_contain!r}")

        if self.is_json:
            try:
                obj = json.loads(data or "")
            except json.JSONDecodeError as e:
                probs.append(f"{self.path}: not valid JSON ({e})")
            else:
                if self.json_keys:
                    if not isinstance(obj, dict):
                        probs.append(f"{self.path}: JSON is not an object; cannot check keys")
                    else:
                        missing = [k for k in self.json_keys if k not in obj]
                        if missing:
                            probs.append(f"{self.path}: JSON missing keys {missing}")
        return probs


def verify(*artifacts: Artifact) -> None:
    """Verify every declared artifact. Raise one report listing all problems."""
    all_problems: list[str] = []
    for a in artifacts:
        all_problems.extend(a.problems())
    if all_problems:
        raise VerificationError(
            "reality does not match the claim — "
            f"{len(all_problems)} problem(s):\n  - " + "\n  - ".join(all_problems)
        )


def claim_vs_reality(
    claimed_success: bool,
    verifier: Callable[[], None],
    *,
    claim_label: str = "task",
) -> None:
    """
    Reconcile a claim with reality.

    `claimed_success` : what the agent/script reported (e.g. exit 0, "done", ok=True).
    `verifier`        : a callable that raises if reality is wrong (e.g. lambda: verify(...)).

    Failure cases:
      * claimed success but verifier raised  -> the dangerous silent failure.
      * claimed failure but verifier passed  -> a false alarm worth knowing about.
    """
    try:
        verifier()
        reality_ok = True
        reality_err = None
    except AssertionError as e:
        reality_ok = False
        reality_err = str(e)

    if claimed_success and not reality_ok:
        raise VerificationError(
            f"SILENT FAILURE on {claim_label!r}: it reported success, "
            f"but verification failed:\n{reality_err}"
        )
    if not claimed_success and reality_ok:
        raise VerificationError(
            f"FALSE ALARM on {claim_label!r}: it reported failure, but verification passed. "
            f"Check the error handling/exit code."
        )


@dataclass
class _Check:
    name: str
    fn: Callable[[], None]


class Checklist:
    """
    Run many named checks and report all results at once. Good for verifying a
    multi-step agent run where you want to see everything that's wrong, not just
    the first failure.

        cl = Checklist("deploy")
        cl.add("config written", lambda: verify(Artifact("config.json", is_json=True)))
        cl.add("log mentions success", lambda: assert_file("run.log", must_contain="OK"))
        cl.run()   # raises with a full pass/fail table if anything failed
    """

    def __init__(self, name: str = "checklist"):
        self.name = name
        self._checks: list[_Check] = []

    def add(self, name: str, fn: Callable[[], None]) -> Checklist:
        self._checks.append(_Check(name, fn))
        return self

    def run(self, *, raise_on_fail: bool = True) -> list[tuple[str, bool, str]]:
        results: list[tuple[str, bool, str]] = []
        for c in self._checks:
            try:
                c.fn()
                results.append((c.name, True, ""))
            except AssertionError as e:
                results.append((c.name, False, str(e)))

        failed = [r for r in results if not r[1]]
        if failed and raise_on_fail:
            lines = []
            for name, ok, err in results:
                mark = "PASS" if ok else "FAIL"
                lines.append(f"  [{mark}] {name}" + (f"\n        {err}" if err else ""))
            raise VerificationError(
                f"checklist {self.name!r}: {len(failed)}/{len(results)} checks failed\n"
                + "\n".join(lines)
            )
        return results
