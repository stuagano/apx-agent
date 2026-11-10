"""Point Strands at a Databricks model through the OpenAI-compatible serving endpoint.

Replaces BedrockModel. Auth is whatever identity the process has: the app's service principal on
Apps, the CLI profile locally. No keys in code.
"""

import os
from functools import cache
from typing import Any

from databricks.sdk import WorkspaceClient
from strands.models.openai import OpenAIModel

# Pay-per-token FMAPI endpoint name; see "Foundation Model APIs: supported models" in the Databricks docs.
DEFAULT_MODEL = "databricks-claude-sonnet-5-5"
MODEL = os.environ.get("AGENT_MODEL", DEFAULT_MODEL)


@cache
def _client() -> WorkspaceClient:
    return WorkspaceClient()


class FMAPIModel(OpenAIModel):
    """FMAPI can stream `delta.content` as a list of parts; Strands expects a string."""

    def format_chunk(self, event: dict[str, Any], **kwargs: Any):
        data = event.get("data")
        if isinstance(data, list):
            event = {**event, "data": "".join(p["text"] for p in data if isinstance(p, dict) and p.get("type") == "text")}
        return super().format_chunk(event, **kwargs)


def databricks_model() -> OpenAIModel:
    # ponytail: fetch a fresh token per request (Apps OAuth tokens expire); cache if it shows up in latency.
    config = _client().config
    token = config.authenticate()["Authorization"].removeprefix("Bearer ")
    return FMAPIModel(
        client_args={"api_key": token, "base_url": f"{config.host}/serving-endpoints"},
        model_id=MODEL,
        params={"max_tokens": 1024},
    )
