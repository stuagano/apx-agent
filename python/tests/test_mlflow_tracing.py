"""Tests for _mlflow_tracing.py — MLflow span emission across the compile path.

Verifies that:

  1. ``safe_span`` no-ops cleanly when MLflow is unavailable (won't break the
     compile path for users who skipped the ``eval`` extra).
  2. ``safe_span`` no-ops cleanly when ``mlflow.start_span`` raises (e.g.
     tracking misconfigured) — agent invocations never fail because of
     telemetry.
  3. ``ChatAgent.predict`` emits the AGENT-typed top-level span with the
     expected attributes (model, message_count, user_scoped, streaming).
  4. ``ChatAgent.predict`` also emits inner ``compile_to_langgraph`` and
     ``graph.invoke`` CHAIN spans.
  5. The ``/invocations`` route emits a CHAIN-typed request span and bridges
     OBO into ``apx.user_scoped`` on the span.
  6. ``APX_AGENT_MLFLOW_AUTOLOG=1`` triggers ``mlflow.langchain.autolog()`` on
     ``create_app`` startup; default doesn't.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

from fastapi.testclient import TestClient  # noqa: E402

from apx_agent import (  # noqa: E402
    LlmAgent,
    AgentConfig,
    create_app,
    safe_span,
)


# ---------------------------------------------------------------------------
# safe_span unit tests
# ---------------------------------------------------------------------------


class TestSafeSpan:
    def test_yields_none_when_mlflow_missing(self) -> None:
        """If MLflow can't be imported, safe_span just runs the block and
        yields None — never breaks the calling code."""
        with patch("apx_agent._mlflow_tracing.is_mlflow_available", return_value=False):
            entered = False
            with safe_span("test", span_type="AGENT") as span:
                entered = True
                assert span is None
            assert entered

    def test_yields_none_when_start_span_raises(self) -> None:
        """If mlflow.start_span throws (misconfigured tracking, etc.), the
        block still runs — telemetry must not break the request."""
        import mlflow

        # Make start_span raise; safe_span should swallow it.
        with patch.object(mlflow, "start_span", side_effect=RuntimeError("no tracking uri")):
            entered = False
            with safe_span("test", span_type="AGENT") as span:
                entered = True
                assert span is None
            assert entered

    def test_passes_through_to_mlflow_when_available(self) -> None:
        """Happy path: mlflow.start_span is called with the right name + type."""
        import mlflow

        mock_span = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_span)
        cm.__exit__ = MagicMock(return_value=None)

        with patch.object(mlflow, "start_span", return_value=cm) as mock_start:
            with safe_span(
                "my-span",
                span_type="AGENT",
                inputs={"k": "v"},
                attributes={"apx.foo": "bar"},
            ):
                pass

            mock_start.assert_called_once_with(name="my-span", span_type="AGENT")
            mock_span.set_inputs.assert_called_once_with({"k": "v"})
            mock_span.set_attribute.assert_any_call("apx.foo", "bar")


# ---------------------------------------------------------------------------
# set_trace_tags unit tests (issue #404 — version-correlation trace tags)
# ---------------------------------------------------------------------------


class TestSetTraceTags:
    def test_no_op_when_mlflow_missing(self) -> None:
        from apx_agent._mlflow_tracing import set_trace_tags

        with patch("apx_agent._mlflow_tracing.is_mlflow_available", return_value=False):
            set_trace_tags({"apx.git_sha": "abc"})  # must not raise

    def test_no_op_for_empty_tags(self) -> None:
        import mlflow
        from apx_agent._mlflow_tracing import set_trace_tags

        with patch.object(mlflow, "update_current_trace") as mock_update:
            set_trace_tags({})
            mock_update.assert_not_called()

    def test_calls_update_current_trace(self) -> None:
        import mlflow
        from apx_agent._mlflow_tracing import set_trace_tags

        with patch.object(mlflow, "update_current_trace") as mock_update:
            set_trace_tags({"apx.git_sha": "abc", "apx.model_version": "3"})
            mock_update.assert_called_once_with(
                tags={"apx.git_sha": "abc", "apx.model_version": "3"}
            )

    def test_swallows_update_failure(self) -> None:
        """No active trace / tagging failure must never fail the request."""
        import mlflow
        from apx_agent._mlflow_tracing import set_trace_tags

        with patch.object(
            mlflow, "update_current_trace", side_effect=RuntimeError("no trace"),
        ):
            set_trace_tags({"apx.git_sha": "abc"})  # must not raise


# ---------------------------------------------------------------------------
# ChatAgent.predict span emission
# ---------------------------------------------------------------------------


class TestChatAgentSpans:
    def test_predict_emits_agent_span_with_attributes(self) -> None:
        from apx_agent import chat_agent_for
        from mlflow.types.agent import ChatAgentMessage

        agent = LlmAgent(tools=[])
        wrapped = chat_agent_for(agent, model="databricks-claude-sonnet-4-6")

        # Replace compile_to_langgraph so we don't actually compile.
        fake_graph = MagicMock()
        from langchain_core.messages import AIMessage

        def _fake_invoke(state):
            return {"messages": [*state["messages"], AIMessage(content="ok")]}

        fake_graph.invoke.side_effect = _fake_invoke

        # Capture every start_span call.
        observed: list[dict[str, Any]] = []

        def _spy_start_span(name, span_type="UNKNOWN", **kwargs):
            observed.append({"name": name, "span_type": span_type})
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=MagicMock())
            cm.__exit__ = MagicMock(return_value=None)
            return cm

        import mlflow

        with patch("apx_agent._chat_agent.compile_to_langgraph", return_value=fake_graph), patch(
            "apx_agent._defaults._make_workspace_client", return_value=MagicMock()
        ), patch.object(mlflow, "start_span", side_effect=_spy_start_span):
            wrapped.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs={"user_token": "tok"},
            )

        span_names = {s["name"] for s in observed}
        span_types = {s["span_type"] for s in observed}

        assert "ApxChatAgent.predict" in span_names
        assert "graph.invoke" in span_names
        assert "AGENT" in span_types
        assert "CHAIN" in span_types

    def test_predict_stamps_version_correlation_from_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With APX_GIT_SHA in the env (injected at deploy — issue #404), the
        root predict span gets the apx.git_sha attribute and the trace gets
        the matching trace tag so `canary analyze` can partition on it."""
        from apx_agent import chat_agent_for
        from mlflow.types.agent import ChatAgentMessage

        monkeypatch.delenv("APX_MODEL_VERSION", raising=False)
        monkeypatch.setenv("APX_GIT_SHA", "d" * 40)

        agent = LlmAgent(tools=[])
        wrapped = chat_agent_for(agent, model="databricks-claude-sonnet-4-6")

        fake_graph = MagicMock()
        from langchain_core.messages import AIMessage

        def _fake_invoke(state):
            return {"messages": [*state["messages"], AIMessage(content="ok")]}

        fake_graph.invoke.side_effect = _fake_invoke

        observed_attrs: list[tuple[str, Any]] = []

        def _spy_start_span(name, span_type="UNKNOWN", **kwargs):
            mock_span = MagicMock()
            mock_span.set_attribute.side_effect = (
                lambda k, v: observed_attrs.append((k, v))
            )
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=mock_span)
            cm.__exit__ = MagicMock(return_value=None)
            return cm

        import mlflow

        with patch("apx_agent._chat_agent.compile_to_langgraph", return_value=fake_graph), patch(
            "apx_agent._defaults._make_workspace_client", return_value=MagicMock()
        ), patch.object(mlflow, "start_span", side_effect=_spy_start_span), patch.object(
            mlflow, "update_current_trace"
        ) as mock_update:
            wrapped.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs={"user_token": "tok"},
            )

        assert ("apx.git_sha", "d" * 40) in observed_attrs
        mock_update.assert_called_once_with(tags={"apx.git_sha": "d" * 40})


# ---------------------------------------------------------------------------
# /invocations route span emission
# ---------------------------------------------------------------------------


class TestInvocationsRouteSpans:
    def test_route_emits_request_span_with_obo_attribute(self) -> None:
        """The /invocations handler wraps each request in a CHAIN span whose
        attributes reflect the OBO bridge state."""
        import mlflow
        from apx_agent import _chat_agent as _ca_module
        from mlflow.types.agent import ChatAgentResponse

        agent = LlmAgent(tools=[])
        config = AgentConfig(name="test-agent", model="databricks-claude-sonnet-4-6")

        captured_chat_agent: dict[str, Any] = {}
        original_factory = _ca_module.chat_agent_for

        def _spy_factory(agent_arg, *, model, conversation_store=None, agent_id=None, checkpointer=None):
            ca = original_factory(agent_arg, model=model)
            captured_chat_agent["ca"] = ca
            ca.predict = MagicMock(return_value=ChatAgentResponse(messages=[]))
            return ca

        # Capture start_span calls; record attributes from the span the route opens.
        observed_attrs: list[dict[str, Any]] = []
        observed_names: list[str] = []

        def _spy_start_span(name, span_type="UNKNOWN", **kwargs):
            observed_names.append(name)
            mock_span = MagicMock()

            def _set_attr(k, v):
                observed_attrs.append({"name": name, "key": k, "value": v})

            mock_span.set_attribute.side_effect = _set_attr
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=mock_span)
            cm.__exit__ = MagicMock(return_value=None)
            return cm

        with patch.object(_ca_module, "chat_agent_for", side_effect=_spy_factory), patch(
            "apx_agent._wiring._make_workspace_client", return_value=MagicMock()
        ), patch.object(mlflow, "start_span", side_effect=_spy_start_span):
            app = create_app(agent, config=config)
            with TestClient(app) as client:
                client.post(
                    "/invocations",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    headers={"X-Forwarded-Access-Token": "obo-token"},
                )

        # The route's span fires.
        assert "POST /invocations" in observed_names

        # Attributes on the request-level span reflect OBO bridge state.
        route_attrs = {
            a["key"]: a["value"]
            for a in observed_attrs
            if a["name"] == "POST /invocations"
        }
        assert route_attrs["apx.user_scoped"] is True
        assert route_attrs["apx.agent_name"] == "test-agent"
        assert route_attrs["http.route"] == "/invocations"


# ---------------------------------------------------------------------------
# Env-var autolog opt-in
# ---------------------------------------------------------------------------


class TestAutologEnvVar:
    def test_autolog_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APX_AGENT_MLFLOW_AUTOLOG", raising=False)
        with patch("mlflow.langchain.autolog") as mock_autolog:
            from apx_agent._mlflow_tracing import autolog_if_env

            autolog_if_env()
            mock_autolog.assert_not_called()

    def test_autolog_on_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APX_AGENT_MLFLOW_AUTOLOG", "1")
        with patch("mlflow.langchain.autolog") as mock_autolog:
            from apx_agent._mlflow_tracing import autolog_if_env

            autolog_if_env()
            mock_autolog.assert_called_once()


# ---------------------------------------------------------------------------
# emit_progress unit tests
# ---------------------------------------------------------------------------


class TestEmitProgress:
    @pytest.fixture(autouse=True)
    def _isolate_tracing(self, tmp_path):
        """De-flake: give each test a fully clean, valid MLflow tracing
        destination so ``start_span`` always yields a real recording span.

        In the full suite, prior tests can leave tracing in a broken global
        state — a tracking URI pointing at a since-deleted temp dir, and/or
        tracing disabled (autolog / repeated trace-export failures act as a
        circuit-breaker). Either way ``start_span`` returns a NonRecordingSpan,
        ``emit_progress`` finds no active span and no-ops, and the asserted
        event is never recorded — so this test failed depending on suite order.

        Fresh ``file://`` store + experiment + ``tracing.enable()`` guarantees a
        LiveSpan (verified against the worst case: deleted-dir URI + disabled).
        The original tracking URI is restored after so THIS test's soon-deleted
        ``tmp_path`` doesn't become the same pollution for whatever runs next.
        """
        import mlflow

        old_uri = mlflow.get_tracking_uri()
        mlflow.set_tracking_uri(f"file://{tmp_path}")
        try:
            mlflow.set_experiment("apx-emit-progress-test")
            mlflow.tracing.enable()
        except Exception:
            pass
        try:
            yield
        finally:
            mlflow.set_tracking_uri(old_uri)

    def test_emit_progress_noop_without_active_span(self) -> None:
        from apx_agent._mlflow_tracing import emit_progress

        # No active span / tracking — must not raise.
        emit_progress("hello", warehouse_id="wh-1")

    def test_emit_progress_adds_event_to_active_span(self) -> None:
        import mlflow

        from apx_agent._mlflow_tracing import emit_progress

        with mlflow.start_span(name="parent") as span:
            emit_progress("Starting SQL warehouse", warehouse_id="wh-1")
        # The span recorded an event named for apx progress carrying the message.
        # The fixture guarantees tracing is on, so `span` is a real LiveSpan with
        # `.events` — direct attribute access (no defensive getattr needed).
        events = span.events or []
        assert any(
            e.name == "apx.progress"
            and (e.attributes or {}).get("message") == "Starting SQL warehouse"
            for e in events
        )
