"""Scaffold output content check (claim-vs-reality, via the vendored ctk kit).

Complements ``test_cli.py::test_scaffold_creates_expected_files``, which only
asserts the generated files ``.exists()``. A scaffold that wrote an *empty* or
content-less ``agent.py`` would pass that check while producing a broken
project. These ``ctk.Artifact`` checks assert the files are real: non-empty AND
carrying the wiring that makes the scaffold runnable (``DataAgent`` in
``agent.py``, ``create_app`` in ``app.py``, the ``[tool.apx.agent]`` block in
``pyproject.toml``).

Uses ``ctk`` (python/.ctk, on the path via ``pythonpath`` in pyproject) — the
claim-vs-reality pattern is what that kit is for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apx_agent.cli import main
from ctk import Artifact, expect, verify


@pytest.mark.unit
def test_scaffold_outputs_are_real_not_just_present(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "agents", "scaffold", "my_agent",
            "--target", "model-serving",
            "--dir", str(tmp_path),
            "--no-yaml",
        ],
    )
    assert result.exit_code == 0, result.output
    expect(result.output).nonempty().verify()

    base = tmp_path / "my_agent"
    # Reality, not just existence: non-empty AND actually wired.
    verify(
        Artifact(str(base / "agent.py"), min_bytes=40, must_contain="DataAgent"),
        Artifact(str(base / "app.py"), min_bytes=20, must_contain="create_app"),
        Artifact(str(base / "pyproject.toml"), min_bytes=40, must_contain="[tool.apx.agent]"),
        Artifact(str(base / "README.md"), min_bytes=20),
    )
