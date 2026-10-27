"""Allow-listed session state for durable composites (docs/design/composite-session-keys.md).

One managed Session per agent/user/session thread (suffix "apx-state"); each completed turn
appends a snapshot of the allow-listed keys. No graph checkpointer is involved."""

from __future__ import annotations

from typing import Any

MAX_SNAPSHOTS = 20
_ITEM_TYPE = "apx_session_state"


def _session(client: Any, thread_id: str) -> Any:
    from databricks.sdk.errors import NotFound
    from databricks_agentkit.langgraph.session_store import _session_id

    session_id = _session_id(thread_id, "apx-state")
    try:
        return client.get_session(session_id=session_id)
    except NotFound:
        return client.create_session(actor_id=thread_id, session_id=session_id,
                                     metadata={"apx_state_thread_id": thread_id})


def _snapshots(client: Any, session: Any) -> list[dict[str, Any]]:
    return [item.data["state"] for item in client.list_items(session)
            if isinstance(item.data, dict) and item.data.get("type") == _ITEM_TYPE
            and isinstance(item.data.get("state"), dict)]


def load_session_state(client: Any, thread_id: str, keys: tuple[str, ...]) -> dict[str, Any]:
    """The latest snapshot's allow-listed keys, or {} for a new session."""
    snapshots = _snapshots(client, _session(client, thread_id))
    return {k: v for k, v in snapshots[-1].items() if k in keys} if snapshots else {}


def save_session_state(client: Any, thread_id: str, keys: tuple[str, ...], state: dict[str, Any]) -> None:
    """Append a snapshot of the allow-listed keys; compact past MAX_SNAPSHOTS."""
    session = _session(client, thread_id)
    item = {"type": _ITEM_TYPE, "state": {k: state[k] for k in keys if k in state}}
    if len(_snapshots(client, session)) >= MAX_SNAPSHOTS:
        client.clear_items(session)  # ponytail: last-write-wins; the SDK serializes per session
    client.append_items(session, items=[item])
