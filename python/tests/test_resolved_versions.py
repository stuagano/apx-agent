"""Resolved artifact versions on traces (#1247): one registry, every AGENT span, upstream links."""

from __future__ import annotations

import contextvars
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apx_agent._agents import LlmAgent
from apx_agent._prompt_pins import apply_pinned_prompts
from apx_agent._resolved_versions import (
    ResolvedVersion,
    clear_resolved,
    record_resolved,
    resolved_versions,
    stamp_resolved_versions,
    upstream_trace_tags,
    version_tags,
)

_TOML = """\
schema_version = 1

[agent]
framework = "langgraph"
server = "agentbricks"

[experimental.prompts]
writer = "main.prompts.writer" # prompts:/main.prompts.writer/7
"""


class _Version:
    def __init__(self, uri: str) -> None:
        self.template = f"text for {uri}"
        self.version = int(uri.rsplit("/", 1)[-1])


@pytest.fixture(autouse=True)
def _clear():
    clear_resolved()
    yield
    clear_resolved()


def _prompt(key: str, version: int) -> ResolvedVersion:
    name = f"main.prompts.{key}"
    return ResolvedVersion(kind="prompt", key=key, name=name, uri=f"prompts:/{name}/{version}", version=str(version))


def _pin(root: Path) -> None:
    (root / "agent.toml").write_text(_TOML, encoding="utf-8")
    apply_pinned_prompts(LlmAgent(instructions="local"), root, loader=_Version)


def test_record_replaces_a_kind_and_empty_clears_it() -> None:
    record_resolved("prompt", [_prompt("b", 2), _prompt("a", 1)])
    assert [version.key for version in resolved_versions()] == ["a", "b"]
    record_resolved("prompt", [_prompt("c", 3)])
    assert [version.key for version in resolved_versions()] == ["c"]
    record_resolved("prompt", [])
    assert resolved_versions() == []


def test_record_rejects_a_kind_without_tag_keys() -> None:
    with pytest.raises(ValueError, match="model"):
        record_resolved("model", [])
    with pytest.raises(ValueError, match="kind 'model'"):
        record_resolved("prompt", [ResolvedVersion("model", "m", "main.models.m", "models:/main.models.m/3", "3")])


def test_a_new_kind_is_tagged_like_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extension point: one ``_TAG_KEYS`` row and a kind lands on traces the same way."""
    pytest.importorskip("mlflow")
    from apx_agent import _resolved_versions

    monkeypatch.setitem(_resolved_versions._TAG_KEYS, "model", ("apx.m.key", "apx.m.uri", "apx.m.version"))
    record_resolved("prompt", [_prompt("writer", 7)])
    record_resolved("model", [ResolvedVersion("model", "llm", "main.models.llm", "models:/main.models.llm/3", "3")])
    assert version_tags(resolved_versions()) == {
        "apx.m.key": "llm",
        "apx.m.uri": "models:/main.models.llm/3",
        "apx.m.version": "3",
        "apx.prompt.key": "writer",
        "apx.prompt.uri": "prompts:/main.prompts.writer/7",
        "apx.prompt.version": "7",
    }
    # Only prompts have an upstream MLflow trace link today.
    assert set(upstream_trace_tags(resolved_versions())) == {"mlflow.linkedPrompts"}


def test_upstream_link_is_mlflows_own_format() -> None:
    pytest.importorskip("mlflow")
    from mlflow.tracing.constant import TraceTagKey

    tags = upstream_trace_tags([_prompt("b", 2), _prompt("a", 1)])
    assert TraceTagKey.LINKED_PROMPTS == "mlflow.linkedPrompts"
    assert json.loads(tags[TraceTagKey.LINKED_PROMPTS]) == [
        {"name": "main.prompts.b", "version": "2"},
        {"name": "main.prompts.a", "version": "1"},
    ]


def test_nothing_recorded_stamps_nothing() -> None:
    span = MagicMock(parent_id=None)
    with patch("apx_agent._mlflow_tracing.set_trace_tags") as tags:
        stamp_resolved_versions(span)
    tags.assert_not_called()
    span.set_attribute.assert_not_called()


def test_root_span_adds_the_upstream_link_and_nested_span_does_not(tmp_path: Path) -> None:
    pytest.importorskip("mlflow")
    _pin(tmp_path)
    root, nested = MagicMock(parent_id=None), MagicMock(parent_id="abc")
    with patch("apx_agent._mlflow_tracing.set_trace_tags") as tags:
        stamp_resolved_versions(root)
        stamp_resolved_versions(nested)
    root_tags, nested_tags = (call.args[0] for call in tags.call_args_list)
    assert json.loads(root_tags["mlflow.linkedPrompts"]) == [{"name": "main.prompts.writer", "version": "7"}]
    assert "mlflow.linkedPrompts" not in nested_tags
    assert nested_tags["apx.prompt.version"] == root_tags["apx.prompt.version"] == "7"


def test_startup_in_another_context_still_reaches_request_traces(tmp_path: Path) -> None:
    """Startup runs in the server lifespan; requests run in other tasks and threads."""
    (tmp_path / "agent.toml").write_text(_TOML, encoding="utf-8")
    agent = LlmAgent(instructions="local")
    contextvars.Context().run(apply_pinned_prompts, agent, tmp_path, loader=_Version)
    seen: list[list[ResolvedVersion]] = []
    worker = threading.Thread(target=lambda: seen.append(resolved_versions()))
    worker.start()
    worker.join()
    assert [version.uri for version in seen[0]] == ["prompts:/main.prompts.writer/7"]


def test_an_agent_without_pins_clears_earlier_prompt_tags(tmp_path: Path) -> None:
    _pin(tmp_path)
    bare = tmp_path / "bare"
    bare.mkdir()
    apply_pinned_prompts(LlmAgent(instructions="other"), bare)
    assert resolved_versions() == []


def test_a_real_agent_trace_carries_the_pinned_versions(tmp_path: Path) -> None:
    """Read the exported trace back: the tags are on the stored trace, not only on a mock."""
    mlflow = pytest.importorskip("mlflow")
    from apx_agent._mlflow_tracing import safe_span

    _pin(tmp_path)
    mlflow.set_experiment(f"resolved-versions-{tmp_path.name}")
    with safe_span("turn", span_type="AGENT"):
        with safe_span("inner", span_type="AGENT"):
            pass
    trace_id = mlflow.get_last_active_trace_id()
    assert trace_id is not None
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(trace_id)
    assert trace is not None
    assert trace.info.tags["apx.prompt.uri"] == "prompts:/main.prompts.writer/7"
    assert trace.info.tags["apx.prompt.version"] == "7"
    assert json.loads(trace.info.tags["mlflow.linkedPrompts"]) == [{"name": "main.prompts.writer", "version": "7"}]
    root = next(span for span in trace.data.spans if span.parent_id is None)
    assert root.attributes["apx.prompt.key"] == "writer"
