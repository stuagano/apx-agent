"""
Logging guard helper.

Backs the kit's `fail_on_error_log` fixture (failure mode #3: swallowed/
handled-but-real errors that get logged at ERROR/CRITICAL and otherwise pass
quietly). Lives in the package — not in ``conftest.py`` — so it is importable
under one unambiguous name even though the repo has multiple ``conftest.py``
files (a bare ``import conftest`` resolves ambiguously across them).
"""

from __future__ import annotations

import logging


class CapturingHandler(logging.Handler):
    """Capture ERROR/CRITICAL log records so a test can assert on them."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
