"""AC-6 reality guard: the optimize-instructions route writes NO source.

A route that returns ``{ok:true}`` while quietly splicing the candidate into
``agent.py`` would pass a plain status-code test but violate the whole design
(landing goes only through the explicit Save path). This proves the on-disk
agent source is byte-identical after a mocked successful optimize, the ctk way:
read the bytes back and assert the artifact still carries the *original*
instructions and never the candidate.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ctk import verify
from ctk.verify import Artifact

from apx_agent._dev import build_dev_ui_router

_AGENT_SRC = '''from apx_agent import Agent

agent = Agent(
    name="demo",
    instructions="ORIGINAL_INSTRUCTIONS_DO_NOT_CHANGE",
)
'''


@pytest.mark.asyncio
async def test_optimize_route_does_not_write_source(tmp_path: Path):
    agent_file = tmp_path / "agent_router.py"
    agent_file.write_text(_AGENT_SRC)
    before = agent_file.read_bytes()

    app = FastAPI()
    app.include_router(build_dev_ui_router())
    app.state.agent_context = None

    optimize = MagicMock(
        return_value=SimpleNamespace(
            optimized_prompts=[SimpleNamespace(template="CANDIDATE_NEW_INSTRUCTIONS")],
            initial_eval_score=0.4,
            final_eval_score=0.8,
        )
    )
    with patch("apx_agent._ui_edit._find_agent_router_path", return_value=agent_file), patch(
        "apx_agent._dev._load_optimize_eval_rows", return_value=[{"question": "q"}]
    ), patch.multiple(
        "mlflow.genai", optimize_prompts=optimize, register_prompt=MagicMock(return_value=SimpleNamespace(name="p", version=1))
    ), patch("mlflow.genai.scorers.get_scorer", MagicMock(return_value=MagicMock())), patch(
        "mlflow.genai.optimize.GepaPromptOptimizer", MagicMock()
    ), patch("mlflow.MlflowClient", MagicMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/edit/optimize-instructions", json={"judge_name": "j"})

    assert r.status_code == 200, r.json()
    assert r.json()["candidate"] == "CANDIDATE_NEW_INSTRUCTIONS"

    # Read-after-write: the source is untouched and never gained the candidate.
    assert agent_file.read_bytes() == before, "optimize route must not write agent source"
    verify(
        Artifact(
            str(agent_file),
            min_bytes=40,
            must_contain="ORIGINAL_INSTRUCTIONS_DO_NOT_CHANGE",
            must_not_contain="CANDIDATE_NEW_INSTRUCTIONS",
        )
    )
