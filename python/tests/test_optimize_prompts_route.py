"""Gate tests for POST /_apx/edit/optimize-instructions (the 'Improve instructions' button).

optimize_prompts (GEPA) is always mocked — only the plumbing and failure paths
are gated (GEPA quality is stochastic and not unit-testable). See the PRD.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent._dev import build_dev_ui_router


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(build_dev_ui_router())
    a.state.agent_context = None  # route reads request.app.state.agent_context
    return a


def _mlflow_mocks(*, optimize: MagicMock, get_scorer: MagicMock | None = None):
    """Patch every mlflow symbol _optimize_instructions_sync imports at call time.

    Returns the register_prompt and MlflowClient() spies so tests can assert the
    transient prompt is created then cleaned up.
    """
    register = MagicMock(return_value=SimpleNamespace(name="apx_optimize_x", version=1))
    client_instance = MagicMock()
    scorer = get_scorer or MagicMock(return_value=MagicMock())
    return patch.multiple(
        "mlflow.genai",
        optimize_prompts=optimize,
        register_prompt=register,
    ), patch("mlflow.genai.scorers.get_scorer", scorer), patch(
        "mlflow.genai.optimize.GepaPromptOptimizer", MagicMock()
    ), patch("mlflow.MlflowClient", MagicMock(return_value=client_instance)), register, client_instance


class _Resp(NamedTuple):
    status: int
    body: dict


async def _post(app: FastAPI, body: dict) -> _Resp:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/_apx/edit/optimize-instructions", json=body)
    return _Resp(r.status_code, r.json())


@pytest.mark.asyncio
async def test_optimize_route_returns_candidate_and_scores(app: FastAPI):
    """AC-1: registered route returns {ok, candidate, scores:{before,after}}."""
    optimize = MagicMock(
        return_value=SimpleNamespace(
            optimized_prompts=[SimpleNamespace(template="BETTER INSTRUCTIONS")],
            initial_eval_score=0.5,
            final_eval_score=0.9,
        )
    )
    p_multi, p_scorer, p_gepa, p_client, register, client = _mlflow_mocks(optimize=optimize)
    with patch("apx_agent._dev._load_optimize_eval_rows", return_value=[{"question": "q"}]), patch(
        "apx_agent._dev._current_root_instructions", return_value="old instructions"
    ), p_multi, p_scorer, p_gepa, p_client:
        status, body = await _post(app, {"judge_name": "my_judge"})
    assert status == 200, body
    assert body["ok"] is True
    assert body["candidate"] == "BETTER INSTRUCTIONS"
    assert body["scores"] == {"before": 0.5, "after": 0.9}
    assert "status" not in body  # internal key stripped before responding
    optimize.assert_called_once()


@pytest.mark.asyncio
async def test_missing_judge_name_returns_422(app: FastAPI):
    """AC-2: empty/missing judge_name → 422 and optimize_prompts never called."""
    optimize = MagicMock()
    p_multi, p_scorer, p_gepa, p_client, register, client = _mlflow_mocks(optimize=optimize)
    with patch("apx_agent._dev._load_optimize_eval_rows", return_value=[{"question": "q"}]), p_multi, p_scorer, p_gepa, p_client:
        status, body = await _post(app, {"judge_name": "   "})
    assert status == 422
    assert body["ok"] is False
    optimize.assert_not_called()


@pytest.mark.asyncio
async def test_missing_eval_dataset_fails_clear(app: FastAPI):
    """AC-3: no eval dataset → clear error, optimize_prompts never called."""
    optimize = MagicMock()
    p_multi, p_scorer, p_gepa, p_client, register, client = _mlflow_mocks(optimize=optimize)
    with patch("apx_agent._dev._load_optimize_eval_rows", return_value=[]), p_multi, p_scorer, p_gepa, p_client:
        status, body = await _post(app, {"judge_name": "my_judge"})
    assert status == 422
    assert body["ok"] is False
    assert "eval dataset" in body["error"].lower()
    optimize.assert_not_called()


@pytest.mark.asyncio
async def test_missing_judge_fails_clear(app: FastAPI):
    """AC-4: unknown judge (get_scorer raises) → clear error, optimize never called."""
    optimize = MagicMock()
    get_scorer = MagicMock(side_effect=RuntimeError("no such scorer"))
    p_multi, p_scorer, p_gepa, p_client, register, client = _mlflow_mocks(optimize=optimize, get_scorer=get_scorer)
    with patch("apx_agent._dev._load_optimize_eval_rows", return_value=[{"question": "q"}]), patch(
        "apx_agent._dev._current_root_instructions", return_value="old"
    ), p_multi, p_scorer, p_gepa, p_client:
        status, body = await _post(app, {"judge_name": "ghost_judge"})
    assert status == 422
    assert body["ok"] is False
    assert "ghost_judge" in body["error"]
    optimize.assert_not_called()


@pytest.mark.asyncio
async def test_transient_prompt_registered_and_cleaned_up(app: FastAPI):
    """AC-5: transient prompt registered then deleted, even when optimize raises."""
    optimize = MagicMock(side_effect=RuntimeError("gepa blew up"))
    p_multi, p_scorer, p_gepa, p_client, register, client = _mlflow_mocks(optimize=optimize)
    with patch("apx_agent._dev._load_optimize_eval_rows", return_value=[{"question": "q"}]), patch(
        "apx_agent._dev._current_root_instructions", return_value="old"
    ), p_multi, p_scorer, p_gepa, p_client:
        status, body = await _post(app, {"judge_name": "my_judge"})
    register.assert_called_once()
    client.delete_prompt.assert_called_once()  # cleanup ran despite the raise
    assert status == 500
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_edit_ui_has_improve_button():
    """AC-7: rendered Edit-tab HTML has the button + a fetch to the route."""
    from apx_agent._ui_edit import _render_edit_ui

    html = _render_edit_ui('agent = Agent(instructions="hi")')
    assert 'id="btn-improve"' in html
    assert "/_apx/edit/optimize-instructions" in html
    # candidate loads into the editor (dispatch), not an auto-save (no POST /_apx/edit here)
    assert "view.dispatch" in html


@pytest.mark.parametrize("changed_during_request", [False, True])
def test_improve_selected_node_preserves_text_and_concurrent_edits(changed_during_request):
    """Execute the rendered JS: send the target, preserve $, and reject stale edits."""
    from apx_agent._ui_edit import _render_edit_ui

    source = 'second = Agent(instructions="old")\n'
    candidate_source = 'second = Agent(instructions="Charge $1 $$ $& $` é")\n'
    html = _render_edit_ui(source)
    function = re.search(r"async function improveInstructions\(\) \{[\s\S]*?\n\}\ndocument.getElementById\('btn-improve'\)", html)
    assert function is not None
    function_js = function.group(0).rsplit("\ndocument.getElementById", 1)[0]
    node = shutil.which("node")
    assert node is not None, "node is required to execute the editor workflow"
    script = f"""
let text = {json.dumps(source)};
const original = text, changed = {json.dumps(changed_during_request)};
const candidate = {json.dumps(candidate_source)};
const controls = {{'btn-improve': {{textContent:'Improve'}}, 'status-msg': {{}}}};
const document = {{getElementById: id => controls[id]}};
const optimizeNode = {{value: 'second'}};
const prompt = () => 'judge';
let request;
const window = {{apxDevFetch: async (url, options) => {{
  request = JSON.parse(options.body);
  if (changed) text = 'user edits';
  return {{json: async () => ({{ok:true, node:'second', source:candidate}})}};
}}}};
const view = {{state:{{doc:{{toString:()=>text}}}}, dispatch: event => {{text=event.changes.insert;}}}};
function schedulePreview() {{}}
{function_js}
improveInstructions().then(()=>process.stdout.write(JSON.stringify({{text,request,status:controls['status-msg']}})));
"""
    result = json.loads(subprocess.run([node, "-e", script], check=True, capture_output=True, text=True).stdout)
    assert result["request"] == {"judge_name": "judge", "node": "second", "source": source}
    assert result["text"] == ("user edits" if changed_during_request else candidate_source)
    assert result["status"]["className"] == ("err" if changed_during_request else "ok")
