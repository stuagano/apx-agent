"""Reality check for the generate-produces-a-describable-project capability."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apx_agent.cli import main


def test_generate_produces_a_describable_project(tmp_path: Path) -> None:
    classify_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({
            "template": "base", "name": "ctk-helper-agent", "persona": None,
            "objective": None, "join_key": None, "catalog_hint": None,
            "schema_hint": None, "missing": [],
        })
    ))])
    author_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=(
            "name: ctk-helper-agent\nmodel: databricks-claude-sonnet-4-6\n"
            "instructions: Answer general questions.\ntools: []\n"
        )
    ))])
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = [classify_response, author_response]

    with patch("databricks.sdk.WorkspaceClient", return_value=fake_ws):
        result = CliRunner().invoke(
            main, ["generate", "a general-purpose helper agent", "--dir", str(tmp_path)],
        )
    assert result.exit_code == 0, result.output

    project = tmp_path / "ctk-helper-agent"
    assert (project / "agent.py").read_text().strip(), "agent.py must be non-empty"

    # click.testing.CliRunner.invoke has no cwd kwarg — chdir manually.
    # Also clear the bare "agent" module: other tests import an agent.py
    # under that same default module name from a different directory, and
    # importlib.import_module would otherwise hand back a cached module.
    prev = os.getcwd()
    sys.modules.pop("agent", None)
    os.chdir(project)
    try:
        describe = CliRunner().invoke(main, ["agents", "describe"])
    finally:
        os.chdir(prev)
        sys.modules.pop("agent", None)
    assert describe.exit_code == 0, describe.output
    assert "Answer general questions" in describe.output
