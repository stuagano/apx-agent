"""Eval bridge — factory for MLflow evaluation predict functions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

def app_predict_fn(url: str, token: str | None = None) -> Callable[[dict[str, Any]], str]:
    """Return a predict function for mlflow.genai.evaluate().

    ``token`` is a Databricks personal access token or OBO token used to
    authenticate against the deployed Databricks App. When omitted, no
    Authorization header is sent (suitable for local dev or public endpoints).

    Example::

        from apx_agent import app_predict_fn

        predict = app_predict_fn(
            "https://my-agent.my-workspace.databricksapps.com",
            token=dbutils.secrets.get("my-scope", "pat"),
        )
        results = mlflow.genai.evaluate(
            data=eval_dataset,
            predict_fn=predict,
            scorers=[correctness_scorer],
        )

    The predict function accepts a dict with a "messages" key (list of message
    dicts) or a plain string, posts to the agent's /responses endpoint, and
    returns the response text.
    """
    import httpx

    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    def predict(inputs: dict[str, Any]) -> str:
        if isinstance(inputs, str):
            messages = [{"role": "user", "content": inputs}]
        else:
            messages = inputs.get("messages") or [
                {"role": "user", "content": str(inputs.get("input", inputs))}
            ]

        response = httpx.post(
            f"{base}/responses",
            json={"input": messages},
            headers=headers,
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()
        try:
            return data["output"][0]["content"][0]["text"]
        except (KeyError, IndexError):
            return str(data)

    return predict
