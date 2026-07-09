r"""
Output contracts: declarative validation of *what the output should look like*.

This is the answer to "no output validation". Instead of eyeballing output, you
declare the shape it must have. Every check runs; you get ALL failures at once,
not just the first.

    expect(result).nonempty().matches(r"\d+ rows").is_json().has_keys("rows", "ok")
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any


class ContractError(AssertionError):
    """Raised when one or more contract checks fail."""


class Expect:
    """
    Fluent, accumulating validator. Each method records a pass/fail and returns
    self so you can chain. Every check runs; call .verify() at the end of the
    chain to raise one report with all accumulated failures.
    """

    def __init__(self, value: Any, *, label: str = "value"):
        self.value = value
        self.label = label
        self._failures: list[str] = []
        self._parsed_json: Any = None
        self._json_done = False

    # ----- core recording -----
    def _check(self, ok: bool, msg: str) -> Expect:
        if not ok:
            self._failures.append(f"[{self.label}] {msg}")
        return self

    def _context(self) -> str:
        v = self.value
        s = v if isinstance(v, str) else repr(v)
        if len(s) > 1500:
            s = s[:1500] + "...[truncated]"
        return f"\n--- actual ---\n{s}\n--------------"

    # ----- string / generic checks -----
    def nonempty(self) -> Expect:
        v = self.value
        empty = (
            v is None
            or (hasattr(v, "__len__") and len(v) == 0)
            or (isinstance(v, str) and not v.strip())
        )
        return self._check(not empty, "expected non-empty value")

    def equals(self, other: Any) -> Expect:
        return self._check(self.value == other, f"expected == {other!r}")

    def contains(self, sub: Any) -> Expect:
        try:
            ok = sub in self.value
        except TypeError:
            ok = False
        return self._check(ok, f"expected to contain {sub!r}")

    def matches(self, pattern: str) -> Expect:
        ok = isinstance(self.value, str) and re.search(pattern, self.value) is not None
        return self._check(ok, f"expected to match /{pattern}/")

    def not_matches(self, pattern: str) -> Expect:
        ok = not (isinstance(self.value, str) and re.search(pattern, self.value))
        return self._check(ok, f"expected NOT to match /{pattern}/")

    def min_len(self, n: int) -> Expect:
        ok = hasattr(self.value, "__len__") and len(self.value) >= n
        got = len(self.value) if hasattr(self.value, "__len__") else "n/a"
        return self._check(ok, f"expected length >= {n}, got {got}")

    def one_of(self, options: Iterable[Any]) -> Expect:
        opts = list(options)
        return self._check(self.value in opts, f"expected one of {opts!r}")

    def satisfies(self, predicate, desc: str = "custom predicate") -> Expect:
        try:
            ok = bool(predicate(self.value))
        except Exception as e:  # noqa: BLE001 - report, don't swallow
            ok = False
            desc = f"{desc} (raised {type(e).__name__}: {e})"
        return self._check(ok, f"expected to satisfy: {desc}")

    # ----- JSON checks -----
    def is_json(self) -> Expect:
        try:
            self._parsed_json = (
                json.loads(self.value) if isinstance(self.value, str) else self.value
            )
            self._json_done = True
            return self._check(True, "")
        except (json.JSONDecodeError, TypeError) as e:
            self._json_done = False
            return self._check(False, f"expected valid JSON ({e})")

    def has_keys(self, *keys: str) -> Expect:
        obj = self._parsed_json if self._json_done else self.value
        if not isinstance(obj, dict):
            return self._check(False, f"expected a JSON object to check keys {keys}")
        missing = [k for k in keys if k not in obj]
        return self._check(not missing, f"missing keys: {missing}")

    # ----- terminal -----
    def verify(self) -> Any:
        """Raise ContractError with every accumulated failure. Returns value."""
        if self._failures:
            raise ContractError(
                "output contract failed:\n  - " + "\n  - ".join(self._failures) + self._context()
            )
        return self.value


def expect(value: Any, *, label: str = "value") -> Expect:
    """Start an output contract. Remember to end the chain with .verify()."""
    return Expect(value, label=label)
