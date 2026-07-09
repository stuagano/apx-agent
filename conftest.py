"""
Shared pytest fixtures and a guard plugin for the kit.

Most useful pieces:
  * workspace        : an isolated tmp dir + helper to write/path files.
  * run_started_at   : timestamp before your action, for Artifact(newer_than=...).
  * fail_on_error_log: autouse — a test FAILS if your code logged ERROR/CRITICAL.
                       This is the runtime counterpart to the swallowed-exception
                       scanner: even if an error is caught, if it was logged at
                       ERROR level the test won't pass quietly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from ctk.logguard import CapturingHandler


@dataclass
class Workspace:
    root: Path

    def path(self, *parts: str) -> str:
        return str(self.root.joinpath(*parts))

    def write(self, name: str, content: str) -> str:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return str(p)

    def read(self, name: str) -> str:
        return (self.root / name).read_text()


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """Isolated scratch directory for a test's inputs/outputs."""
    return Workspace(root=tmp_path)


@pytest.fixture
def run_started_at() -> float:
    """
    Epoch seconds captured at fixture setup. Pass to Artifact(newer_than=...)
    to prove an output file was actually (re)written during this test, not left
    over from a previous run.
    """
    return time.time()


# ---------------------------------------------------------------------------
# Guard: fail tests that logged ERROR/CRITICAL, unless explicitly allowed.
# Opt out per-test with @pytest.mark.allow_error_logs
# The capturing handler lives in ctk.logguard so it is importable under one
# unambiguous name (see that module for why).
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_error_logs: do not fail the test if it logs at ERROR/CRITICAL level",
    )


@pytest.fixture(autouse=True)
def fail_on_error_log(request: pytest.FixtureRequest):
    if request.node.get_closest_marker("allow_error_logs"):
        yield
        return

    handler = CapturingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)

    if handler.records:
        msgs = "\n".join(
            f"  - {r.levelname} {r.name}: {r.getMessage()}" for r in handler.records[:20]
        )
        pytest.fail(
            "code logged ERROR/CRITICAL during this test (likely a swallowed/"
            "handled-but-real failure):\n" + msgs,
            pytrace=False,
        )
