"""slack-agent: Databricks identity agent reachable from Slack slash commands.

The Slack webhook surface lives in ``webhook.py`` and the stored OAuth tokens in
``token_store.py``; ``agent_server/start_server.py`` mounts the webhook next to
``/invocations`` and ``/mcp``. Slack hits ``/slack/events`` with a slash-command
payload, the webhook validates the signature, looks up the stored Databricks
token, and calls this agent via in-process HTTP loopback with
``X-Forwarded-Access-Token`` set so ``Dependencies.UserClient`` sees the real user.
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
    instructions="You are a helpful assistant connected to Databricks. "
                 "When asked who the user is or what account they are using, call who_am_i.",
    tools=[who_am_i],
)
