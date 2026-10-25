"""Declared session extraction through the real AgentKit client."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.errors import NotFound

from apx_agent._memory_extract import extract_declared_session, extraction_enabled
from apx_agent._models import AgentConfig


class ExtractionApi:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.actor_id = "user:alice"
        self.entries: list[dict[str, Any]] = [
            {
                "name": "memory-stores/agent-memory/entries/entry-1",
                "actor_id": "user:alice",
                "path": "/preferences/communication.md",
                "content": "Prefers email.",
                "description": "Communication preference",
                "session_id": "session-1",
            }
        ]
        self.memory: dict[str, dict[str, Any]] = {}
        self.failure: Exception | None = None
        self.session_missing = False

    def do(self, method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        if self.failure is not None and path.endswith("/extractions"):
            raise self.failure
        if path == "/api/2.0/agents/session-stores/agent-sessions":
            return {"name": "session-stores/agent-sessions", "session_store_name": "agent-sessions"}
        if path == "/api/2.0/agents/session-stores/agent-sessions/sessions/session-1":
            if self.session_missing:
                raise NotFound("missing session")
            return {
                "name": "session-stores/agent-sessions/sessions/session-1",
                "session_id": "session-1",
                "actor_id": self.actor_id,
            }
        if path == "/api/2.0/agents/session-stores/agent-sessions/sessions/session-1/extractions":
            assert method == "POST"
            if not body.get("dry_run"):
                for entry in self.entries:
                    self.memory[entry["name"].rsplit("/", 1)[-1]] = dict(entry)
            return {"entries": self.entries}
        if path == "/api/2.0/agents/memory-stores/agent-memory":
            return {
                "name": "memory-stores/agent-memory",
                "display_name": "agent-memory",
            }
        if path.startswith("/api/2.0/agents/memory-stores/agent-memory/entries/"):
            entry_id = path.rsplit("/", 1)[-1]
            if entry_id not in self.memory:
                raise NotFound(entry_id)
            return self.memory[entry_id]
        raise AssertionError(f"unexpected {method} {path}")


def _config(**memory: Any) -> AgentConfig:
    return AgentConfig(
        name="orders",
        target="durable_agent_server",
        memory={"type": "managed", "store_name": "agent-memory", **memory},
        session={"type": "managed", "store_name": "agent-sessions"},
    )


def _ws(api: ExtractionApi) -> SimpleNamespace:
    return SimpleNamespace(api_client=api)


def test_extraction_stays_off_without_an_explicit_policy() -> None:
    assert not extraction_enabled(_config())
    api = ExtractionApi()
    with pytest.raises(ValueError, match="Session extraction is off"):
        extract_declared_session(_ws(api), _config(), session_id="session-1", actor_id="user:alice")
    assert not api.calls


def test_extraction_is_refused_for_non_managed_memory() -> None:
    with pytest.raises(ValueError, match="type='managed'"):
        AgentConfig(
            name="orders",
            memory={"type": "inmemory", "extraction": {"enabled": True}},
        )


def test_dry_run_returns_entries_without_writing_or_reading_memory() -> None:
    api = ExtractionApi()
    result = extract_declared_session(
        _ws(api),
        _config(extraction={"enabled": True, "instructions": "Keep durable preferences."}),
        session_id="session-1",
        actor_id="user:alice",
        dry_run=True,
    )
    assert result.dry_run
    assert result.entries[0].content == "Prefers email."
    assert not result.entries[0].written
    assert api.memory == {}
    extract = next(call for call in api.calls if call["path"].endswith("/extractions"))
    assert extract["body"]["memory_store"] == "memory-stores/agent-memory"
    assert extract["body"]["instructions"] == "Keep durable preferences."
    assert extract["body"]["dry_run"] is True
    assert not any("/memory-stores/" in call["path"] for call in api.calls)


def test_write_is_read_back_for_the_same_actor() -> None:
    api = ExtractionApi()
    result = extract_declared_session(
        _ws(api),
        _config(extraction={"enabled": True}),
        session_id="session-1",
        actor_id="user:alice",
    )
    assert result.entries[0].written
    assert api.memory["entry-1"]["content"] == "Prefers email."
    assert api.memory["entry-1"]["actor_id"] == "user:alice"
    reads = [call for call in api.calls if call["path"].endswith("/entries/entry-1")]
    assert reads and reads[0]["query"] == {"read_mask": "*"}


def test_second_actor_cannot_extract_the_first_session() -> None:
    api = ExtractionApi()
    with pytest.raises(ValueError, match="belongs to actor"):
        extract_declared_session(
            _ws(api),
            _config(extraction={"enabled": True}),
            session_id="session-1",
            actor_id="user:bob",
        )
    assert not any(call["path"].endswith("/extractions") for call in api.calls)


def test_service_failure_propagates_and_reports_no_write() -> None:
    api = ExtractionApi()
    api.failure = RuntimeError("distillation unavailable")
    with pytest.raises(Exception, match="distillation unavailable") as caught:
        extract_declared_session(
            _ws(api),
            _config(extraction={"enabled": True}),
            session_id="session-1",
            actor_id="user:alice",
        )
    assert type(caught.value).__name__ == "AgentCliError"
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert api.memory == {}


def test_read_back_must_match_path_and_session() -> None:
    api = ExtractionApi()
    original = api.do

    def wrong_path(method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        result = original(method, path, query=query, body=body)
        if path.endswith("/entries/entry-1"):
            return {**result, "path": "/other.md"}
        return result

    api.do = wrong_path
    with pytest.raises(ValueError, match="read-back did not match"):
        extract_declared_session(
            _ws(api),
            _config(extraction={"enabled": True}),
            session_id="session-1",
            actor_id="user:alice",
        )


def test_read_back_mismatch_is_not_reported_as_success() -> None:
    api = ExtractionApi()
    api.entries[0]["content"] = "Prefers email."
    original = api.do

    def tamper(method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        result = original(method, path, query=query, body=body)
        if path.endswith("/entries/entry-1"):
            return {**result, "content": "different"}
        return result

    api.do = tamper
    with pytest.raises(ValueError, match="read-back did not match"):
        extract_declared_session(
            _ws(api),
            _config(extraction={"enabled": True}),
            session_id="session-1",
        )


def test_blank_instruction_override_is_refused_before_the_service() -> None:
    api = ExtractionApi()
    config = _config(extraction={"enabled": True, "instructions": "Keep facts."})
    assert config.memory is not None and config.memory.extraction is not None
    config.memory.extraction.instructions = "   "
    with pytest.raises(ValueError, match="non-blank text"):
        extract_declared_session(
            _ws(api), config, session_id="session-1", actor_id="user:alice",
        )
    assert api.calls == []


def test_cli_extract_is_off_until_declared(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from apx_agent.cli import main

    (tmp_path / "pyproject.toml").write_text('[tool.apx.deploy]\nname = "orders"\n', encoding="utf-8")
    (tmp_path / "agent.py").write_text(
        'from apx_agent import Agent\nagent = Agent(name="orders", memory={"type": "inmemory"})\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["memory", "extract", "--session-id", "session-1"])
    assert result.exit_code != 0
    assert "Session extraction is off" in result.output


def test_cli_extract_dry_run_uses_declared_policy(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from apx_agent.cli import main

    (tmp_path / "pyproject.toml").write_text('[tool.apx.deploy]\nname = "orders"\n', encoding="utf-8")
    (tmp_path / "agent.py").write_text(
        """
from apx_agent import Agent

agent = Agent(
    name="orders",
    target="durable_agent_server",
    memory={
        "type": "managed",
        "store_name": "agent-memory",
        "extraction": {"enabled": True, "instructions": "Keep account facts."},
    },
    session={"type": "managed", "store_name": "agent-sessions"},
)
""",
        encoding="utf-8",
    )
    api = ExtractionApi()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient",
        lambda profile=None: SimpleNamespace(api_client=api, profile=profile),
    )
    result = CliRunner().invoke(main, [
        "memory", "extract", "--session-id", "session-1", "--actor-id", "user:alice", "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["written"] is False
    assert payload["entries"][0]["content"] == "Prefers email."
    assert api.memory == {}
