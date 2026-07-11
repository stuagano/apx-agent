"""Unit tests for InMemoryConversationStore — pagination, ordering, and search.

Covers the cursor arithmetic and ordering invariants that must be correct
before trusting any Delta or Lakebase implementation.  All tests use the
public ConversationStore API; no private methods are called.
"""

from __future__ import annotations

import pytest

from apx_agent._conversation import (
    CompactionData,
    ConversationNotFoundError,
    FunctionCallData,
    FunctionCallOutputData,
    InMemoryConversationStore,
    MessageData,
    NewConversationItem,
    NativeToolData,
    ReasoningData,
    paginate_in_memory,
    parse_item_data,
    synthesize_conversation_title,
    compute_search_text,
    PagedList,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _user_msg(text: str) -> NewConversationItem:
    """Build a user message NewConversationItem with plain text content."""
    return NewConversationItem(
        type="message",
        response_id="resp_test",
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _asst_msg(text: str, agent: str = "bot") -> NewConversationItem:
    """Build an assistant message NewConversationItem."""
    return NewConversationItem(
        type="message",
        response_id="resp_test",
        data=MessageData(
            role="assistant",
            agent=agent,
            content=[{"type": "output_text", "text": text}],
        ),
    )


def _func_call(name: str, args: str = "{}") -> NewConversationItem:
    """Build a function_call NewConversationItem."""
    return NewConversationItem(
        type="function_call",
        response_id="resp_fn",
        data=FunctionCallData(agent="bot", name=name, arguments=args, call_id="call_1"),
    )


# ── conversation lifecycle ────────────────────────────────────────────────────


def test_create_and_get_conversation():
    store = InMemoryConversationStore()
    conv = store.create_conversation(kind="default", title="Hello", agent_id="ag1")
    assert conv.id.startswith("conv_")
    assert conv.kind == "default"
    assert conv.title == "Hello"
    assert conv.agent_id == "ag1"
    assert conv.root_conversation_id == conv.id

    fetched = store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.id == conv.id
    assert fetched.title == "Hello"


def test_get_missing_conversation_returns_none():
    store = InMemoryConversationStore()
    assert store.get_conversation("conv_nonexistent") is None


def test_child_conversation_inherits_root_id():
    store = InMemoryConversationStore()
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id)
    assert child.root_conversation_id == parent.root_conversation_id == parent.id


def test_create_with_missing_parent_raises():
    store = InMemoryConversationStore()
    with pytest.raises(ConversationNotFoundError):
        store.create_conversation(parent_conversation_id="conv_missing")


def test_delete_conversation_removes_items():
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    store.append(conv.id, [_user_msg("hi")])
    assert store.delete_conversation(conv.id) is True
    assert store.get_conversation(conv.id) is None
    assert store.list_items(conv.id).data == []


def test_delete_missing_returns_false():
    store = InMemoryConversationStore()
    assert store.delete_conversation("conv_ghost") is False


# ── append ────────────────────────────────────────────────────────────────────


def test_append_assigns_ids_and_timestamps():
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    items = store.append(conv.id, [_user_msg("hello"), _asst_msg("hi back")])
    assert len(items) == 2
    # IDs are distinct and properly prefixed
    assert items[0].id.startswith("item_")
    assert items[1].id.startswith("item_")
    assert items[0].id != items[1].id
    # Timestamps are set
    assert items[0].created_at > 0
    assert items[1].created_at > 0
    # Status
    assert items[0].status == "completed"


def test_append_to_missing_conversation_raises():
    store = InMemoryConversationStore()
    with pytest.raises(ConversationNotFoundError):
        store.append("conv_missing", [_user_msg("hi")])


def test_append_bumps_conversation_updated_at():
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    original_ts = conv.updated_at
    store.append(conv.id, [_user_msg("a")])
    updated = store.get_conversation(conv.id)
    assert updated is not None
    assert updated.updated_at >= original_ts


# ── list_items ordering ───────────────────────────────────────────────────────


def test_list_items_ascending_order():
    """Items returned in insertion order when order='asc'."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    for i in range(5):
        store.append(conv.id, [_user_msg(f"msg{i}")])

    page = store.list_items(conv.id, order="asc")
    texts = [
        next(b["text"] for b in item.data.content if b.get("type") == "input_text")
        for item in page.data
        if hasattr(item.data, "content")
    ]
    assert texts == ["msg0", "msg1", "msg2", "msg3", "msg4"]


def test_list_items_descending_order():
    """Items returned in reverse-insertion order when order='desc'."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    for i in range(5):
        store.append(conv.id, [_user_msg(f"msg{i}")])

    page = store.list_items(conv.id, order="desc")
    texts = [
        next(b["text"] for b in item.data.content if b.get("type") == "input_text")
        for item in page.data
        if hasattr(item.data, "content")
    ]
    assert texts == ["msg4", "msg3", "msg2", "msg1", "msg0"]


# ── list_items cursor pagination ──────────────────────────────────────────────


def test_list_items_after_cursor():
    """after= returns items strictly after the cursor item."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    appended = []
    for i in range(5):
        items = store.append(conv.id, [_user_msg(f"msg{i}")])
        appended.extend(items)

    # after= cursor at index 1 should return items 2, 3, 4
    page = store.list_items(conv.id, after=appended[1].id, order="asc")
    assert len(page.data) == 3
    assert page.data[0].id == appended[2].id
    assert page.data[-1].id == appended[4].id


def test_list_items_before_cursor():
    """before= returns items strictly before the cursor item."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    appended = []
    for i in range(5):
        items = store.append(conv.id, [_user_msg(f"msg{i}")])
        appended.extend(items)

    # before= cursor at index 3 should return items 0, 1, 2
    page = store.list_items(conv.id, before=appended[3].id, order="asc")
    assert len(page.data) == 3
    assert page.data[0].id == appended[0].id
    assert page.data[-1].id == appended[2].id


def test_list_items_limit_and_has_more():
    """limit= truncates the page; has_more is True when more items exist."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    for i in range(5):
        store.append(conv.id, [_user_msg(f"msg{i}")])

    page = store.list_items(conv.id, limit=3, order="asc")
    assert len(page.data) == 3
    assert page.has_more is True

    full = store.list_items(conv.id, limit=5, order="asc")
    assert full.has_more is False


def test_list_items_type_filter():
    """type= filter returns only items of the given type."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    store.append(conv.id, [_user_msg("hi"), _func_call("tool_x")])

    messages = store.list_items(conv.id, type="message")
    assert all(i.type == "message" for i in messages.data)
    assert len(messages.data) == 1

    calls = store.list_items(conv.id, type="function_call")
    assert all(i.type == "function_call" for i in calls.data)
    assert len(calls.data) == 1


def test_list_items_first_last_id_populated():
    """first_id and last_id match the first and last items in the page."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    for i in range(3):
        store.append(conv.id, [_user_msg(f"msg{i}")])
    page = store.list_items(conv.id, order="asc")
    assert page.first_id == page.data[0].id
    assert page.last_id == page.data[-1].id


# ── list_conversations ────────────────────────────────────────────────────────


def test_list_conversations_kind_filter():
    """kind= filter returns only conversations of the matching kind."""
    store = InMemoryConversationStore()
    store.create_conversation(kind="default")
    store.create_conversation(kind="default")
    store.create_conversation(kind="sub_agent")

    defaults = store.list_conversations(kind="default")
    assert all(c.kind == "default" for c in defaults.data)
    assert len(defaults.data) == 2

    sub = store.list_conversations(kind="sub_agent")
    assert len(sub.data) == 1

    all_kinds = store.list_conversations(kind=None)
    assert len(all_kinds.data) == 3


def test_list_conversations_agent_id_filter():
    """agent_id= filter returns only conversations for that agent."""
    store = InMemoryConversationStore()
    store.create_conversation(agent_id="ag1")
    store.create_conversation(agent_id="ag1")
    store.create_conversation(agent_id="ag2")

    page = store.list_conversations(kind=None, agent_id="ag1")
    assert len(page.data) == 2
    assert all(c.agent_id == "ag1" for c in page.data)


def test_list_conversations_search_query_title_match():
    """search_query matches on title."""
    store = InMemoryConversationStore()
    store.create_conversation(title="Deploy pipeline", kind=None)
    store.create_conversation(title="Data analysis", kind=None)

    page = store.list_conversations(kind=None, search_query="pipeline")
    assert len(page.data) == 1
    assert page.data[0].title == "Deploy pipeline"


def test_list_conversations_search_query_item_content_match():
    """search_query matches on item content when title doesn't match."""
    store = InMemoryConversationStore()
    c1 = store.create_conversation(kind=None)
    c2 = store.create_conversation(kind=None)
    store.append(c1.id, [_user_msg("kubernetes deployment error")])
    store.append(c2.id, [_user_msg("unrelated topic")])

    page = store.list_conversations(kind=None, search_query="kubernetes")
    assert len(page.data) == 1
    assert page.data[0].id == c1.id


def test_list_conversations_cursor_pagination():
    """after= cursor advances through the list."""
    store = InMemoryConversationStore()
    for i in range(6):
        store.create_conversation(kind="default")

    page1 = store.list_conversations(limit=3, order="asc")
    assert len(page1.data) == 3
    assert page1.has_more is True

    page2 = store.list_conversations(limit=3, order="asc", after=page1.last_id)
    assert len(page2.data) == 3
    # No overlap between pages
    ids1 = {c.id for c in page1.data}
    ids2 = {c.id for c in page2.data}
    assert ids1.isdisjoint(ids2)


# ── search ────────────────────────────────────────────────────────────────────


def test_search_returns_matching_items():
    """search() returns items whose content contains the query."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    store.append(conv.id, [_user_msg("how to deploy to kubernetes")])
    store.append(conv.id, [_user_msg("what is the weather")])

    results = store.search("kubernetes")
    assert len(results) == 1
    assert isinstance(results[0].data, MessageData)
    content_text = " ".join(
        b["text"] for b in results[0].data.content if isinstance(b, dict) and "text" in b
    )
    assert "kubernetes" in content_text.lower()


def test_search_scoped_to_conversation():
    """search() respects conversation_id scoping."""
    store = InMemoryConversationStore()
    c1 = store.create_conversation()
    c2 = store.create_conversation()
    store.append(c1.id, [_user_msg("error in production")])
    store.append(c2.id, [_user_msg("error in staging")])

    results = store.search("error", conversation_id=c1.id)
    assert len(results) == 1


def test_search_limit():
    """search() respects the limit parameter."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    for i in range(10):
        store.append(conv.id, [_user_msg(f"error message {i}")])

    results = store.search("error", limit=3)
    # 3 results exactly — limit is enforced
    assert len(results) == 3


# ── update_conversation ───────────────────────────────────────────────────────


def test_update_conversation_title():
    store = InMemoryConversationStore()
    conv = store.create_conversation(title="old")
    updated = store.update_conversation(conv.id, title="new")
    assert updated is not None
    assert updated.title == "new"


def test_update_conversation_model_override():
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    updated = store.update_conversation(conv.id, model_override="claude-opus-4-8")
    assert updated is not None
    assert updated.model_override == "claude-opus-4-8"


def test_update_conversation_unset_flags():
    """_unset_* flags clear the field even when the parameter is also set."""
    store = InMemoryConversationStore()
    conv = store.create_conversation()
    store.update_conversation(conv.id, reasoning_effort="high", model_override="opus")

    cleared = store.update_conversation(
        conv.id, _unset_reasoning_effort=True, _unset_model_override=True
    )
    assert cleared is not None
    assert cleared.reasoning_effort is None
    assert cleared.model_override is None


def test_update_missing_conversation_returns_none():
    store = InMemoryConversationStore()
    result = store.update_conversation("conv_ghost", title="irrelevant")
    assert result is None


# ── paginate_in_memory ────────────────────────────────────────────────────────


def test_paginate_in_memory_empty():
    """Paginating an empty list returns an empty PagedList."""
    result = paginate_in_memory([], id_fn=lambda x: x, limit=10)
    assert result.data == []
    assert result.first_id is None
    assert result.last_id is None
    assert result.has_more is False


def test_paginate_in_memory_single_page():
    items = ["a", "b", "c"]
    result = paginate_in_memory(items, id_fn=lambda x: x, limit=10, order="asc")
    assert result.data == ["a", "b", "c"]
    assert result.has_more is False
    assert result.first_id == "a"
    assert result.last_id == "c"


def test_paginate_in_memory_desc_reverses():
    items = ["a", "b", "c"]
    result = paginate_in_memory(items, id_fn=lambda x: x, limit=10, order="desc")
    assert result.data == ["c", "b", "a"]


def test_paginate_in_memory_after_cursor():
    items = ["a", "b", "c", "d"]
    result = paginate_in_memory(items, id_fn=lambda x: x, limit=10, after="b", order="asc")
    assert result.data == ["c", "d"]


def test_paginate_in_memory_before_cursor():
    items = ["a", "b", "c", "d"]
    result = paginate_in_memory(items, id_fn=lambda x: x, limit=10, before="c", order="asc")
    assert result.data == ["a", "b"]


def test_paginate_in_memory_has_more_flag():
    items = list("abcdefgh")  # 8 items
    # limit=5: page has 5, has_more=True
    result = paginate_in_memory(items, id_fn=lambda x: x, limit=5, order="asc")
    assert len(result.data) == 5
    assert result.has_more is True
    # limit=8: all items, has_more=False
    result2 = paginate_in_memory(items, id_fn=lambda x: x, limit=8, order="asc")
    assert result2.has_more is False


# ── entity helpers ────────────────────────────────────────────────────────────


def test_synthesize_conversation_title_truncates():
    content = [{"type": "input_text", "text": "a" * 100}]
    title = synthesize_conversation_title(content, limit=60)
    assert title is not None
    assert len(title) <= 60
    assert title.endswith("…")


def test_synthesize_conversation_title_no_text_returns_none():
    assert synthesize_conversation_title([]) is None
    assert synthesize_conversation_title([{"type": "image"}]) is None


def test_compute_search_text_message():
    data = MessageData(
        role="user", content=[{"type": "input_text", "text": "hello world"}]
    )
    assert "hello world" in compute_search_text("message", data)


def test_compute_search_text_function_call():
    data = FunctionCallData(agent="bot", name="search", arguments='{"q":"foo"}', call_id="c1")
    text = compute_search_text("function_call", data)
    assert "search" in text
    assert "foo" in text


def test_compute_search_text_non_searchable():
    data = NativeToolData(item={"type": "web_search_call"})
    assert compute_search_text("native_tool", data) == ""


def test_parse_item_data_roundtrip():
    """parse_item_data deserialises a dict back to the correct model type."""
    raw = {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    data = parse_item_data("message", raw)
    assert isinstance(data, MessageData)
    assert data.role == "user"


def test_parse_item_data_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown item type"):
        parse_item_data("made_up_type", {})


def test_drop_orphaned_tool_outputs_removes_result_without_matching_call():
    """A function_call_output whose call_id has no function_call is dropped;
    a well-formed call/result pair is kept (read-boundary guard, #329)."""
    from apx_agent._conversation import (
        ConversationItem,
        FunctionCallData,
        FunctionCallOutputData,
        drop_orphaned_tool_outputs,
    )

    def _item(item_id, type_, data):
        return ConversationItem(
            id=item_id, type=type_, status="completed", response_id="r1",
            created_at=1000, data=data,
        )

    items = [
        _item("i1", "function_call",
              FunctionCallData(agent="bot", name="f", arguments="{}", call_id="c1")),
        _item("i2", "function_call_output", FunctionCallOutputData(call_id="c1", output="ok")),
        # orphan: no function_call with call_id "orphan"
        _item("i3", "function_call_output", FunctionCallOutputData(call_id="orphan", output="x")),
    ]
    kept = drop_orphaned_tool_outputs(items)
    kept_ids = [it.id for it in kept]
    assert kept_ids == ["i1", "i2"]  # the paired result survives; the orphan is gone


def test_drop_orphaned_tool_outputs_removes_call_without_matching_result():
    """The mirror case: a function_call whose call_id has no function_call_output
    is dropped too. If a tool's output never got persisted (a crash or a race
    during parallel tool execution, for instance), replaying the orphaned call
    alone produces a tool_use block with no following tool_result — Databricks-
    Claude 400s the whole request, permanently corrupting that conversation
    thread on every subsequent turn. A well-formed pair is kept."""
    from apx_agent._conversation import (
        ConversationItem,
        FunctionCallData,
        FunctionCallOutputData,
        drop_orphaned_tool_outputs,
    )

    def _item(item_id, type_, data):
        return ConversationItem(
            id=item_id, type=type_, status="completed", response_id="r1",
            created_at=1000, data=data,
        )

    items = [
        _item("i1", "function_call",
              FunctionCallData(agent="bot", name="f", arguments="{}", call_id="c1")),
        _item("i2", "function_call_output", FunctionCallOutputData(call_id="c1", output="ok")),
        # orphan: a second tool call whose output never got persisted
        _item("i3", "function_call",
              FunctionCallData(agent="bot", name="g", arguments="{}", call_id="orphan")),
    ]
    kept = drop_orphaned_tool_outputs(items)
    kept_ids = [it.id for it in kept]
    assert kept_ids == ["i1", "i2"]  # the paired call/result survives; the orphaned call is gone


def test_drop_orphaned_tool_outputs_drops_both_sides_of_a_parallel_call_turn():
    """Reproduces the reported bug directly: an assistant turn calls two tools
    in parallel (two function_call items sharing one response_id), but only
    one tool's output was ever persisted. Before the fix, the surviving
    orphaned function_call coalesces into the same AIMessage as its sibling
    (_conv_items_to_lc_messages groups by response_id), producing a tool_calls
    list with no matching ToolMessage for one call_id — which is exactly the
    'tool_use ids were found without tool_result blocks' 400 from Anthropic."""
    from apx_agent._conversation import (
        ConversationItem,
        FunctionCallData,
        FunctionCallOutputData,
        drop_orphaned_tool_outputs,
    )

    def _item(item_id, type_, data):
        return ConversationItem(
            id=item_id, type=type_, status="completed", response_id="r1",
            created_at=1000, data=data,
        )

    items = [
        _item("i1", "function_call",
              FunctionCallData(agent="bot", name="ask_brickroad", arguments="{}", call_id="c1")),
        _item("i2", "function_call",
              FunctionCallData(agent="bot", name="run_sql", arguments="{}", call_id="c2")),
        # only c1's output ever got persisted; c2's never did
        _item("i3", "function_call_output", FunctionCallOutputData(call_id="c1", output="ok")),
    ]
    kept = drop_orphaned_tool_outputs(items)
    kept_ids = [it.id for it in kept]
    assert kept_ids == ["i1", "i3"]  # c1's call+result pair survives; c2's orphaned call is gone


def test_conversation_item_to_api_dict():
    """to_api_dict() returns a flat, JSON-safe representation."""
    from apx_agent._conversation import ConversationItem
    item = ConversationItem(
        id="item_1",
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=1000,
        data=MessageData(
            role="user", content=[{"type": "input_text", "text": "hi"}]
        ),
    )
    d = item.to_api_dict()
    assert d["id"] == "item_1"
    assert d["type"] == "message"
    assert d["role"] == "user"
    assert "content" in d
    assert "created_at" in d
    # None fields excluded
    assert "model" not in d
    assert "created_by" not in d


def test_message_data_assistant_requires_agent():
    """MessageData validator raises if assistant message has no agent."""
    with pytest.raises(Exception):
        MessageData(role="assistant", content=[])


def test_new_conversation_item_type_mismatch_raises():
    """NewConversationItem validator rejects mismatched type/data combinations."""
    with pytest.raises(Exception):
        NewConversationItem(
            type="function_call",
            response_id="r1",
            data=MessageData(role="user", content=[]),
        )


# ── item-data alias round-trip (the "model"/"agent" storage round-trip) ───────
# Regression guard: Delta/Lakebase stores persist item data with
# ``model_dump_json(by_alias=True)`` (key ``"model"``) and rebuild with
# ``parse_item_data`` (``cls(**raw)``). The ``agent`` field must accept the
# alias back, or every read of a persisted assistant turn raises.


@pytest.mark.parametrize(
    "item_type,data",
    [
        ("message", MessageData(role="assistant", content=[{"type": "text", "text": "hi"}], agent="acme")),
        ("function_call", FunctionCallData(agent="acme", name="search", arguments="{}", call_id="c1")),
        ("reasoning", ReasoningData(agent="acme", summary=[{"type": "summary_text", "text": "x"}])),
    ],
)
def test_item_data_survives_by_alias_storage_round_trip(item_type, data):
    """data -> model_dump_json(by_alias=True) -> parse_item_data preserves ``agent``."""
    import json

    wire = json.loads(data.model_dump_json(by_alias=True, exclude_none=True))
    assert wire["model"] == "acme"  # persisted under the alias
    back = parse_item_data(item_type, wire)
    assert back.agent == "acme"  # ...and read back without dropping it
