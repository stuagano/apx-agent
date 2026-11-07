"""#1091 claim-vs-reality: the memory docs must match the memory API on main.

``docs/running/sessions-and-memory.md`` showed ``store.add(principal_id=...)``,
a ``lambda ctx:`` resolver passed positionally, and a "three stores" table that
listed two; ``docs/agents/llm-agent.md`` listed three of the five memory tiers.
Each assertion pins a fixed doc claim to the code it describes.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from apx_agent import (
    InMemoryMemoryStore,
    LlmAgent,
    MemoryFilter,
    RecallOptions,
    consolidate_memories,
    make_memory_tools,
)
from apx_agent._memory_strategies import OUTPUT_CONTRACTS, strategy_system_prompt
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


def test_consolidate_memories_is_keyword_only() -> None:
    params = inspect.signature(consolidate_memories).parameters
    for name in ("store", "principal_id", "summarize_fn"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert "consolidate_memories(store=..., principal_id=..., summarize_fn=...)" in SESSIONS


def test_session_store_count_matches_table() -> None:
    assert "Two stores cover the main workloads:" in SESSIONS
    assert "Three stores" not in SESSIONS


def test_extract_cli_uses_the_installed_script_name() -> None:
    assert "apx-agent memory extract --session-id" in SESSIONS
    assert "\napx memory extract" not in SESSIONS


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


def test_memory_strategies_example_is_the_real_config() -> None:
    """#1088: the documented strategies block validates and does what the prose says."""
    section = SESSIONS.split("### Memory strategies", 1)[1].split("\n### ", 1)[0]
    block = re.search(r"```python\nmemory=(\{.*?\})\n```", section, re.S)
    assert block is not None
    cfg = MemoryBackendConfig.model_validate(ast.literal_eval(block.group(1)))
    assert cfg.strategies == ["facts", "preferences", "summary"]
    assert strategy_system_prompt(cfg, "preferences") == (
        "Only capture reporting-format preferences.\n\n" + OUTPUT_CONTRACTS["preferences"]
    )
    assert cfg.recalled_namespaces() == ["default", "facts", "preferences"]

    listed = re.search(r'"recall_namespaces": (\[.*?\])', section)
    assert listed is not None
    opted = cfg.model_copy(update={"recall_namespaces": ast.literal_eval(listed.group(1))})
    assert "summary" in opted.recalled_namespaces()
    retention = re.search(r'"retention": (\{.*?\}\})', section)
    assert retention is not None
    MemoryBackendConfig(type="lakebase", retention=ast.literal_eval(retention.group(1)))
    assert "extract_agent_session(agent, model=..., transcript=...," in section
