"""
Loud, plain-function assertions with rich failure messages.

Use these when a fluent chain is overkill. The rule: every assertion either
passes for a real reason or fails with enough context to act on.
"""

from __future__ import annotations

import os
from typing import Any


class CheckError(AssertionError):
    pass


def must(condition: Any, message: str) -> None:
    """
    The base building block. `assert` can be stripped with python -O; `must`
    cannot. Use it anywhere a silent pass would be dangerous.
    """
    if not condition:
        raise CheckError(f"requirement failed: {message}")


def assert_eq(actual: Any, expected: Any, message: str = "") -> None:
    if actual != expected:
        raise CheckError(
            f"{message + ': ' if message else ''}values differ\n"
            f"  expected: {expected!r}\n"
            f"  actual:   {actual!r}"
        )


def assert_nonempty(value: Any, message: str = "value") -> None:
    empty = (
        value is None
        or (hasattr(value, "__len__") and len(value) == 0)
        or (isinstance(value, str) and not value.strip())
    )
    if empty:
        raise CheckError(f"{message} was empty/None (looked successful but had no content)")


def assert_file(
    path: str,
    *,
    min_bytes: int = 1,
    must_contain: str | None = None,
) -> None:
    """Assert a file exists, is non-trivial in size, and optionally contains text."""
    if not os.path.exists(path):
        raise CheckError(f"expected file does not exist: {path}")
    size = os.path.getsize(path)
    if size < min_bytes:
        raise CheckError(
            f"file {path} exists but is too small ({size} bytes < {min_bytes}); "
            f"likely a silent failure that created an empty file"
        )
    if must_contain is not None:
        with open(path, errors="replace") as f:
            data = f.read()
        if must_contain not in data:
            raise CheckError(f"file {path} does not contain expected text {must_contain!r}")
