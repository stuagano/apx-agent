"""Conversation history for Strands, stored in the managed Session Store (replaces AgentCore Memory).

Each Strands message is one Session Store item, replayed oldest first. Without AGENT_SESSION_STORE
it falls back to an in-process dict (fine for local runs, lost on restart).
"""

import asyncio
import os

from databricks.sdk.errors import NotFound
from databricks_agentkit.runtime.session_store_client import SessionStoreClient

STORE = os.environ.get("AGENT_SESSION_STORE")
_local: dict[str, list] = {}


def _session(client: SessionStoreClient, session_id: str, actor: str):
    try:
        return client.get_session(session_id=session_id)
    except NotFound:
        return client.create_session(actor_id=actor, session_id=session_id, metadata={"client": "strands"})


async def load(session_id: str, actor: str) -> list:
    if not STORE:
        return list(_local.get(session_id, []))

    def _read():
        client = SessionStoreClient().set_session_store(STORE)
        items = client.list_items(_session(client, session_id, actor), order_by="create_time asc")
        return [item.data for item in items]

    return await asyncio.get_running_loop().run_in_executor(None, _read)


async def append(session_id: str, actor: str, messages: list) -> None:
    if not messages:
        return
    if not STORE:
        _local.setdefault(session_id, []).extend(messages)
        return

    def _write():
        client = SessionStoreClient().set_session_store(STORE)
        client.append_items(_session(client, session_id, actor), items=messages)

    await asyncio.get_running_loop().run_in_executor(None, _write)
