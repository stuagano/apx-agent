"""Typed memory strategies (#1088): config, extractor, managed M2 path, recall."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.errors import NotFound
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import ValidationError

from apx_agent._memory import InMemoryMemoryStore, MemoryFilter, RecallOptions
from apx_agent._memory_extract import extract_declared_session, extraction_enabled
from apx_agent._memory_strategies import (
    BUILTIN_PROMPTS,
    OUTPUT_CONTRACTS,
    extract_strategy_memories,
    parse_strategy_output,
    strategy_system_prompt,
)
from apx_agent._models import AgentConfig, MemoryBackendConfig
from apx_agent._prompt_assembly import assemble_memory_context

ALL = ["facts", "preferences", "summary"]


# ---------------------------------------------------------------- config


@pytest.mark.parametrize("name", ["episodic", "semantic"])
def test_unknown_strategy_is_refused_naming_the_allowed_set(name: str) -> None:
    with pytest.raises(ValidationError, match="'facts', 'preferences' or 'summary'"):
        MemoryBackendConfig(strategies=[name])


def test_strategy_prompt_key_must_be_a_declared_strategy() -> None:
    with pytest.raises(ValidationError, match="not in memory.strategies"):
        MemoryBackendConfig(strategies=["facts"], strategy_prompts={"summary": "Recap."})


def test_blank_strategy_prompt_is_refused() -> None:
    with pytest.raises(ValidationError, match="non-blank"):
        MemoryBackendConfig(strategies=["facts"], strategy_prompts={"facts": "  "})


def test_no_field_changes_the_output_contract() -> None:
    with pytest.raises(ValidationError):
        MemoryBackendConfig(strategies=["facts"], output_contract="anything")  # type: ignore[call-arg]
    cfg = MemoryBackendConfig(strategies=["facts"], strategy_prompts={"facts": "Only deploy targets."})
    prompt = strategy_system_prompt(cfg, "facts")
    assert prompt.startswith("Only deploy targets.")
    assert BUILTIN_PROMPTS["facts"] not in prompt  # the override replaces the built-in text
    assert prompt.endswith(OUTPUT_CONTRACTS["facts"])  # the contract is always appended


def test_strategies_are_allowed_on_every_store_type() -> None:
    for kind in ("inmemory", "lakebase", "managed"):
        assert MemoryBackendConfig(type=kind, strategies=ALL).strategies == ALL


def test_recalled_namespaces_default_and_override() -> None:
    assert MemoryBackendConfig().recalled_namespaces() == ["default"]
    assert MemoryBackendConfig(strategies=ALL).recalled_namespaces() == ["default", "facts", "preferences"]
    listed = MemoryBackendConfig(strategies=ALL, recall_namespaces=["facts", "summary"])
    assert listed.recalled_namespaces() == ["facts", "summary"]
    with pytest.raises(ValidationError, match="non-blank"):
        MemoryBackendConfig(recall_namespaces=[" "])


# ---------------------------------------------------------------- extractor


class _Model(GenericFakeChatModel):
    """Answers each strategy by matching its built-in instructions in the system prompt."""

    answers: dict[str, str] = {}
    seen: list[list[BaseMessage]] = []

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        self.seen.append(list(messages))
        system = str(messages[0].content)
        strategy = next(s for s in ALL if BUILTIN_PROMPTS[s] in system or f"[{s}]" in system)
        self.messages = iter([AIMessage(content=self.answers[strategy])])
        return super()._generate(messages, *args, **kwargs)


def _model(**answers: str) -> _Model:
    return _Model(messages=iter([]), answers=answers, seen=[])


def _cfg(**kw: Any) -> MemoryBackendConfig:
    return MemoryBackendConfig(strategies=ALL, **kw)


GOOD = {
    "facts": json.dumps(["Alice owns the billing service"]),
    "preferences": "```json\n" + json.dumps(["Wants answers as tables"]) + "\n```",
    "summary": json.dumps("Alice asked about billing deploys."),
}


def _extract(store: InMemoryMemoryStore, model: _Model, **kw: Any) -> Any:
    return extract_strategy_memories(
        store, model, kw.pop("cfg", _cfg()), transcript="user: hi", principal_id="user:alice",
        session_id=kw.pop("session_id", "s1"), **kw,
    )


def test_records_land_in_their_strategy_namespace_under_the_session_user() -> None:
    store = InMemoryMemoryStore()
    written = _extract(store, _model(**GOOD))
    by_ns = {m.namespace: m.content for m in written}
    assert by_ns == {
        "facts": "Alice owns the billing service",
        "preferences": "Wants answers as tables",
        "summary": "Alice asked about billing deploys.",
    }
    assert {m.principal_id for m in written} == {"user:alice"}
    assert store.list(MemoryFilter(principal_id="app-service-principal")) == []
    assert all(m.metadata["session_id"] == "s1" for m in written)


def test_each_strategy_gets_its_own_call_with_the_fixed_contract() -> None:
    model = _model(**GOOD)
    _extract(InMemoryMemoryStore(), model)
    systems = [str(p[0].content) for p in model.seen]
    assert len(systems) == 3
    for strategy, system in zip(ALL, systems):
        assert system.endswith(OUTPUT_CONTRACTS[strategy])
    assert "user: hi" in str(model.seen[0][1].content)


def test_rerunning_a_session_keeps_one_summary() -> None:
    store = InMemoryMemoryStore()
    _extract(store, _model(**GOOD))
    _extract(store, _model(**{**GOOD, "summary": json.dumps("Second recap.")}))
    summaries = store.list(MemoryFilter(principal_id="user:alice", namespace="summary"))
    assert [m.content for m in summaries] == ["Second recap."]
    # A different session gets its own summary.
    _extract(store, _model(**GOOD), session_id="s2")
    assert len(store.list(MemoryFilter(principal_id="user:alice", namespace="summary"))) == 2


@pytest.mark.parametrize("strategy,bad", [
    ("facts", "Alice owns billing"),          # not JSON
    ("preferences", json.dumps({"a": 1})),     # not an array
    ("facts", json.dumps(["ok", ""])),         # blank entry
    ("summary", json.dumps(["a", "b"])),       # summary must be one string
])
def test_malformed_output_raises_and_writes_nothing(strategy: str, bad: str) -> None:
    store = InMemoryMemoryStore()
    with pytest.raises(ValueError, match=strategy):
        _extract(store, _model(**{**GOOD, strategy: bad}))
    assert store.list(MemoryFilter(principal_id="user:alice")) == []


def test_empty_array_writes_nothing_for_that_strategy() -> None:
    assert parse_strategy_output("facts", "[]") == []
    store = InMemoryMemoryStore()
    _extract(store, _model(**{**GOOD, "facts": "[]"}))
    assert store.list(MemoryFilter(principal_id="user:alice", namespace="facts")) == []


def test_override_prompt_reaches_the_model() -> None:
    model = _model(**GOOD)
    cfg = _cfg(strategy_prompts={"facts": "[facts] Only deploy targets."})
    _extract(InMemoryMemoryStore(), model, cfg=cfg)
    assert str(model.seen[0][0].content).startswith("[facts] Only deploy targets.")


# ---------------------------------------------------------------- managed (M2)

MEM = "/api/2.0/agents/memory-stores/agent-memory"
SESSION = "/api/2.0/agents/session-stores/agent-sessions/sessions/session-1"


class ManagedApi:
    """Fake AgentKit REST: a session whose dry-run extraction answers per strategy."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entries: dict[str, dict[str, Any]] = {}
        self.tamper = False

    def _found(self, instructions: str) -> list[dict[str, Any]]:
        for strategy, text in (("summary", "Recap of session-1."), ("preferences", "Prefers email."),
                               ("facts", "Owns billing.")):
            if BUILTIN_PROMPTS[strategy] in instructions:
                return [{"name": f"memory-stores/agent-memory/entries/draft-{strategy}", "content": text,
                         "actor_id": "user:alice", "session_id": "session-1"}]
        raise AssertionError(instructions)

    def do(self, method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        if path == "/api/2.0/agents/session-stores/agent-sessions":
            return {"name": "session-stores/agent-sessions", "session_store_name": "agent-sessions"}
        if path == SESSION:
            return {"name": "session-stores/agent-sessions/sessions/session-1",
                    "session_id": "session-1", "actor_id": "user:alice"}
        if path == SESSION + "/extractions":
            assert body["dry_run"] is True  # M2 never lets the service write
            return {"entries": self._found(body["instructions"])}
        if path == MEM:
            return {"name": "memory-stores/agent-memory", "display_name": "agent-memory"}
        if path == MEM + "/entries" and method == "POST":
            entry_id = f"entry-{len(self.calls)}"
            entry = {**body, "name": f"memory-stores/agent-memory/entries/{entry_id}"}
            self.entries[entry_id] = entry
            return entry
        if path == MEM + "/entries":
            return {"managed_memory_entries": [
                e for e in self.entries.values()
                if e["actor_id"] == query["actor_id"] and e["path"].startswith(query["path_prefix"])
                and e.get("session_id") == query["session_id"]
            ]}
        entry_id = path.rsplit("/", 1)[-1]
        if entry_id not in self.entries:
            raise NotFound(entry_id)
        if method == "DELETE":
            del self.entries[entry_id]
            return {}
        return {**self.entries[entry_id], "content": "tampered"} if self.tamper else self.entries[entry_id]


def _managed(**memory: Any) -> AgentConfig:
    return AgentConfig(
        name="orders", target="durable_agent_server",
        memory={"type": "managed", "store_name": "agent-memory", "strategies": ALL, **memory},
        session={"type": "managed", "store_name": "agent-sessions"},
    )


def _extractions(api: ManagedApi) -> list[dict[str, Any]]:
    return [c for c in api.calls if c["path"].endswith("/extractions")]


def test_managed_strategies_enable_extraction_without_a_policy() -> None:
    assert extraction_enabled(_managed())
    assert not extraction_enabled(_managed(extraction={"enabled": False}))


def test_managed_one_dry_run_per_strategy_then_writes_at_the_strategy_path() -> None:
    api = ManagedApi()
    result = extract_declared_session(
        SimpleNamespace(api_client=api), _managed(extraction={"enabled": True, "instructions": "Be terse."}),
        session_id="session-1", actor_id="user:alice",
    )
    calls = _extractions(api)
    assert len(calls) == 3
    for strategy, call in zip(ALL, calls):
        assert call["body"]["instructions"] == f"Be terse.\n\n{BUILTIN_PROMPTS[strategy]}"
        assert call["body"]["dry_run"] is True
    paths = sorted(e["path"].rsplit("/", 1)[0] for e in api.entries.values())
    assert paths == ["/memories/facts", "/memories/preferences", "/memories/summary"]
    assert {e["session_id"] for e in api.entries.values()} == {"session-1"}
    assert {e["actor_id"] for e in api.entries.values()} == {"user:alice"}
    assert all(entry.written for entry in result.entries)
    # Each write was read back.
    reads = [c for c in api.calls if c["method"] == "GET" and "/entries/entry-" in c["path"]]
    assert len(reads) == 3


def test_managed_rerun_replaces_the_session_summary() -> None:
    api = ManagedApi()
    for _ in range(2):
        extract_declared_session(SimpleNamespace(api_client=api), _managed(), session_id="session-1")
    summaries = [e for e in api.entries.values() if e["path"].startswith("/memories/summary/")]
    assert len(summaries) == 1


def test_managed_dry_run_writes_nothing() -> None:
    api = ManagedApi()
    result = extract_declared_session(
        SimpleNamespace(api_client=api), _managed(), session_id="session-1", dry_run=True,
    )
    assert api.entries == {}
    assert [e.path for e in result.entries] == ["/memories/facts/", "/memories/preferences/", "/memories/summary/"]


def test_managed_read_back_mismatch_raises() -> None:
    api = ManagedApi()
    api.tamper = True
    with pytest.raises(ValueError, match="read-back did not match"):
        extract_declared_session(SimpleNamespace(api_client=api), _managed(), session_id="session-1")


def test_managed_without_strategies_keeps_the_single_call() -> None:
    api = ManagedApi()
    api._found = lambda instructions: []  # type: ignore[method-assign]
    original = api.do

    def single(method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        if path.endswith("/extractions"):
            api.calls.append({"method": method, "path": path, "query": query, "body": body})
            return {"entries": []}
        return original(method, path, query=query, body=body)

    api.do = single  # type: ignore[method-assign]
    config = AgentConfig(
        name="orders", target="durable_agent_server",
        memory={"type": "managed", "store_name": "agent-memory",
                "extraction": {"enabled": True, "instructions": "Keep facts."}},
        session={"type": "managed", "store_name": "agent-sessions"},
    )
    extract_declared_session(SimpleNamespace(api_client=api), config, session_id="session-1")
    calls = _extractions(api)
    assert len(calls) == 1
    assert calls[0]["body"]["instructions"] == "Keep facts."
    assert not calls[0]["body"].get("dry_run")  # the service writes, as before


# ---------------------------------------------------------------- recall


def test_recall_merges_namespaces_caps_at_k_and_skips_summary() -> None:
    vectors = {"q": [1.0, 0.0], "fact": [0.9, 0.1], "pref": [0.8, 0.2], "deflt": [0.7, 0.3],
               "recap": [1.0, 0.0], "other": [0.0, 1.0]}
    store = InMemoryMemoryStore(embedding_fn=lambda texts: [vectors[t] for t in texts])
    for ns, content in (("facts", "fact"), ("preferences", "pref"), ("default", "deflt"),
                        ("summary", "recap"), ("consolidated", "other")):
        store.add({"principal_id": "alice", "namespace": ns, "content": content})
    cfg = _cfg(recall_k=2)
    opts = RecallOptions(principal_id="alice", query="q", k=cfg.recall_k)

    block = assemble_memory_context(store, opts, namespaces=cfg.recalled_namespaces())
    assert block.splitlines()[1:] == ["- fact", "- pref"]  # merged by score, top k
    assert "recap" not in block

    listed = _cfg(recall_k=2, recall_namespaces=["facts", "preferences", "default", "summary"])
    block = assemble_memory_context(store, opts, namespaces=listed.recalled_namespaces())
    assert block.splitlines()[1:] == ["- recap", "- fact"]
