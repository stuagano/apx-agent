"""The gate primitive: one Check that either passes or raises CheckFailure.

verify collects a Check per declared fact and reports every miss together.
A Check is a named predicate that raises CheckFailure, with what was expected
set against what was found.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class CheckFailure(AssertionError):
    def __init__(self, name: str, expected: str, found: str) -> None:
        self.name = name
        self.expected = expected
        self.found = found
        super().__init__(f"{name}: expected {expected}, found {found}")


@dataclass(frozen=True)
class Check:
    """A named predicate. `run()` raises CheckFailure when it does not hold."""

    name: str
    expected: str
    predicate: Callable[[], str | None]

    def run(self) -> None:
        found = self.predicate()
        if found is not None:
            raise CheckFailure(self.name, self.expected, found)
