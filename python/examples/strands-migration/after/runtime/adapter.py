"""Translate DurableAgentServer invocations to the unchanged Strands agent.

Old contract (AgentCore): POST {prompt} -> {result}.
New contract: POST /api/invocations {id, session_id, input: {prompt}} -> output {result}.
"""

import os

from databricks_agentkit import InvocationContext

from runtime import history
from runtime.model import databricks_model
from support_agent.agent import build_agent

# Demo-only: with AGENT_ENABLE_CRASH_TEST=1, an input of {"crash_test": true} kills the worker after
# the agent answers but before history is saved, on the first attempt only. Off by default.
CRASH_TEST_ENABLED = os.environ.get("AGENT_ENABLE_CRASH_TEST") == "1"


async def _run(value: dict, context: InvocationContext, *, crash_ok: bool) -> dict:
    if not context.session_id:
        raise ValueError("session_id is required")
    # ponytail: actor = session_id. Production should use the calling principal so users stay separate.
    actor = context.session_id
    prior = await history.load(context.session_id, actor)
    agent = build_agent(databricks_model(), messages=prior)
    start = len(agent.messages)

    async for event in agent.stream_async(value["prompt"]):
        if "data" in event:
            await context.emit({"type": "delta", "content": event["data"]})
    if crash_ok and CRASH_TEST_ENABLED and value.get("crash_test"):
        os._exit(1)

    # Save the turn only once it finished, so a replay after a crash never duplicates history.
    await history.append(context.session_id, actor, agent.messages[start:])
    reply = agent.messages[-1]["content"] if agent.messages else []
    return {"result": "".join(block["text"] for block in reply if "text" in block)}


async def invoke(value: dict, context: InvocationContext) -> dict:
    return await _run(value, context, crash_ok=True)


async def recover(value: dict, context: InvocationContext) -> dict:
    # Strands has no per-step checkpoint, so recovery replays the whole turn.
    # ponytail: tools re-run on replay; make write tools idempotent, or checkpoint per tool call.
    result = await _run(value, context, crash_ok=False)
    return {**result, "recovered": True}
