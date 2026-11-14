"""#1091 claim-vs-reality: the memory docs must match the memory API on main.

``docs/running/sessions-and-memory.md`` showed ``store.add(principal_id=...)``,
a ``lambda ctx:`` resolver passed positionally, and a "three stores" table that
listed two; ``docs/agents/llm-agent.md`` listed three of the five memory tiers.
Each assertion pins a fixed doc claim to the code it describes.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from apx_agent import (
    InMemoryMemoryStore,
    LlmAgent,
    MemoryFilter,
    RecallOptions,
    make_memory_tools,
)
from apx_agent._memory_tools import make_memory_tools as _raw_make_memory_tools
from apx_agent._models import _KNOB_TO_TYPE, MemoryBackendConfig

DOCS = Path(__file__).resolve().parents[2] / "docs"
SESSIONS = (DOCS / "running" / "sessions-and-memory.md").read_text()
LLM_AGENT = (DOCS / "agents" / "llm-agent.md").read_text()


def test_documented_add_and_recall_shapes_run() -> None:
    """The doc's add({...}) / RecallOptions / MemoryFilter calls work as written."""
    assert 'store.add({\n    "principal_id": "user:alice",' in SESSIONS
    assert "store.recall(RecallOptions(" in SESSIONS
    store = InMemoryMemoryStore()
    store.add(
        {
            "principal_id": "user:alice",
            "content": "Prefers email summaries on Mondays, not Slack pings.",
            "namespace": "profile",
            "tags": ["preference", "notification"],
            "importance": 0.8,
        }
    )
    hits = store.recall(
        RecallOptions(principal_id="user:alice", query="notify alice", namespace="profile", k=3)
    )
    assert [h.memory.content for h in hits] == ["Prefers email summaries on Mondays, not Slack pings."]
    assert len(store.list(MemoryFilter(principal_id="user:alice", namespace="profile"))) == 1


def test_memory_tool_factory_is_keyword_only_with_zero_arg_resolver() -> None:
    sig = inspect.signature(make_memory_tools)
    assert sig.parameters["store"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "Callable[[], str | None]" in str(sig.parameters["principal_id_resolver"].annotation)
    assert "make_memory_tools(store=store, principal_id_resolver=current_principal)" in SESSIONS
    assert "lambda ctx" not in SESSIONS
    tools = make_memory_tools(store=InMemoryMemoryStore(), principal_id_resolver=lambda: "user:alice")
    assert [t.__name__ for t in tools] == ["recall", "remember", "forget"]


def test_session_store_count_matches_table() -> None:
    assert "Two stores cover the main workloads:" in SESSIONS
    assert "Three stores" not in SESSIONS


def test_memory_pipeline_is_how_sessions_become_memory() -> None:
    """#858 / ADR 0001: apx no longer extracts; the doc points at the pipeline."""
    section = SESSIONS.split("### Memory pipeline", 1)[1].split("\n### ", 1)[0]
    assert "agentbricks memory pipeline create" in section
    assert "apx-agent memory extract" not in SESSIONS
    assert "consolidate_memories" not in SESSIONS


def test_no_doc_uses_the_uninstalled_apx_memory_script() -> None:
    """Only ``apx-agent`` is in [project.scripts]; ``apx memory`` does not exist."""
    stale = [p for p in DOCS.rglob("*.md") if "apx memory " in p.read_text()]
    assert stale == []


def test_customer_triage_readme_imports_only_exported_names() -> None:
    import apx_agent

    readme = (DOCS.parent / "python" / "examples" / "customer_triage" / "README.md").read_text()
    assert "DeltaConversationStore" not in readme
    assert "from apx_agent import compile_to_chat_agent, LakebaseConversationStore" in readme
    assert hasattr(apx_agent, "LakebaseConversationStore")


def test_llm_agent_memory_row_lists_every_tier() -> None:
    row = next(line for line in LLM_AGENT.splitlines() if line.startswith("| `memory` |"))
    for tier in _KNOB_TO_TYPE:
        assert f'"{tier}"' in row, f"llm-agent.md memory row is missing tier {tier!r}"
    assert "session" in inspect.signature(LlmAgent).parameters
    assert any(line.startswith("| `session` |") for line in LLM_AGENT.splitlines())


def test_forget_docstring_does_not_claim_server_side_scoping() -> None:
    """The client-side ownership check is the real gate; the docstring must say so."""
    source = inspect.getsource(_raw_make_memory_tools)
    assert "already scopes get/delete server-side" not in source
    assert "principal-agnostic" not in source
    assert "GET-by-ID is not actor scoped" in source


def test_documented_retention_and_recall_namespaces_still_validate() -> None:
    assert MemoryBackendConfig(recall_namespaces=["summary"]).recalled_namespaces() == ["default", "summary"]
    MemoryBackendConfig(type="lakebase", retention={"namespaces": {"summary": 30}})
