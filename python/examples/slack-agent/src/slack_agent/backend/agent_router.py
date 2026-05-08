from apx_agent import Agent, Dependencies


def who_am_i(ws: Dependencies.UserClient) -> str:
    """Return the identity of the current Databricks user.

    When called from the browser, Dependencies.UserClient reads
    X-Forwarded-Access-Token injected automatically by the Databricks Apps proxy.
    When called from Slack, the Slack handler injects the stored OAuth token
    into that same header before calling /responses — the agent sees no difference.
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
