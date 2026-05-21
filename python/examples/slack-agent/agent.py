"""slack-agent root agent — ADK-style top-level definition.

The Slack webhook surface lives in ``webhook.py``; ``agent_server/start_server.py``
mounts it next to ``/invocations`` and ``/mcp``. Slack hits ``/slack/events`` with
a slash-command payload, the webhook validates the signature + looks up the
stored Databricks token, then calls this agent via the in-process HTTP loopback
with ``X-Forwarded-Access-Token`` set so ``Dependencies.UserClient`` sees the
real user.
"""
from __future__ import annotations

from apx_agent import Agent, Dependencies


def who_am_i(ws: Dependencies.UserClient) -> str:
    """Return the identity of the current Databricks user.

    When called from the browser, ``Dependencies.UserClient`` reads
    ``X-Forwarded-Access-Token`` injected automatically by the Databricks Apps
    proxy. When called from Slack, the webhook injects the stored OAuth token
    into that same header before calling ``/invocations`` — the agent sees no
    difference.
    """
    user = ws.current_user.me()
    return f"{user.display_name} ({user.user_name})"


agent = Agent(
    tools=[who_am_i],
    instructions=(
        "You are a helpful assistant connected to Databricks. "
        "When asked who the user is or what account they are using, call who_am_i."
    ),
)
