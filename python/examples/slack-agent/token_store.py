# In-memory store: Slack user ID -> Databricks access token.
#
# DEPLOY BLOCKER — demo only. This dict is single-process, resets on redeploy,
# has no encryption at rest, and has no refresh. For production use one of:
#   Option B: slack_bolt InstallationStore (e.g. FileInstallationStore)
#   Option C: Delta table via WorkspaceClient SQL execution
#   Option D: UC u2m credential store (see slack-uc-mcp)

from __future__ import annotations

_store: dict[str, str] = {}


def get_token(slack_user_id: str) -> str | None:
    return _store.get(slack_user_id)


def set_token(slack_user_id: str, access_token: str) -> None:
    _store[slack_user_id] = access_token


def clear_token(slack_user_id: str) -> None:
    _store.pop(slack_user_id, None)
