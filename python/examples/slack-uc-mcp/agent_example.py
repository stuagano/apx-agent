"""Reference: call Slack through a UC u2m connection as the calling user.

Private preview. The connection (`slack_mcp`) and the user credentials must
already exist — see this directory's README for setup. This script is the
runtime side only: agent (running as SP) reads Slack for a given user_identity.

Run from inside a Databricks App, or set SP_CLIENT_ID / SP_CLIENT_SECRET in
your environment if invoking locally.
"""

from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ExternalFunctionRequestHttpMethod


def slack_history(channel_id: str, user_identity: str) -> dict:
    """Read the last messages from a Slack channel as the named user.

    user_identity is the same string posted to UC's user-credentials API
    during the user's OAuth callback — typically a Slack user ID or email.

    UC vends that user's Slack access token onto the outbound call. If the
    user isn't in the channel, Slack returns not_in_channel and we surface
    it raw — the agent should not retry under the SP's identity.
    """
    ws = WorkspaceClient(
        client_id=os.environ["SP_CLIENT_ID"],
        client_secret=os.environ["SP_CLIENT_SECRET"],
    )
    response = ws.serving_endpoints.http_request(
        conn="slack_mcp",
        method=ExternalFunctionRequestHttpMethod.POST,
        path="/api/conversations.history",
        json={"channel": channel_id, "limit": 5},
        headers={"with-user-identity": user_identity},
    )
    return response.json()


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 3:
        print("Usage: agent_example.py <channel_id> <user_identity>")
        sys.exit(1)

    channel_id, user_identity = sys.argv[1], sys.argv[2]
    print(json.dumps(slack_history(channel_id, user_identity), indent=2))
