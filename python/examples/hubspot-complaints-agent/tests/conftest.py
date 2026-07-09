"""Shared fixtures for hubspot-complaints-agent example tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _add_example_to_path() -> None:
    """Make agent.py / scripts/ importable from the example directory."""
    example_dir = Path(__file__).parent.parent
    if str(example_dir) not in sys.path:
        sys.path.insert(0, str(example_dir))
    yield
