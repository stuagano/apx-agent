"""Translate runtime invocations to the unchanged RCA graph.

Old contract: POST /rca {tenant, thread_id, question} -> {anomalies, report}.
New contract: POST /api/invocations {id, session_id=<thread_id>, input: {tenant, question}}
              -> output {anomalies, report}.
"""

import contextvars
import os
from typing import Any

from databricks_agentkit import InvocationContext
from databricks_agentkit.langgraph import checkpointer, start_trace, thread_config
from databricks_agentkit.langgraph.session_store import (
    invocation_id_from_metadata,
    invocation_metadata,
)
from langchain_core.messages import AIMessageChunk

from rca import graph as rca_graph

# Demo-only: with AGENT_ENABLE_CRASH_TEST=1, an input of {"crash_test": true} kills the worker just
# before `analyze` on the first attempt (never on recovery), to show checkpoint-resume recovery.
# Off by default so a request body alone can never take a worker down.
CRASH_TEST_ENABLED = os.environ.get("AGENT_ENABLE_CRASH_TEST") == "1"
CRASH_TEST = contextvars.ContextVar("crash_test", default=False)


def _crash_hook() -> None:
    if CRASH_TEST_ENABLED and CRASH_TEST.get():
        os._exit(1)


rca_graph.BEFORE_ANALYZE = _crash_hook
GRAPH = rca_graph.build_graph(checkpointer())


def _config(context: InvocationContext, tenant: str) -> dict:
    if not context.session_id:
        raise ValueError("session_id (the old thread_id) is required")
    # ponytail: tenant comes from the payload, so the caller asserts it. Production should derive it from
    # the verified caller, e.g. one service principal per tenant (docs/safety/identity-passthrough.md).
    config = thread_config(context.session_id, tenant)
    config["metadata"] = invocation_metadata(context.invocation_id)
    return config


async def _run(agent_input: Any, config: dict, context: InvocationContext) -> dict:
    with start_trace(name="rca", inputs=agent_input, session_id=context.session_id):
        async for mode, payload in GRAPH.astream(
            agent_input, config, stream_mode=["updates", "messages"], durability="sync"
        ):
            if mode == "updates":
                await context.emit({"type": "step", "nodes": list(payload)})
            elif mode == "messages" and isinstance(payload[0], AIMessageChunk) and payload[0].content:
                await context.emit({"type": "delta", "content": payload[0].content})
    state = (await GRAPH.aget_state(config)).values
    messages = state.get("messages") or []
    report = messages[-1].content if messages and messages[-1].type == "ai" else None
    return {"anomalies": state.get("anomalies", []), "report": report}


DEFAULT_QUESTION = "Why are KPIs off?"  # the old /rca endpoint's default


def _input(value: dict) -> dict:
    return {"messages": [("user", value.get("question", DEFAULT_QUESTION))], "tenant": value["tenant"]}


async def invoke(value: dict, context: InvocationContext) -> dict:
    CRASH_TEST.set(bool(value.get("crash_test")))
    return await _run(_input(value), _config(context, value["tenant"]), context)


async def recover(value: dict, context: InvocationContext) -> dict:
    config = _config(context, value["tenant"])
    checkpoint = await GRAPH.checkpointer.aget_tuple(config)
    resumed = bool(checkpoint and invocation_id_from_metadata(checkpoint.metadata) == context.invocation_id)
    result = await _run(None if resumed else _input(value), config, context)
    return {**result, "recovered": True, "resumed_from_checkpoint": resumed}
