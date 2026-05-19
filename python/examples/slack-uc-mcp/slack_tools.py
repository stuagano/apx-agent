"""apx-agent tool factories for Slack via UC u2m per-user connections.

Wraps the raw `ws.serving_endpoints.http_request(conn="slack_mcp", ...)` call
pattern into proper apx-agent tools that:

  * Declare the UC connection as a `ResourceSpec` so `log_agent` auto-emits the
    serving-endpoint resource and the platform-minted token has the right scope.
  * Resolve `user_identity` from the calling user's request context (the OBO
    header chain), so the agent doesn't have to plumb it manually.
  * Surface Slack API errors as structured tool output the LLM can act on
    (e.g., `not_in_channel` → suggest joining the channel rather than retrying
    under the SP's identity).

Usage::

    from apx_agent import Agent
    from examples.slack_uc_mcp.slack_tools import slack_history_tool, slack_post_tool

    agent = Agent(
        instructions="You answer questions from Slack channels.",
        tools=[
            slack_history_tool(connection="slack_mcp"),
            slack_post_tool(connection="slack_mcp"),
        ],
    )

The connection name (`slack_mcp` here) and the per-user credentials must be
provisioned in Unity Catalog before the tool can call out — see this
directory's README for the one-time admin setup.

Private preview: the UC u2m surface is currently in Private Preview on AWS,
Azure, and GCP. Endpoints, JSON shapes, and CLI surfaces may change before GA.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_user_identity() -> str | None:
    """Pull the Slack `user_identity` from the calling user's request context.

    The request context is set by apx-agent's chat-agent / Apps wiring when
    an OBO header chain arrives. The `slack_user_identity` key is the
    application-level mapping from the calling Databricks user to their
    Slack user ID / email — typically set by the calling app's session
    bootstrap or by a UC table lookup.

    Falls back to the `SLACK_USER_IDENTITY` env var for batch / dev use.
    """
    try:
        from apx_agent import get_request_context  # type: ignore[attr-defined]

        ctx = get_request_context()
        if ctx and isinstance(ctx, dict):
            ident = ctx.get("slack_user_identity")
            if isinstance(ident, str) and ident:
                return ident
    except Exception:  # pragma: no cover — request context optional
        pass
    return os.environ.get("SLACK_USER_IDENTITY")


def _slack_call(
    *,
    connection: str,
    path: str,
    body: dict[str, Any],
    user_identity: str | None,
    ws: Any | None = None,
) -> dict[str, Any]:
    """Perform a single Slack API call via the UC u2m connection.

    The agent runs as the SP; UC vends the right user's Slack token at call
    time based on the `with-user-identity` header.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import ExternalFunctionRequestHttpMethod

    if ws is None:
        ws = WorkspaceClient(
            client_id=os.environ.get("SP_CLIENT_ID"),
            client_secret=os.environ.get("SP_CLIENT_SECRET"),
        )

    if not user_identity:
        return {
            "ok": False,
            "error": "missing_user_identity",
            "message": (
                "No slack_user_identity in the calling user's request context. "
                "Set it via the calling app, or set SLACK_USER_IDENTITY for batch use."
            ),
        }

    response = ws.serving_endpoints.http_request(
        conn=connection,
        method=ExternalFunctionRequestHttpMethod.POST,
        path=path,
        json=body,
        headers={"with-user-identity": user_identity},
    )
    try:
        payload: dict[str, Any] = response.json()
    except Exception as e:  # pragma: no cover — defensive
        return {"ok": False, "error": "invalid_json", "message": str(e)}
    return payload


def slack_history_tool(
    *,
    connection: str = "slack_mcp",
    name: str = "slack_history",
    description: str | None = None,
    default_limit: int = 5,
) -> Any:
    """Return a tool that reads recent Slack channel messages as the calling user.

    The agent (running as the SP) calls Slack via `serving_endpoints.http_request`
    against the UC u2m connection; UC vends the right user's Slack token at
    call time. The user_identity is resolved from the request context, falling
    back to the `SLACK_USER_IDENTITY` env var.

    The LLM sees two parameters — `channel_id` and `limit` — but the user
    identity, SP credentials, and connection routing are all handled by the
    framework + UC.

    Args:
        connection: UC connection name. Defaults to ``"slack_mcp"``.
        name: Tool name shown to the LLM. Defaults to ``"slack_history"``.
        description: Tool description. A sensible default is provided.
        default_limit: Number of messages when ``limit`` is omitted.
    """
    from apx_agent._resources import ResourceSpec, attach_resources

    _desc = (
        description
        or "Read recent messages from a Slack channel as the calling user. "
        "Use this when the user asks 'what did anyone say about X in #channel?'. "
        "Returns a dict with `messages` (list) or `error` (string) keys."
    )

    def _slack_history(channel_id: str, limit: int = default_limit) -> dict[str, Any]:
        """Placeholder — overwritten below."""
        user_identity = _resolve_user_identity()
        return _slack_call(
            connection=connection,
            path="/api/conversations.history",
            body={"channel": channel_id, "limit": limit},
            user_identity=user_identity,
        )

    _slack_history.__name__ = name
    _slack_history.__qualname__ = name
    _slack_history.__doc__ = _desc

    attach_resources(
        _slack_history,
        [ResourceSpec("serving_endpoint", f"connection:{connection}")],
    )
    return _slack_history


def slack_post_tool(
    *,
    connection: str = "slack_mcp",
    name: str = "slack_post",
    description: str | None = None,
) -> Any:
    """Return a tool that posts a Slack message as the calling user.

    Same UC u2m pattern as ``slack_history_tool`` — agent runs as SP, UC
    vends the user's Slack token. The message lands in Slack attributed to
    the calling user, not the SP.

    Args:
        connection: UC connection name. Defaults to ``"slack_mcp"``.
        name: Tool name shown to the LLM. Defaults to ``"slack_post"``.
        description: Tool description. A sensible default is provided.
    """
    from apx_agent._resources import ResourceSpec, attach_resources

    _desc = (
        description
        or "Post a message to a Slack channel as the calling user. The message "
        "appears in Slack attributed to the user, not to any service principal. "
        "Returns a dict with `ok` and either `ts` (message timestamp) or "
        "`error` keys."
    )

    def _slack_post(channel_id: str, text: str) -> dict[str, Any]:
        """Placeholder — overwritten below."""
        user_identity = _resolve_user_identity()
        return _slack_call(
            connection=connection,
            path="/api/chat.postMessage",
            body={"channel": channel_id, "text": text},
            user_identity=user_identity,
        )

    _slack_post.__name__ = name
    _slack_post.__qualname__ = name
    _slack_post.__doc__ = _desc

    attach_resources(
        _slack_post,
        [ResourceSpec("serving_endpoint", f"connection:{connection}")],
    )
    return _slack_post
