"""Session state and halt statuses for durable composites (docs/design/composite-session-keys.md)."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict

from apx_agent import Agent, SequentialAgent, compile_to_langgraph
from apx_agent import _compile


def test_sequence_declares_session_keys_and_halt_statuses():
    seq = SequentialAgent([Agent(name="a")], session_keys=("intake",), halt_statuses=("needs_input",))
    assert seq._session_keys == ("intake",)
    assert seq._halt_statuses == frozenset({"needs_input"})
    assert SequentialAgent([Agent()])._session_keys == () and SequentialAgent([Agent()])._halt_statuses == frozenset()


@pytest.mark.parametrize("keys", [("user:pref",), ("app:flag",), ("",), ("a", "a")])
def test_session_keys_reject_scoped_blank_or_duplicate(keys):
    with pytest.raises(ValueError, match="session_keys"):
        SequentialAgent([Agent()], session_keys=keys)


def test_halt_statuses_reject_blank():
    with pytest.raises(ValueError, match="halt_statuses"):
        SequentialAgent([Agent()], halt_statuses=("",))


class Intake(BaseModel):
    status: str
    question: str | None = None


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _model(monkeypatch, replies):
    model = _Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)


def _intake_sequence(seen):
    return SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",)),
        Agent(name="downstream", before_agent_callback=seen.append),
    ], name="pipeline", halt_statuses=("needs_input",))


def test_halt_status_ends_the_sequence_with_the_steps_message(monkeypatch):
    seen = []
    _model(monkeypatch, [AIMessage(content='{"status": "needs_input", "question": "Which window?"}')])
    graph = compile_to_langgraph(_intake_sequence(seen), ws=MagicMock(), model="fake")
    result = graph.invoke({"messages": [HumanMessage(content="price the savings product")]})
    assert seen == []  # the downstream step never ran
    assert result["state"]["intake"] == {"status": "needs_input", "question": "Which window?"}
    assert "Which window?" in result["messages"][-1].text  # the halting step's message is the answer


def test_non_halt_status_continues(monkeypatch):
    seen = []
    _model(monkeypatch, [AIMessage(content='{"status": "ready"}'), AIMessage(content="done")])
    graph = compile_to_langgraph(_intake_sequence(seen), ws=MagicMock(), model="fake")
    result = graph.invoke({"messages": [HumanMessage(content="price the savings product")]})
    assert len(seen) == 1 and result["messages"][-1].content == "done"


class _FakeSessions:
    """Stands in for the SDK SessionStoreClient: sessions keyed by id, items appended in order."""

    def __init__(self):
        self.items: dict[str, list[Any]] = {}

    def get_session(self, *, session_id):
        from databricks.sdk.errors import NotFound
        if session_id not in self.items:
            raise NotFound("no session")
        return MagicMock(session_id=session_id)

    def create_session(self, *, actor_id, session_id=None, metadata=None):
        self.items[session_id] = []
        return MagicMock(session_id=session_id)

    def append_items(self, session, *, items):
        self.items[session.session_id].extend(items)

    def list_items(self, session, *, order_by=None):
        return iter(MagicMock(data=d) for d in self.items[session.session_id])

    def clear_items(self, session):
        self.items[session.session_id] = []


def test_snapshot_round_trip_keeps_only_listed_keys():
    from apx_agent._composite_session import load_session_state, save_session_state
    store = _FakeSessions()
    assert load_session_state(store, "t1", ("intake",)) == {}
    save_session_state(store, "t1", ("intake",), {"intake": {"slots": {"bank": "acme"}}, "market": {"rows": 3}})
    assert load_session_state(store, "t1", ("intake",)) == {"intake": {"slots": {"bank": "acme"}}}
    [item] = next(iter(store.items.values()))
    assert item == {"type": "apx_session_state", "state": {"intake": {"slots": {"bank": "acme"}}}}


def test_snapshots_compact_after_the_cap():
    from apx_agent._composite_session import MAX_SNAPSHOTS, load_session_state, save_session_state
    store = _FakeSessions()
    for n in range(MAX_SNAPSHOTS + 3):
        save_session_state(store, "t1", ("intake",), {"intake": {"n": n}})
    assert len(next(iter(store.items.values()))) <= MAX_SNAPSHOTS
    assert load_session_state(store, "t1", ("intake",)) == {"intake": {"n": MAX_SNAPSHOTS + 2}}


def test_threads_are_isolated():
    from apx_agent._composite_session import load_session_state, save_session_state
    store = _FakeSessions()
    save_session_state(store, "user-a", ("intake",), {"intake": {"who": "a"}})
    assert load_session_state(store, "user-b", ("intake",)) == {}


from tests.test_runtime_targets import execution  # noqa: E402,F401


def _store(monkeypatch):
    store = _FakeSessions()
    import apx_agent._durable_agent as durable
    monkeypatch.setattr(durable, "_session_state_client", lambda name, ws: store)
    return store


def _seeded(monkeypatch):
    """Records the state each durable turn's graph is seeded with."""
    import apx_agent._durable_agent as durable
    seeded, real = [], durable.run_graph_turn

    def spy(*args, **kwargs):
        seeded.append(dict(kwargs.get("domain_input") or {}))
        return real(*args, **kwargs)
    monkeypatch.setattr(durable, "run_graph_turn", spy)
    return seeded


def _host(tmp_path, monkeypatch, root, execution):
    from databricks_agentkit.runtime.auth import RequestAuthContext
    from apx_agent import compile_agent
    from tests.test_runtime_targets import _write_native_pyproject

    def user_client(auth, mode):  # session_keys require a request user (as in test_native_invocation_contract)
        user = MagicMock()
        user.current_user.me.return_value.id = auth.namespace("principal", "verified")
        return user
    monkeypatch.setattr(RequestAuthContext, "client_for", user_client)
    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    return compile_agent(root, target="durable_agent_server", model="fake", service_ws=execution.ws, session_store="s")


def _turn(client, body, principal="alice"):
    import time
    from tests.test_native_invocation_contract import headers
    r = client.post("/api/invocations", json=body, headers=headers(principal))
    if r.status_code >= 500:  # a synchronous invocation reports its failure on the POST
        return {"status": "failed"}
    assert r.status_code in (200, 202), r.text
    for _ in range(100):
        state = client.get(f"/api/invocations/{body['id']}", headers=headers(principal)).json()
        if state.get("status") in ("completed", "failed"):
            return state
        time.sleep(0.05)
    raise AssertionError(f"invocation did not finish: {state}")


def _msg(text, sid):
    import uuid
    return {"id": str(uuid.uuid4()), "session_id": sid, "input": {"messages": [{"role": "user", "content": text}]}}


def test_session_keys_persist_across_turns_and_nothing_else(execution, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import uuid
    store, seeded = _store(monkeypatch), _seeded(monkeypatch)
    root = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",)),
        Agent(name="market", output_key="market"),
    ], name="pipeline", session_keys=("intake",), halt_statuses=("needs_input",))
    _model(monkeypatch, [AIMessage(content='{"status": "ready"}'), AIMessage(content="m1"),
                         AIMessage(content='{"status": "needs_input", "question": "q"}')])
    app = _host(tmp_path, monkeypatch, root, execution)
    sid = str(uuid.uuid4())
    with TestClient(app) as client:
        assert _turn(client, _msg("turn one", sid))["status"] == "completed"
        assert _turn(client, _msg("turn two", sid))["status"] == "completed"
    assert "intake" not in seeded[0]                                        # turn 1 starts empty
    assert seeded[1]["intake"] == {"status": "ready"}                       # turn 2 sees turn 1's intake
    assert "market" not in seeded[1]                                        # non-listed key never persists
    assert all(set(i["state"]) <= {"intake"} for items in store.items.values() for i in items)


def test_failed_turn_saves_nothing(execution, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import uuid
    store = _store(monkeypatch)

    def boom(_messages):
        raise RuntimeError("downstream failed")
    root = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",)),
        Agent(name="market", before_agent_callback=boom),
    ], name="pipeline", session_keys=("intake",))
    _model(monkeypatch, [AIMessage(content='{"status": "ready"}')])
    app = _host(tmp_path, monkeypatch, root, execution)
    with TestClient(app) as client:
        assert _turn(client, _msg("x", str(uuid.uuid4())))["status"] == "failed"
    assert all(items == [] for items in store.items.values())


def test_users_do_not_share_session_state():
    from apx_agent._durable_agent import session_thread_config
    a = session_thread_config("pipeline", "same", "user-a")["configurable"]["thread_id"]
    b = session_thread_config("pipeline", "same", "user-b")["configurable"]["thread_id"]
    assert a != b


def test_composite_session_keys_require_a_store(execution, monkeypatch, tmp_path):
    from apx_agent import compile_agent
    from tests.test_runtime_targets import _write_native_pyproject
    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="session_keys.*session store"):
        compile_agent(SequentialAgent([Agent(name="a")], session_keys=("intake",)),
                      target="durable_agent_server", model="fake", service_ws=execution.ws)


def test_composite_still_rejects_a_checkpointer(execution):
    from langgraph.checkpoint.memory import InMemorySaver
    from apx_agent import compile_agent
    with pytest.raises((NotImplementedError, ValueError)):
        compile_agent(SequentialAgent([Agent(name="a")], session_keys=("k",)), target="durable_agent_server",
                      model="fake", service_ws=execution.ws, checkpointer=InMemorySaver())


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bank: str


def test_typed_part_skips_an_untyped_intake(execution, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from apx_agent._native_input import native_data_part
    import uuid
    _store(monkeypatch)
    seen = []
    root = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", before_agent_callback=seen.append),
        Agent(name="intelligence", input_schema=Request, input_key="request"),
    ], name="pipeline", session_keys=("intake",))
    _model(monkeypatch, [AIMessage(content="ok")])
    app = _host(tmp_path, monkeypatch, root, execution)
    part = native_data_part(Request, {"bank": "acme"})
    body = {"id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()), "input": {"messages": [], "apx_data_parts": [part]}}
    with TestClient(app) as client:
        assert _turn(client, body)["status"] == "completed"
    assert seen == []  # the untyped intake was skipped


def test_typed_root_sequence_is_unchanged():
    from apx_agent._durable_agent import typed_entry_step
    seq = SequentialAgent([Agent(name="intelligence", input_schema=Request, input_key="request"), Agent(name="b")])
    assert typed_entry_step(seq) is None  # first step already typed: normal entry


# --- final-review fixes -------------------------------------------------------------------


def test_compiles_on_the_serve_path_with_an_agent_config(execution, monkeypatch, tmp_path):
    # C1: _serve.create_app passes config= and session_store=$AGENT_SESSION_STORE.
    from apx_agent import AgentConfig, compile_agent
    from tests.test_runtime_targets import _write_native_pyproject
    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    _store(monkeypatch)
    root = SequentialAgent([Agent(name="a")], name="p", session_keys=("intake",))
    config = AgentConfig(name="p", model="fake", target="durable_agent_server")
    # A deployed App's AGENT_SESSION_STORE is the config's declared/derived managed store.
    compile_agent(root, config=config, target="durable_agent_server", model="fake",
                  service_ws=execution.ws, session_store=config.session.store_name)


def test_typed_input_wins_over_loaded_session_state(execution, monkeypatch, tmp_path):
    # I1: a typed caller's fresh part must not be overwritten by a persisted key of the same name.
    from fastapi.testclient import TestClient
    from apx_agent._native_input import native_data_part
    import uuid
    _store(monkeypatch)
    seeded = _seeded(monkeypatch)
    root = SequentialAgent([
        Agent(name="intake", output_key="request"),
        Agent(name="lookup", input_schema=Request, input_key="request"),
    ], name="pipeline", session_keys=("request",))
    _model(monkeypatch, [AIMessage(content="ok"), AIMessage(content="ok")])
    app = _host(tmp_path, monkeypatch, root, execution)
    sid = str(uuid.uuid4())
    with TestClient(app) as client:
        for bank in ("a", "b"):
            part = native_data_part(Request, {"bank": bank})
            assert _turn(client, {"id": str(uuid.uuid4()), "session_id": sid,
                                  "input": {"messages": [], "apx_data_parts": [part]}})["status"] == "completed"
    assert seeded[1]["request"] == {"bank": "b"}


def test_nested_halt_stops_the_outer_sequence(monkeypatch):
    # I2: spec — nested sequences halt the outer sequence too.
    seen = []
    _model(monkeypatch, [AIMessage(content='{"status": "needs_input", "question": "q"}')])
    inner = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",)),
        Agent(name="inner2", before_agent_callback=seen.append),
    ], name="inner", halt_statuses=("needs_input",))
    outer = SequentialAgent([inner, Agent(name="after", before_agent_callback=seen.append)], name="outer")
    compile_to_langgraph(outer, ws=MagicMock(), model="fake").invoke({"messages": [HumanMessage(content="x")]})
    assert seen == []


def test_snapshots_are_read_oldest_first():
    # I3: "latest" must not depend on the server's default item order.
    from apx_agent._composite_session import load_session_state
    store = _FakeSessions()
    orders = []
    real = store.list_items
    store.list_items = lambda session, *, order_by=None: (orders.append(order_by), real(session))[1]
    load_session_state(store, "t1", ("intake",))
    assert orders == ["create_time asc"]


def test_session_keys_require_user_identity(execution, monkeypatch, tmp_path):
    # I4: persisted user-entered state is per user; app-auth would key it by a caller-chosen session_id.
    _store(monkeypatch)
    root = SequentialAgent([Agent(name="a")], name="p", session_keys=("intake",))
    app = _host(tmp_path, monkeypatch, root, execution)
    assert app.auth_policy.requires_user is True


def test_app_store_wins_over_an_auto_derived_session_name(execution, monkeypatch, tmp_path):
    # On Apps, AGENT_SESSION_STORE is the bound store; a composite can't declare session=, so the
    # config's auto-derived default ("apx-<name>-sessions") must not conflict with it.
    from apx_agent import AgentConfig, compile_agent
    from tests.test_runtime_targets import _write_native_pyproject
    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    _store(monkeypatch)
    root = SequentialAgent([Agent(name="a")], name="p", session_keys=("intake",))
    config = AgentConfig(name="p", model="fake", target="durable_agent_server")
    assert config.session.store_name != "apx-the-app-sessions"
    compile_agent(root, config=config, target="durable_agent_server", model="fake",
                  service_ws=execution.ws, session_store="apx-the-app-sessions")


def test_an_explicit_session_still_conflicts_loudly():
    from apx_agent import Agent as A, AgentConfig
    from apx_agent._runtime_targets import inspect_target
    root = SequentialAgent([A(name="a")], name="p", session_keys=("intake",))
    config = AgentConfig(name="p", model="fake", target="durable_agent_server",
                         session={"type": "managed", "store_name": "apx-declared-sessions"})
    with pytest.raises(ValueError, match="conflicts"):
        inspect_target(root, config=config, target="durable_agent_server", session_store="apx-other-sessions")


def test_a_halting_step_can_name_its_human_message(monkeypatch):
    # The halting step's recorded output stays exact JSON; the person typing gets the question.
    seen = []
    _model(monkeypatch, [AIMessage(content='{"status": "needs_input", "question": "Which window?"}')])
    seq = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",),
              output_message_path=("question",)),
        Agent(name="downstream", before_agent_callback=seen.append),
    ], name="pipeline", halt_statuses=("needs_input",))
    result = compile_to_langgraph(seq, ws=MagicMock(), model="fake").invoke(
        {"messages": [HumanMessage(content="price the savings product")]})
    assert seen == [] and result["state"]["intake"]["question"] == "Which window?"
    last = result["messages"][-1]
    assert last.content == "Which window?" and last.additional_kwargs["apx_answer"] is True


def test_a_non_halting_step_adds_no_message(monkeypatch):
    seen = []
    _model(monkeypatch, [AIMessage(content='{"status": "ready", "question": "unused"}'), AIMessage(content="done")])
    seq = SequentialAgent([
        Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",),
              output_message_path=("question",)),
        Agent(name="downstream", before_agent_callback=seen.append),
    ], name="pipeline", halt_statuses=("needs_input",))
    result = compile_to_langgraph(seq, ws=MagicMock(), model="fake").invoke(
        {"messages": [HumanMessage(content="price the savings product")]})
    assert len(seen) == 1 and result["messages"][-1].content == "done"
    assert not any(m.additional_kwargs.get("apx_answer") for m in result["messages"])


@pytest.mark.parametrize("path", [("",), ("a-b",), (), "question"])
def test_output_message_path_is_validated_like_the_status_path(path):
    with pytest.raises(ValueError, match="output_message_path"):
        Agent(name="intake", output_schema=Intake, output_message_path=path)
    with pytest.raises(ValueError, match="output_message_path"):
        Agent(name="intake", output_message_path=("question",))  # needs an output_schema


def _messaged_intake() -> Any:
    return Agent(name="intake", output_schema=Intake, output_key="intake", output_status_path=("status",),
                 output_message_path=("question",))


def test_a_halting_last_step_still_names_its_message(monkeypatch):
    _model(monkeypatch, [AIMessage(content="first done"),
                         AIMessage(content='{"status": "needs_input", "question": "Which window?"}')])
    seq = SequentialAgent([Agent(name="first"), _messaged_intake()], name="pipeline", halt_statuses=("needs_input",))
    result = compile_to_langgraph(seq, ws=MagicMock(), model="fake").invoke({"messages": [HumanMessage(content="go")]})
    assert result["messages"][-1].content == "Which window?"
    assert result["messages"][-1].additional_kwargs["apx_answer"] is True


def test_the_halt_message_keeps_the_steps_typed_output(monkeypatch):
    # Responses/A2A callers read DataParts from the final assistant message; the halt must not hide them.
    from apx_agent._a2a_models import output_data_parts

    _model(monkeypatch, [AIMessage(content='{"status": "needs_input", "question": "Which window?"}')])
    plain = compile_to_langgraph(_intake_sequence([]), ws=MagicMock(), model="fake").invoke(
        {"messages": [HumanMessage(content="go")]})
    _model(monkeypatch, [AIMessage(content='{"status": "needs_input", "question": "Which window?"}')])
    seq = SequentialAgent([_messaged_intake(), Agent(name="downstream")], name="pipeline",
                          halt_statuses=("needs_input",))
    halted = compile_to_langgraph(seq, ws=MagicMock(), model="fake").invoke({"messages": [HumanMessage(content="go")]})
    assert output_data_parts(halted["messages"]) == output_data_parts(plain["messages"])


class Summary(BaseModel):
    text: str


def test_a_finishing_last_step_names_the_answer(monkeypatch):
    # The last step's declared message is the answer a person reads, not every step's JSON.
    _model(monkeypatch, [AIMessage(content="first done"), AIMessage(content='{"text": "Hold at 4.80%."}')])
    seq = SequentialAgent([Agent(name="first"), Agent(name="summary", output_schema=Summary, output_key="summary",
                                                      output_message_path=("text",))], name="pipeline")
    result = compile_to_langgraph(seq, ws=MagicMock(), model="fake").invoke({"messages": [HumanMessage(content="go")]})
    assert result["messages"][-1].content == "Hold at 4.80%."
    assert result["messages"][-1].additional_kwargs["apx_answer"] is True


def test_a_message_path_that_is_never_shown_is_refused():
    seq = SequentialAgent([Agent(name="summary", output_schema=Summary, output_key="summary", output_message_path=("text",)),
                           Agent(name="after")], name="pipeline")
    with pytest.raises(ValueError, match="output_message_path"):
        compile_to_langgraph(seq, ws=MagicMock(), model="fake")


def test_an_empty_question_adds_no_halt_message(monkeypatch):
    _model(monkeypatch, [AIMessage(content='{"status": "needs_input", "question": null}')])
    seq = SequentialAgent([_messaged_intake(), Agent(name="downstream")], name="pipeline",
                          halt_statuses=("needs_input",))
    result = compile_to_langgraph(seq, ws=MagicMock(), model="fake").invoke({"messages": [HumanMessage(content="go")]})
    assert not any(m.additional_kwargs.get("apx_answer") for m in result["messages"])
