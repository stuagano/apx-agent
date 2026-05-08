# In-memory store: Slack user ID → Databricks access token.
# Single-process safe. Resets on redeploy — for production use one of:
#   Option B: slack_bolt InstallationStore (e.g. FileInstallationStore)
#   Option C: Delta table via WorkspaceClient SQL execution

_store: dict[str, str] = {}


def get_token(slack_user_id: str) -> str | None:
    return _store.get(slack_user_id)


def set_token(slack_user_id: str, access_token: str) -> None:
    _store[slack_user_id] = access_token


def clear_token(slack_user_id: str) -> None:
    _store.pop(slack_user_id, None)
