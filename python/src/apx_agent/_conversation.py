"""Conversation persistence layer — entities, abstract interface, and in-memory implementation.

Aligned copy of the omniagents ConversationStore interface (subset).
  Source: omniagents/entities/conversation.py,
          omniagents/entities/pagination.py,
          omniagents/stores/conversation_store/__init__.py
  Aligned at: omniagents main, 2026-06-09

Agent-plane fields stripped from Conversation:
  runner_id, host_id, workspace, git_branch, terminal_launch_args,
  external_session_id, sub_agent_name, labels, archived.

Agent-plane methods stripped from ConversationStore:
  get_runner_ids, get_session_connectivity, get_conversations,
  list_latest_message_items_for_conversations, set_labels,
  set_session_usage, update_conversation_runner, create_session, create_response,
  close_inbox, open_inbox, delete_response, list_responses, find_for_token,
  and fork-related methods.

(set_session_state was restored for G3 session-state persistence — see docs/design/session-state-persistence.md.)

When omniagents is released as a public package, replace this module with a direct import.
"""

from __future__ import annotations

import secrets
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Constants ─────────────────────────────────────────────────────────────────

T = TypeVar("T")

# Item types that are metadata/lifecycle events — not content the agent loop
# should include in the LLM's message context.
NON_CONTENT_ITEM_TYPES: frozenset[str] = frozenset(
    {"compaction", "resource_event", "slash_command", "terminal_command"}
)

# ── Pagination ────────────────────────────────────────────────────────────────


@dataclass
class PagedList(Generic[T]):
    """
    A page of results matching the OpenAI list pagination shape.

    :param data: The items in this page.
    :param first_id: ID of the first item in the page, or ``None`` if empty.
    :param last_id: ID of the last item in the page, or ``None`` if empty.
    :param has_more: ``True`` if more pages exist after this one.
    """

    data: list[T] = field(default_factory=list)
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


def paginate_in_memory(
    items: list[T],
    id_fn: Callable[[T], str],
    *,
    limit: int = 20,
    after: str | None = None,
    before: str | None = None,
    order: str = "desc",
) -> PagedList[T]:
    """Apply cursor-based pagination to an in-memory list.

    Items should be in a stable ascending order before calling.
    The function slices using ``after``/``before`` cursors keyed by
    item id and returns at most ``limit`` items.

    :param items: Pre-sorted items to paginate (ascending order).
    :param id_fn: Callable that extracts the id from an item.
    :param limit: Maximum items to return, default 20.
    :param after: Cursor id — return items after this one.
    :param before: Cursor id — return items before this one.
    :param order: ``"desc"`` reverses the list, ``"asc"`` keeps it.
    :returns: A paginated :class:`PagedList`.
    """
    working = list(items)
    if order == "desc":
        working = list(reversed(working))

    if after is not None:
        idx = next((i for i, item in enumerate(working) if id_fn(item) == after), None)
        if idx is not None:
            working = working[idx + 1 :]

    if before is not None:
        idx = next((i for i, item in enumerate(working) if id_fn(item) == before), None)
        if idx is not None:
            working = working[:idx]

    has_more = len(working) > limit
    page = working[:limit]
    return PagedList(
        data=page,
        first_id=id_fn(page[0]) if page else None,
        last_id=id_fn(page[-1]) if page else None,
        has_more=has_more,
    )


# ── Item data types ───────────────────────────────────────────────────────────


class _BaseItemData(BaseModel):
    """Shared config for all item data models: ignore unknown fields for forward compat."""

    model_config = ConfigDict(extra="ignore")


class MessageData(_BaseItemData):
    """
    Data for a message item (user or assistant).

    :param role: ``"user"`` or ``"assistant"``.
    :param content: Heterogeneous content blocks, e.g.
        ``[{"type": "input_text", "text": "Hello"}]``.
    :param agent: Agent name (required for assistant messages, absent for user).
        Serialized as ``"model"`` in JSON.
    """

    role: Literal["user", "assistant"]
    content: list[dict[str, Any]]
    agent: str | None = Field(default=None, serialization_alias="model")

    @model_validator(mode="after")
    def _check_agent_for_assistant(self) -> MessageData:
        """
        Validate that assistant messages have an agent and user messages do not.

        :returns: The validated instance.
        :raises ValueError: If an assistant message is missing ``agent``.
        """
        if self.role == "assistant" and self.agent is None:
            raise ValueError("assistant messages require 'agent'")
        return self


class FunctionCallData(_BaseItemData):
    """
    Data for a function_call item.

    :param agent: Agent name. Serialized as ``"model"`` in JSON.
    :param name: Tool function name, e.g. ``"search.web"``.
    :param arguments: JSON-encoded arguments string.
    :param call_id: Unique call identifier from the LLM, e.g. ``"call_abc123"``.
    """

    agent: str = Field(serialization_alias="model")
    name: str
    arguments: str
    call_id: str


class FunctionCallOutputData(_BaseItemData):
    """
    Data for a function_call_output item.

    :param call_id: The call_id this output corresponds to, e.g. ``"call_abc123"``.
    :param output: The tool's string result.
    """

    call_id: str
    output: str


class ReasoningData(_BaseItemData):
    """
    Data for a reasoning item.

    :param agent: Agent name. Serialized as ``"model"`` in JSON.
    :param summary: Summary text blocks, e.g. ``[{"type": "summary_text", "text": "..."}]``.
    :param content: Raw reasoning content blocks, or ``None`` if redacted.
    :param encrypted_content: Encrypted reasoning content, or ``None``.
    """

    agent: str = Field(serialization_alias="model")
    summary: list[dict[str, str]]
    content: list[dict[str, str]] | None = None
    encrypted_content: str | None = None


class CompactionData(_BaseItemData):
    """
    Data payload for a compaction summary item.

    :param summary: LLM-generated summary text covering items up through ``last_item_id``.
    :param last_item_id: Item ID (inclusive) of the last item covered by this summary,
        e.g. ``"msg_abc123"``.
    :param model: Model used to generate the summary, e.g. ``"openai/gpt-4o"``.
    :param token_count: Approximate token count of the summary text.
    """

    summary: str
    last_item_id: str
    model: str
    token_count: int


class NativeToolData(_BaseItemData):
    """
    A provider-native tool output item (e.g. ``web_search_call``).

    :param item: Raw dict from the Responses API output, e.g.
        ``{"type": "web_search_call", "id": "ws_abc", "status": "completed"}``.
    """

    item: dict[str, Any]


ItemData = (
    MessageData
    | FunctionCallData
    | FunctionCallOutputData
    | ReasoningData
    | CompactionData
    | NativeToolData
)

ITEM_TYPE_TO_DATA_CLS: dict[str, type[_BaseItemData]] = {
    "message": MessageData,
    "function_call": FunctionCallData,
    "function_call_output": FunctionCallOutputData,
    "reasoning": ReasoningData,
    "compaction": CompactionData,
    "native_tool": NativeToolData,
}


def parse_item_data(item_type: str, raw: dict[str, Any]) -> ItemData:
    """
    Parse a raw dict into the appropriate ItemData model.

    Used by store implementations when deserializing from DB.

    :param item_type: The item type string, e.g. ``"message"``, ``"function_call"``.
    :param raw: The raw dict from the DB ``data`` column.
    :returns: A validated ItemData instance.
    :raises ValueError: If ``item_type`` is unknown.
    """
    cls = ITEM_TYPE_TO_DATA_CLS.get(item_type)
    if cls is None:
        raise ValueError(f"unknown item type: {item_type!r}")
    # Stores serialize the ``agent`` field under its alias "model"
    # (``model_dump_json(by_alias=True)``). Map it back on read so the value
    # round-trips — ``extra="ignore"`` would otherwise drop the "model" key and
    # every read of a persisted assistant message / function_call / reasoning
    # item would raise. Restricted to classes that have an ``agent`` field but
    # no real ``model`` field, so CompactionData's genuine ``model`` is untouched.
    if "model" in raw and "agent" in cls.model_fields and "model" not in cls.model_fields:
        raw = {**raw, "agent": raw["model"]}
    return cls(**raw)  # type: ignore[return-value]


def _validate_type_matches_data(item_type: str, data: ItemData) -> None:
    """
    Validate that ``data`` is the correct model for ``item_type``.

    :param item_type: The declared type string, e.g. ``"message"``.
    :param data: The data model instance to validate.
    :raises ValueError: If ``item_type`` is unknown or ``data`` is the wrong model.
    """
    expected = ITEM_TYPE_TO_DATA_CLS.get(item_type)
    if expected is None:
        raise ValueError(f"unknown item type: {item_type!r}")
    if not isinstance(data, expected):
        raise ValueError(
            f"item type {item_type!r} requires {expected.__name__}, got {type(data).__name__}"
        )


# ── Conversation entity ───────────────────────────────────────────────────────


@dataclass
class Conversation:
    """
    A conversation grouping related turns.

    :param id: Unique conversation identifier, e.g. ``"conv_abc123"``.
    :param created_at: Unix epoch milliseconds of creation.
    :param updated_at: Unix epoch milliseconds of the last update
        (item append, title change, etc.).
    :param root_conversation_id: For child sub-agent conversations, the id of the
        root (top-level) conversation in the spawn tree. Equal to ``id`` for
        top-level conversations.
    :param title: Optional user-assigned title.
    :param kind: Conversation type. ``"default"`` for user-initiated,
        ``"sub_agent"`` for sub-agent execution conversations.
    :param parent_conversation_id: For child sub-agent conversations, points at
        the owning parent. ``None`` for top-level conversations.
    :param agent_id: Foreign key to the agent bound at creation time. ``None``
        for callers that do not bind a conversation.
    :param session_state: Mutable per-conversation key/value store persisted as JSON.
    :param session_usage: Cumulative LLM token usage. Shape:
        ``{"input_tokens": N, "output_tokens": M, "total_tokens": T}``.
    :param reasoning_effort: Per-session reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param model_override: Per-session LLM model override,
        e.g. ``"claude-opus-4-8"``. ``None`` means use the agent default.
    """

    id: str
    created_at: int
    updated_at: int
    root_conversation_id: str
    title: str | None = None
    kind: str = "default"
    parent_conversation_id: str | None = None
    agent_id: str | None = None
    session_state: dict[str, Any] = field(default_factory=dict)
    session_usage: dict[str, float] = field(default_factory=dict)
    reasoning_effort: str | None = None
    model_override: str | None = None


# ── Conversation items ────────────────────────────────────────────────────────


class NewConversationItem(BaseModel):
    """
    An item that has not yet been persisted. No ID or timestamp.

    :param type: Item type, e.g. ``"message"``, ``"function_call"``.
    :param response_id: The task/response ID this item belongs to.
    :param data: The typed data payload (MessageData, etc.).
    :param created_by: Identity of the human actor who authored this item
        (e.g. ``"alice@example.com"``), or ``None`` for agent/tool/system items.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    response_id: str
    data: ItemData
    created_by: str | None = None

    @model_validator(mode="after")
    def _check_type_matches_data(self) -> NewConversationItem:
        """
        Ensure ``type`` field is consistent with ``data`` model.

        :returns: The validated instance.
        :raises ValueError: If ``type`` does not match ``data``.
        """
        _validate_type_matches_data(self.type, self.data)
        return self


class ConversationItem(BaseModel):
    """
    A persisted item with a store-assigned ID.

    :param id: Store-assigned item ID, e.g. ``"item_abc123"``.
    :param type: Item type, e.g. ``"message"``, ``"function_call"``.
    :param status: Item status, e.g. ``"completed"``.
    :param response_id: The task/response ID this item belongs to.
    :param created_at: Unix epoch milliseconds of creation.
    :param data: The typed data payload (MessageData, etc.).
    :param created_by: Identity of the human actor who authored this item,
        or ``None`` for agent/tool/system items.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    type: str
    status: str
    response_id: str
    created_at: int
    data: ItemData
    created_by: str | None = None

    @model_validator(mode="after")
    def _check_type_matches_data(self) -> ConversationItem:
        """
        Ensure ``type`` field is consistent with ``data`` model.

        :returns: The validated instance.
        :raises ValueError: If ``type`` does not match ``data``.
        """
        _validate_type_matches_data(self.type, self.data)
        return self

    def to_api_dict(self) -> dict[str, Any]:
        """
        Render the item as the flat, JSON-safe shape for API responses.

        Common fields (``id``, ``response_id``, ``type``, ``status``,
        ``created_at``) come from the item; type-specific fields (``role``,
        ``content``, ``model``, ``name``, ``arguments``, …) come from
        ``self.data`` and are spread onto the top level.
        ``exclude_none=True`` drops absent optional fields (e.g. ``model``
        on user messages).

        :returns: Flat dict safe for ``json.dumps``, e.g.::

            {"id": "item_abc", "response_id": "resp_xyz",
             "type": "message", "status": "completed",
             "created_at": 1749456000000,
             "role": "assistant",
             "content": [{"type": "output_text", "text": "hi"}],
             "model": "databricks-claude-sonnet-4-6"}
        """
        return {
            "id": self.id,
            "response_id": self.response_id,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at,
            **self.data.model_dump(exclude_none=True, by_alias=True),
            **({"created_by": self.created_by} if self.created_by is not None else {}),
        }


def drop_orphaned_tool_outputs(
    items: list[ConversationItem],
) -> list[ConversationItem]:
    """Drop a ``function_call_output`` whose ``call_id`` has no matching
    ``function_call`` in ``items``.

    An orphaned tool result — e.g. an approval turn persisted the tool result
    while its ``function_call`` lived only in a checkpointer that is since gone —
    replays as a tool message with no preceding tool call, which the LLM rejects
    (Databricks-Claude 400s the whole request). Well-formed history is unchanged:
    every stored result has its call, so nothing is dropped. Called at the head of
    the two history-replay conversions (ChatAgent + ResponsesAgent).
    """
    call_ids = {
        it.data.call_id
        for it in items
        if it.type == "function_call" and isinstance(it.data, FunctionCallData)
    }
    return [
        it
        for it in items
        if not (
            it.type == "function_call_output"
            and isinstance(it.data, FunctionCallOutputData)
            and it.data.call_id not in call_ids
        )
    ]


def synthesize_conversation_title(
    content: list[dict[str, Any]],
    *,
    limit: int = 60,
) -> str | None:
    """
    Derive a one-line conversation title from message content blocks.

    :param content: Message content blocks, e.g.
        ``[{"type": "input_text", "text": "Hello"}]``.
    :param limit: Max chars before truncating with an ellipsis.
    :returns: Collapsed/truncated title, or ``None`` when no usable text is present.
    """
    parts: list[str] = []
    for block in content:
        if block.get("type") == "input_text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    collapsed = " ".join(" ".join(parts).split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def compute_search_text(item_type: str, data: ItemData) -> str:
    """
    Extract indexable plain text from an item for full-text search.

    :param item_type: The item type string, e.g. ``"message"``.
    :param data: The typed data payload.
    :returns: Plain text to index, or ``""`` for non-searchable item types.
    """
    if isinstance(data, MessageData):
        return " ".join(
            block.get("text", "")
            for block in data.content
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
        )
    if isinstance(data, FunctionCallData):
        return f"{data.name}: {data.arguments}"
    if isinstance(data, FunctionCallOutputData):
        return data.output
    if isinstance(data, ReasoningData):
        return " ".join(
            block.get("text", "") for block in data.summary if isinstance(block, dict)
        )
    if isinstance(data, CompactionData):
        return data.summary
    return ""


# ── Exceptions ────────────────────────────────────────────────────────────────


class ConversationNotFoundError(Exception):
    """
    Raised when a required conversation row is missing.

    Store methods use this when absence is not a benign no-op and the
    route layer must return a typed 404.
    """


# ── Abstract base ─────────────────────────────────────────────────────────────


class ConversationStore(ABC):
    """
    Abstract base for conversation persistence.

    Manages conversations and their items: creation, lookup, paginated
    listing, appending items, full-text search, updates, and deletion.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the conversation store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"memory://"`` or ``"catalog.schema.prefix"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create_conversation(
        self,
        id: str | None = None,
        kind: str = "default",
        title: str | None = None,
        parent_conversation_id: str | None = None,
        agent_id: str | None = None,
    ) -> Conversation:
        """
        Create a new conversation and return it.

        ``root_conversation_id`` is set automatically: for top-level
        conversations (no ``parent_conversation_id``) it equals the new
        ``id``; for child conversations it is inherited from the parent.

        :param id: Caller-specified conversation id, e.g. ``"session_abc123"``.
            When ``None``, the store generates a unique id.
        :param kind: Conversation type. ``"default"`` for user-initiated,
            ``"sub_agent"`` for sub-agent execution conversations.
        :param title: Optional title.
        :param parent_conversation_id: Parent conversation id for child
            conversations. ``None`` for top-level.
        :param agent_id: Agent to bind at creation time. ``None`` for
            callers that cannot bind a conversation.
        :returns: The newly created :class:`Conversation`.
        :raises ConversationNotFoundError: If ``parent_conversation_id``
            is set but the parent row does not exist.
        """
        ...

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation, or ``None`` if it does not exist.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The :class:`Conversation` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """
        Return items in a conversation with cursor-based pagination.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return.
        :param after: Cursor item ID; only return items after this one in
            sort order, e.g. ``"item_xyz789"``.
        :param before: Cursor item ID; only return items before this one in
            sort order.
        :param order: Sort direction, ``"asc"`` (chronological) or ``"desc"``.
        :param type: Optional item type filter, e.g. ``"message"``.
            ``None`` returns all types.
        :returns: A :class:`PagedList` of :class:`ConversationItem` objects.
        """
        ...

    @abstractmethod
    def append(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        """
        Append items to a conversation.

        Assigns a globally unique ID and timestamp to each item.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param items: List of :class:`NewConversationItem` objects to persist.
        :returns: The persisted :class:`ConversationItem` list with
            store-assigned IDs and timestamps.
        :raises ConversationNotFoundError: If the conversation does not exist.
        """
        ...

    @abstractmethod
    def list_conversations(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        kind: str | None = "default",
        agent_id: str | None = None,
        order: str = "desc",
        sort_by: str = "created_at",
        search_query: str | None = None,
    ) -> PagedList[Conversation]:
        """
        List conversations with cursor-based pagination.

        :param limit: Maximum number of conversations to return.
        :param after: Cursor conversation ID; return conversations appearing
            after this one in sort order, e.g. ``"conv_abc123"``.
        :param before: Cursor conversation ID; return conversations appearing
            before this one in sort order.
        :param kind: Filter to conversations of this kind. ``"default"``
            returns only user-initiated. ``None`` disables the filter.
        :param agent_id: When set, only return conversations for this agent.
            ``None`` disables the filter.
        :param order: Sort direction, ``"desc"`` (newest-first) or ``"asc"``.
        :param sort_by: Column to sort on, ``"created_at"`` or ``"updated_at"``.
        :param search_query: Case-insensitive substring filter on title or
            item content. ``None`` disables the filter.
        :returns: A :class:`PagedList` of :class:`Conversation` objects.
        """
        ...

    @abstractmethod
    def search(
        self,
        query: str,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """
        Full-text search over conversation items.

        :param query: The search query string, e.g. ``"deployment error"``.
        :param conversation_id: Optional conversation to scope the search to,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of results to return.
        :returns: A list of matching :class:`ConversationItem` objects.
        """
        ...

    @abstractmethod
    def update_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        reasoning_effort: str | None = None,
        _unset_reasoning_effort: bool = False,
        model_override: str | None = None,
        _unset_model_override: bool = False,
    ) -> Conversation | None:
        """
        Update mutable fields on a conversation.

        For ``reasoning_effort`` and ``model_override``, ``None`` means
        "leave unchanged". To explicitly clear them back to ``None``, pass
        the matching ``_unset_*`` flag.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param title: New title, or ``None`` to leave unchanged.
        :param reasoning_effort: Per-session reasoning effort hint,
            e.g. ``"high"``. ``None`` leaves unchanged.
        :param _unset_reasoning_effort: When ``True``, set ``reasoning_effort``
            to ``None`` regardless of the ``reasoning_effort`` param value.
        :param model_override: Per-session LLM model override,
            e.g. ``"claude-opus-4-8"``. ``None`` leaves unchanged.
        :param _unset_model_override: When ``True``, set ``model_override`` to
            ``None`` regardless of the ``model_override`` param value.
        :returns: The updated :class:`Conversation`, or ``None`` if it does
            not exist.
        """
        ...

    @abstractmethod
    def set_session_state(
        self, conversation_id: str, session_state: dict[str, Any]
    ) -> None:
        """Overwrite the conversation's persisted session_state (full replacement).

        ``session_state`` must be JSON-serializable. No-op when the conversation
        does not exist (parity with a SQL ``UPDATE`` matching zero rows).
        """
        ...

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation and all its items.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` if the conversation existed and was deleted,
            ``False`` if it did not exist.
        """
        ...


# ── In-memory implementation ──────────────────────────────────────────────────


def _new_id(prefix: str) -> str:
    """
    Generate a time-sortable unique id with the given prefix.

    :param prefix: Short label for the entity type, e.g. ``"conv"`` or ``"item"``.
    :returns: An id like ``"conv_01949b5a4f80..."``.
    """
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(8)
    return f"{prefix}_{ts:013x}{rand}"


def _now_ms() -> int:
    """Return the current Unix epoch in milliseconds."""
    return int(time.time() * 1000)


class InMemoryConversationStore(ConversationStore):
    """
    In-process conversation store backed by plain dicts and lists.

    Suitable for tests and single-process development; not durable
    across restarts. Thread-safe.
    """

    def __init__(self, storage_location: str = "memory://") -> None:
        """Initialize the in-memory store with empty state."""
        super().__init__(storage_location=storage_location)
        self._conversations: dict[str, Conversation] = {}
        # Items stored in insertion (ascending position) order.
        self._items: dict[str, list[ConversationItem]] = {}
        self._lock = threading.Lock()

    def create_conversation(
        self,
        id: str | None = None,
        kind: str = "default",
        title: str | None = None,
        parent_conversation_id: str | None = None,
        agent_id: str | None = None,
    ) -> Conversation:
        """
        Create and return a new conversation.

        :param id: Caller-specified conversation id, e.g. ``"session_abc123"``.
            When ``None``, a unique id is generated.
        :param kind: Conversation kind, e.g. ``"default"``.
        :param title: Optional title.
        :param parent_conversation_id: Parent id for child conversations.
        :param agent_id: Optional agent id to bind.
        :returns: The newly created :class:`Conversation`.
        :raises ConversationNotFoundError: If ``parent_conversation_id`` is
            set but the parent row does not exist.
        """
        with self._lock:
            now = _now_ms()
            cid = id if id is not None else _new_id("conv")
            root_id = self._resolve_root_id(cid, parent_conversation_id)
            conv = Conversation(
                id=cid,
                created_at=now,
                updated_at=now,
                root_conversation_id=root_id,
                title=title,
                kind=kind,
                parent_conversation_id=parent_conversation_id,
                agent_id=agent_id,
            )
            self._conversations[cid] = conv
            self._items[cid] = []
            return conv

    def _resolve_root_id(self, new_id: str, parent_id: str | None) -> str:
        """
        Resolve ``root_conversation_id`` for a new conversation.

        :param new_id: The newly generated conversation id.
        :param parent_id: Parent conversation id, or ``None`` for top-level.
        :returns: ``new_id`` for top-level, or the parent's ``root_conversation_id``
            for child conversations.
        :raises ConversationNotFoundError: If ``parent_id`` is set but the
            parent row does not exist.
        """
        if parent_id is None:
            return new_id
        parent = self._conversations.get(parent_id)
        if parent is None:
            raise ConversationNotFoundError(
                f"parent conversation {parent_id!r} not found"
            )
        return parent.root_conversation_id

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation, or ``None`` if it does not exist.

        :param conversation_id: Unique conversation identifier.
        :returns: The :class:`Conversation` or ``None``.
        """
        return self._conversations.get(conversation_id)

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """
        Return items in a conversation with cursor-based pagination.

        :param conversation_id: Unique conversation identifier.
        :param limit: Maximum items to return.
        :param after: Cursor item ID; return items after this one.
        :param before: Cursor item ID; return items before this one.
        :param order: ``"asc"`` or ``"desc"``.
        :param type: Optional type filter, e.g. ``"message"``.
        :returns: A :class:`PagedList` of :class:`ConversationItem`.
        """
        items = list(self._items.get(conversation_id, []))
        if type is not None:
            items = [i for i in items if i.type == type]
        return paginate_in_memory(
            items, id_fn=lambda i: i.id, limit=limit, after=after, before=before, order=order
        )

    def append(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        """
        Append items to a conversation and return the persisted items.

        :param conversation_id: Unique conversation identifier.
        :param items: Items to persist.
        :returns: Persisted items with store-assigned IDs and timestamps.
        :raises ConversationNotFoundError: If the conversation does not exist.
        """
        with self._lock:
            if conversation_id not in self._conversations:
                raise ConversationNotFoundError(
                    f"conversation {conversation_id!r} not found"
                )
            now = _now_ms()
            persisted = [
                ConversationItem(
                    id=_new_id("item"),
                    type=item.type,
                    status="completed",
                    response_id=item.response_id,
                    created_at=now,
                    data=item.data,
                    created_by=item.created_by,
                )
                for item in items
            ]
            self._items[conversation_id].extend(persisted)
            self._conversations[conversation_id] = replace(
                self._conversations[conversation_id], updated_at=now
            )
            return persisted

    def list_conversations(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        kind: str | None = "default",
        agent_id: str | None = None,
        order: str = "desc",
        sort_by: str = "created_at",
        search_query: str | None = None,
    ) -> PagedList[Conversation]:
        """
        List conversations with cursor-based pagination.

        :param limit: Maximum conversations to return.
        :param after: Cursor conversation ID; return conversations after this one.
        :param before: Cursor conversation ID; return conversations before this one.
        :param kind: Kind filter. ``None`` disables the filter.
        :param agent_id: Agent id filter. ``None`` disables the filter.
        :param order: ``"desc"`` (newest-first) or ``"asc"``.
        :param sort_by: ``"created_at"`` or ``"updated_at"``.
        :param search_query: Substring filter on title or item content.
        :returns: A :class:`PagedList` of :class:`Conversation`.
        """
        convs = list(self._conversations.values())
        if kind is not None:
            convs = [c for c in convs if c.kind == kind]
        if agent_id is not None:
            convs = [c for c in convs if c.agent_id == agent_id]
        if search_query:
            convs = self._filter_by_search(convs, search_query)

        key_fn = (lambda c: c.updated_at) if sort_by == "updated_at" else (lambda c: c.created_at)
        convs.sort(key=key_fn)

        return paginate_in_memory(
            convs, id_fn=lambda c: c.id, limit=limit, after=after, before=before, order=order
        )

    def _filter_by_search(self, convs: list[Conversation], query: str) -> list[Conversation]:
        """
        Filter conversations by title or item content substring match.

        :param convs: Conversations to filter.
        :param query: Case-insensitive query string.
        :returns: Conversations matching the query.
        """
        q = query.lower()
        title_ids = {c.id for c in convs if c.title and q in c.title.lower()}
        item_ids: set[str] = set()
        for conv in convs:
            for item in self._items.get(conv.id, []):
                if q in compute_search_text(item.type, item.data).lower():
                    item_ids.add(conv.id)
                    break
        matching = title_ids | item_ids
        return [c for c in convs if c.id in matching]

    def search(
        self,
        query: str,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """
        Full-text search over conversation items.

        :param query: Case-insensitive search string.
        :param conversation_id: Optional scope to one conversation.
        :param limit: Maximum results to return.
        :returns: Matching :class:`ConversationItem` objects.
        """
        q = query.lower()
        scope = (
            {conversation_id: self._items.get(conversation_id, [])}
            if conversation_id is not None
            else self._items
        )
        results: list[ConversationItem] = []
        for items in scope.values():
            for item in items:
                if q in compute_search_text(item.type, item.data).lower():
                    results.append(item)
                    if len(results) >= limit:
                        return results
        return results

    def update_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        reasoning_effort: str | None = None,
        _unset_reasoning_effort: bool = False,
        model_override: str | None = None,
        _unset_model_override: bool = False,
    ) -> Conversation | None:
        """
        Update mutable fields on a conversation and return the updated row.

        :param conversation_id: Unique conversation identifier.
        :param title: New title, or ``None`` to leave unchanged.
        :param reasoning_effort: New reasoning effort, or ``None`` to leave unchanged.
        :param _unset_reasoning_effort: When ``True``, clear ``reasoning_effort`` to ``None``.
        :param model_override: New model override, or ``None`` to leave unchanged.
        :param _unset_model_override: When ``True``, clear ``model_override`` to ``None``.
        :returns: Updated :class:`Conversation`, or ``None`` if not found.
        """
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return None
            new_effort = None if _unset_reasoning_effort else (reasoning_effort or conv.reasoning_effort)
            new_model = None if _unset_model_override else (model_override or conv.model_override)
            updated = replace(
                conv,
                updated_at=_now_ms(),
                title=title if title is not None else conv.title,
                reasoning_effort=new_effort,
                model_override=new_model,
            )
            self._conversations[conversation_id] = updated
            return updated

    def set_session_state(
        self, conversation_id: str, session_state: dict[str, Any]
    ) -> None:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return  # no-op for unknown conversation (SQL-UPDATE parity)
            self._conversations[conversation_id] = replace(
                conv, session_state=dict(session_state), updated_at=_now_ms()
            )

    def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation and all its items.

        :param conversation_id: Unique conversation identifier.
        :returns: ``True`` if found and deleted, ``False`` if not found.
        """
        with self._lock:
            if conversation_id not in self._conversations:
                return False
            del self._conversations[conversation_id]
            self._items.pop(conversation_id, None)
            return True
