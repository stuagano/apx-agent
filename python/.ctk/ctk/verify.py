"""Read-after-write proof gate — confirm the thing actually exists, not just that the call succeeded.

    ctk.verify(Artifact("out.json"))                       # exists and non-empty
    ctk.verify(Artifact("out.json", is_json=True))         # ...and parses as JSON
    ctk.verify(Artifact("out.json", min_bytes=100))        # ...and is big enough
    ctk.verify(Artifact("out.csv", must_contain="order"))  # ...and contains a string
    ctk.verify(Artifact("out.csv", rows=5))                # ...and has 5 data rows

Or build a checklist that collects every check and reports ALL failures together:

    cl = ctk.Checklist()
    cl.file_exists("report.csv")
    cl.exit_code(result.returncode, 0)
    cl.verify()  # raises VerificationError listing every failed check
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from ctk.gate import Check, CheckFailure


class VerificationError(AssertionError):
    """One or more verification checks failed."""


class Artifact:
    """A file that must exist, be non-empty, and match any constraints you declare.

    `check()` raises CheckFailure on the first miss. `problems()` returns every
    miss as a string, which is what `verify` and `claim_vs_reality` report.
    """

    def __init__(
        self,
        path: str,
        *,
        min_bytes: int | None = None,
        max_bytes: int | None = None,
        must_contain: str | list[str] | None = None,
        must_not_contain: str | list[str] | None = None,
        contains: str | list[str] | None = None,
        is_json: bool = False,
        json: bool | None = None,
        json_keys: list[str] | None = None,
        json_where: list[tuple[str, Callable[[Any], bool]]] | None = None,
        line_count: tuple[int, int] | None = None,
        rows: int | None = None,
        matches: str | None = None,
        newer_than: float | None = None,
    ) -> None:
        self.path = path
        self.min_bytes = min_bytes
        self.max_bytes = max_bytes
        self.must_contain = _as_list(contains if contains is not None else must_contain)
        self.must_not_contain = _as_list(must_not_contain)
        self.is_json = is_json or bool(json)
        self.json_keys = json_keys or []
        self.json_where = json_where or []
        self.line_count = line_count
        self.rows = rows
        self.matches = matches
        self.newer_than = newer_than
        self._parsed: Any = None

    def problems(self) -> list[str]:
        out: list[str] = []
        for check in self.checks():
            try:
                check.run()
            except CheckFailure as exc:
                out.append(str(exc))
        return out

    def check(self) -> Artifact:
        for check in self.checks():
            check.run()
        return self

    def read(self) -> str:
        self.check()
        return open(self.path, encoding="utf-8").read()

    def value(self) -> Any:
        self.check()
        if self._parsed is None:
            raise CheckFailure(self.path, "json", "not parsed")
        return self._parsed

    def checks(self) -> list[Check]:
        """One Check per declared fact. Absent and empty files stop there."""
        if not os.path.isfile(self.path):
            return [_miss(self.path, "the file to exist", f"MISSING: {self.path} does not exist")]

        size = os.path.getsize(self.path)
        if size == 0:
            return [_miss(self.path, "the file to be non-empty", f"EMPTY: {self.path} is 0 bytes")]

        checks = [
            _ok(self.path, "file exists"),
            self._min_bytes(size),
            self._max_bytes(size),
            self._newer_than(),
            *self._content_checks(),
        ]
        return [c for c in checks if c is not None]

    def _min_bytes(self, size: int) -> Check | None:
        if self.min_bytes is None:
            return None
        return _cmp(
            self.path,
            f"size >= {self.min_bytes} bytes",
            None if size >= self.min_bytes else f"{size} bytes",
        )

    def _max_bytes(self, size: int) -> Check | None:
        if self.max_bytes is None:
            return None
        return _cmp(
            self.path,
            f"size <= {self.max_bytes} bytes",
            None if size <= self.max_bytes else f"{size} bytes",
        )

    def _newer_than(self) -> Check | None:
        if self.newer_than is None:
            return None
        mtime = os.path.getmtime(self.path)
        return _cmp(
            self.path,
            f"modified after {self.newer_than}",
            None if mtime >= self.newer_than else f"mtime {mtime} is older",
        )

    def _content_checks(self) -> list[Check | None]:
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        checks: list[Check | None] = []
        for needle in self.must_contain:
            checks.append(_cmp(
                self.path,
                f"to contain {needle!r}",
                None if needle in text else f"missing content {needle!r}",
            ))
        for needle in self.must_not_contain:
            checks.append(_cmp(
                self.path,
                f"content NOT to contain {needle!r}",
                None if needle not in text else f"found forbidden {needle!r}",
            ))
        if self.matches is not None:
            checks.append(_cmp(
                self.path,
                f"content to match {self.matches!r}",
                None if re.search(self.matches, text) else "it does not",
            ))
        if self.line_count is not None:
            lo, hi = self.line_count
            n = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
            checks.append(_cmp(
                self.path,
                f"between {lo} and {hi} lines",
                None if lo <= n <= hi else f"{n} lines",
            ))
        if self.is_json or self.json_keys or self.json_where:
            checks.extend(self._json_checks(text))
        if self.rows is not None:
            n = len([ln for ln in text.splitlines()[1:] if ln.strip()])
            checks.append(_cmp(
                self.path,
                f"{self.rows} data rows",
                None if n == self.rows else f"{n} data rows",
            ))
        return checks

    def _json_checks(self, text: str) -> list[Check | None]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return [_miss(self.path, "valid JSON", f"JSON parse error: {exc}")]
        self._parsed = data
        checks: list[Check | None] = [_ok(self.path, "valid JSON")]
        if self.json_keys and not isinstance(data, dict):
            return checks + [_miss(self.path, "a JSON object", type(data).__name__)]
        for key in self.json_keys:
            checks.append(_cmp(
                self.path,
                f"JSON key {key!r}",
                None if key in data else "it is missing",
            ))
        for expr, pred in self.json_where:
            checks.append(self._json_where(data, expr, pred))
        return checks

    def _json_where(self, data: Any, expr: str, pred: Callable[[Any], bool]) -> Check:
        actual = _resolve(data, expr)
        return _cmp(
            self.path,
            f"{expr} to satisfy condition",
            None if pred(actual) else f"{expr}={actual!r}",
        )


def verify(*artifacts: Artifact) -> None:
    """Verify every declared artifact. Raise one report listing all problems."""
    problems = [p for a in artifacts for p in a.problems()]
    if problems:
        bullet = "\n  - ".join(problems)
        raise VerificationError(
            f"{len(problems)} verification check(s) failed:\n  - {bullet}"
        )


def claim_vs_reality(
    claim: Mapping[str, Any],
    *,
    min_bytes: dict[str, int] | None = None,
    is_json: set[str] | None = None,
    json_keys: dict[str, list[str]] | None = None,
    must_contain: dict[str, str | list[str]] | None = None,
) -> None:
    """The claim-vs-reality check, from a claim dict.

    `claim` maps a label to a file path (or a list of them) the agent says it
    produced. Pass `min_bytes` / `is_json` / `json_keys` / `must_contain` keyed
    by the same label to require non-trivial content. Raises VerificationError
    naming every path that is missing, empty, or doesn't satisfy its constraints.
    """
    arts: list[Artifact] = []
    json_files = is_json or set()
    sizes = min_bytes or {}
    keys = json_keys or {}
    contains = must_contain or {}
    for label, paths in claim.items():
        for path in paths if isinstance(paths, (list, tuple)) else [paths]:
            if not isinstance(path, str):
                continue
            arts.append(Artifact(
                path,
                min_bytes=sizes.get(label),
                is_json=label in json_files,
                json_keys=keys.get(label),
                must_contain=contains.get(label),
            ))
    verify(*arts)


class _ListedCheck:
    def __init__(self, ok: bool, message: str) -> None:
        self.ok = ok
        self.message = message


class Checklist:
    """Collect many heterogeneous checks, then verify them all at once.

    Unlike `verify` (which checks files), a Checklist can also check exit codes,
    counts, and arbitrary conditions. Nothing is raised until you call `.verify()`,
    so you always see every failure, not just the first.
    """

    def __init__(self) -> None:
        self._checks: list[_ListedCheck] = []

    def file_exists(self, path: str, *, min_bytes: int = 1) -> Checklist:
        art = Artifact(path, min_bytes=min_bytes)
        problems = art.problems()
        if problems:
            self._checks.append(_ListedCheck(False, "; ".join(problems)))
        else:
            size = os.path.getsize(path)
            self._checks.append(_ListedCheck(True, f"exists: {path} ({size} bytes)"))
        return self

    def exit_code(self, actual: int, expected: int = 0, *, label: str = "process") -> Checklist:
        ok = actual == expected
        msg = f"{label} exit {actual}" + ("" if ok else f" (expected {expected})")
        self._checks.append(_ListedCheck(ok, msg))
        return self

    def equals(self, actual: Any, expected: Any, *, label: str) -> Checklist:
        ok = actual == expected
        msg = f"{label}: {actual!r}" + ("" if ok else f" (expected {expected!r})")
        self._checks.append(_ListedCheck(ok, msg))
        return self

    def count(self, actual: int, expected: int, *, label: str) -> Checklist:
        return self.equals(actual, expected, label=label)

    def that(self, condition: bool, message: str) -> Checklist:
        self._checks.append(_ListedCheck(condition, message))
        return self

    def verify(self) -> None:
        failed = [c for c in self._checks if not c.ok]
        if failed:
            bullet = "\n  - ".join(c.message for c in failed)
            raise VerificationError(
                f"{len(failed)} check(s) failed:\n  - {bullet}\n"
                f"({len(self._checks) - len(failed)} passed)"
            )

    def ok(self) -> bool:
        return all(c.ok for c in self._checks)


def _as_list(v: str | list[str] | None) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def _miss(name: str, expected: str, found: str) -> Check:
    return Check(name, expected, lambda: found)


def _ok(name: str, expected: str) -> Check:
    return Check(name, expected, lambda: None)


def _cmp(name: str, expected: str, found: str | None) -> Check:
    return Check(name, expected, lambda: found)


def _resolve(data: Any, expr: str) -> Any:
    cur = data
    for part in expr.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return cur
