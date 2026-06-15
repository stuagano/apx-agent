"""
ctk pytest plugin — fixtures + the error-log guard, packaged as OPT-IN.

apx-agent does NOT enable this globally (it would change behaviour for the whole
existing suite). Turn it on for a specific test tree in one of two ways:

1. Scoped (recommended) — drop a conftest.py in the test dir that wants it:

       # python/tests/<area>/conftest.py
       from ctk.pytest_plugin import workspace, run_started_at, fail_on_error_log  # noqa: F401

   The autouse `fail_on_error_log` guard then applies only to that dir and below.

2. Whole run — `uv run pytest -p ctk.pytest_plugin ...`

The `ctk` helpers themselves (run, expect, verify, Artifact, must, lint) need no
plugin — just `from ctk import ...`; .ctk is on the path via pyproject's
`pythonpath`.

Opt out of the guard for a single test that legitimately logs ERROR:
`@pytest.mark.allow_error_logs`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from .logguard import CapturingHandler


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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_error_logs: do not fail the test if it logs at ERROR/CRITICAL level",
    )


@pytest.fixture(autouse=True)
def fail_on_error_log(request: pytest.FixtureRequest):
    """Fail a test if its code logged at ERROR/CRITICAL — the runtime counterpart
    to the swallowed-exception scanner. Opt out with @pytest.mark.allow_error_logs."""
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
