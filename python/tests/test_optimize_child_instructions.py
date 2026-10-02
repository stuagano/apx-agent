"""Child optimization must run candidates and preserve the served declaration."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from apx_agent import Agent, DataAgent, LoopAgent, RouterAgent, SequentialAgent, _compile, _eval
from apx_agent._dev import build_dev_ui_router


@pytest.mark.asyncio
async def test_child_candidates_execute_without_mutating_live_agent_or_source(tmp_path, monkeypatch):
    source = ('from apx_agent import Agent, SequentialAgent\n'
              'first = Agent(instructions="FIRST")\n'
              'second = Agent(instructions="SECOND é")\n'
              'agent = SequentialAgent([first, second])\n')
    path = tmp_path / "agent.py"
    path.write_text(source)
    first, second = Agent(instructions="FIRST"), Agent(instructions="SECOND é")
    root = SequentialAgent([first, second])
    module = ModuleType("agent")
    module.__file__ = str(path)
    module.first, module.second, module.agent = first, second, root
    monkeypatch.setitem(sys.modules, "agent", module)
    app = FastAPI()
    app.include_router(build_dev_ui_router())
    app.state.agent_context = SimpleNamespace(agent=root, config=SimpleNamespace(name="example", model="fake", instructions=""))
    app.state.workspace_client = None
    monkeypatch.setattr("apx_agent._dev._load_optimize_eval_rows", lambda: [{"inputs": {"question": "go"}}])
    monkeypatch.setattr("mlflow.genai.scorers.get_scorer", lambda **kwargs: MagicMock())
    monkeypatch.setattr("mlflow.genai.optimize.GepaPromptOptimizer", MagicMock())
    register = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(name=kwargs["name"], version=1))
    monkeypatch.setattr("mlflow.genai.register_prompt", register)
    client = MagicMock()
    monkeypatch.setattr("mlflow.MlflowClient", lambda: client)
    prompt = SimpleNamespace(template="CANDIDATE A")
    monkeypatch.setattr("mlflow.genai.load_prompt", lambda uri: prompt)
    # Keep the real compiler/prediction path, but never resolve ambient SDK auth.
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", MagicMock())
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **k: GenericFakeChatModel(messages=iter([AIMessage(content="OK")])))
    compiled_instructions = []
    compile_agent = _eval.compile_to_chat_agent

    def compile_checked(agent, **kwargs):
        compiled_instructions.append([child._instructions for child in agent._agents])
        assert agent is not root
        assert [first._instructions, second._instructions] == ["FIRST", "SECOND é"]
        return compile_agent(agent, **kwargs)

    monkeypatch.setattr("apx_agent.compile_to_chat_agent", compile_checked)
    candidate = "Better $1, $$, $& and quotes 'é'"

    def optimize(**kwargs):
        assert kwargs["predict_fn"]({"question": "go"}) == "OK"
        prompt.template = candidate
        assert kwargs["predict_fn"]({"question": "go"}) == "OK"
        return SimpleNamespace(optimized_prompts=[prompt], initial_eval_score=0.3, final_eval_score=0.8)

    monkeypatch.setattr("mlflow.genai.optimize_prompts", optimize)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/_apx/edit/optimize-instructions", json={"judge_name": "judge", "node": "second", "source": source})
    assert response.status_code == 200, response.text
    assert compiled_instructions == [["FIRST", "CANDIDATE A"], ["FIRST", candidate]]
    assert response.json()["node"] == "second"
    from apx_agent._ui_edit import _parse_agent_nodes

    nodes = {node["name"]: node for node in _parse_agent_nodes(response.json()["source"])}
    assert nodes["first"]["instructions"] == "FIRST"
    assert nodes["second"]["instructions"] == candidate
    assert path.read_text() == source
    assert [first._instructions, second._instructions] == ["FIRST", "SECOND é"]
    client.delete_prompt.assert_called_once_with(register.call_args.kwargs["name"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        saved = await ac.post("/_apx/edit", json={"content": response.json()["source"]})
    assert saved.json()["ok"], saved.text
    assert path.read_text() == response.json()["source"]


@pytest.mark.asyncio
@pytest.mark.parametrize("composition", ["router", "loop", "changed-source"])
async def test_targeting_compositions_and_rejecting_stale_source(tmp_path, monkeypatch, composition):
    source = 'child = Agent(instructions="CHILD")\nagent = RouterAgent([child])\n'
    leaf = Agent(name="child", description="Child task", instructions="CHILD")
    child = leaf
    root = RouterAgent([child])
    if composition == "loop":
        source = 'child = LoopAgent(Agent(instructions="CHILD"))\nagent = SequentialAgent([child])\n'
        child = LoopAgent(leaf)
        root = SequentialAgent([child])
    path = tmp_path / "agent.py"
    path.write_text(source)
    module = ModuleType("agent")
    module.__file__ = str(path)
    module.child, module.agent = child, root
    monkeypatch.setitem(sys.modules, "agent", module)
    app = FastAPI()
    app.include_router(build_dev_ui_router())
    app.state.agent_context = SimpleNamespace(agent=root, config=SimpleNamespace(model="fake", instructions=""))
    monkeypatch.setattr("apx_agent._dev._load_optimize_eval_rows", lambda: [{"inputs": {"question": "go"}}])

    def optimize(**kwargs):
        from apx_agent._dev import _instruction_candidate_agent

        assert kwargs["target"] is leaf
        assert kwargs["current_instructions"] == "CHILD"
        trial = _instruction_candidate_agent(root, kwargs["target"], "CANDIDATE")
        selected = trial._agents[0]._inner if composition == "loop" else trial._routes[0][2]
        assert selected._instructions == "CANDIDATE"
        assert leaf._instructions == "CHILD"
        if composition == "loop":
            assert _instruction_candidate_agent(child, child, "ROOT CANDIDATE")._inner._instructions == "ROOT CANDIDATE"
        if composition == "changed-source":
            path.write_text(source + "# concurrent edit\n")
        return {"ok": True, "status": 200, "candidate": "CANDIDATE", "scores": {"before": 0, "after": 1}}

    monkeypatch.setattr("apx_agent._dev._optimize_instructions_sync", optimize)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        result = await ac.post("/_apx/edit/optimize-instructions", json={"node": "child", "judge_name": "judge", "source": source})
    assert result.status_code == (409 if composition == "changed-source" else 200), result.text
    assert path.read_text() == (source + "# concurrent edit\n" if composition == "changed-source" else source)


@pytest.mark.asyncio
@pytest.mark.parametrize("case,status", [("unknown", 422), ("unreachable", 422), ("composition", 422),
                                        ("invalid-node", 422), ("unsaved", 409), ("expression", 422),
                                        ("generated", 422), ("empty-generated", 422), ("remote", 422), ("remote-loop", 422),
                                        ("saved-composition", 409)])
async def test_invalid_target_is_rejected_before_optimization(tmp_path, monkeypatch, case, status):
    source = 'child = Agent(instructions="child")\nagent = SequentialAgent([child])\n'
    if case == "expression":
        source = 'child = Agent(instructions=PROMPT)\nagent = SequentialAgent([child])\n'
    elif case == "generated":
        source = 'child = DataAgent("main", "sales")\nagent = SequentialAgent([child])\n'
    elif case == "empty-generated":
        source = 'child = DataAgent("main", "sales", instructions="")\nagent = SequentialAgent([child])\n'
    elif case == "remote-loop":
        source = 'child = LoopAgent(Agent(instructions="child"))\nagent = SequentialAgent([child])\n'
    path = tmp_path / "agent.py"
    path.write_text(source)
    child = DataAgent("main", "sales", include_functions=False, instructions="" if case == "empty-generated" else None) if case in ("generated", "empty-generated") else Agent(name="child", instructions="child")
    original_instructions = child._instructions
    assert original_instructions
    if case == "remote-loop":
        child = LoopAgent(child)
    module = ModuleType("agent")
    module.__file__ = str(path)
    module.child = child
    module.agent = SequentialAgent([Agent(instructions="other")] if case == "unreachable" else [child])
    if case in ("remote", "remote-loop"):
        module.agent._apx_remote_leaf_bindings = {"child": SimpleNamespace(endpoint="remote")}
    monkeypatch.setitem(sys.modules, "agent", module)
    app = FastAPI()
    app.include_router(build_dev_ui_router())
    app.state.agent_context = SimpleNamespace(agent=module.agent, config=SimpleNamespace(instructions=""))
    if case == "saved-composition":
        # Saving changes the file, but does not reload the served declaration.
        source = source.replace("SequentialAgent([child])", "SequentialAgent([child, child])")
        path.write_text(source)
    monkeypatch.setattr("apx_agent._dev._load_optimize_eval_rows", lambda: [{"inputs": {"question": "go"}}])
    optimize = MagicMock(side_effect=AssertionError("Invalid input must not start optimization"))
    monkeypatch.setattr("apx_agent._dev._optimize_instructions_sync", optimize)
    node = {"unknown": "missing", "composition": "agent", "invalid-node": []}.get(case, "child")
    body = {"node": node, "judge_name": "judge", "source": source + "# unsaved" if case == "unsaved" else source}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        result = await ac.post("/_apx/edit/optimize-instructions", json=body)
    assert result.status_code == status, result.text
    assert not result.json()["ok"]
    optimize.assert_not_called()
    assert path.read_text() == source
    assert (child._inner if case == "remote-loop" else child)._instructions == original_instructions
