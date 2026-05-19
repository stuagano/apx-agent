"""Eval bridge — run Mosaic AI Agent Evaluation against apx-agent agents.

Two entry points:

  * ``app_predict_fn(url, token)`` — returns a predict function that drives
    a *deployed* Databricks App over HTTP. Use when you want to evaluate
    the production endpoint as-is (auth, network, gateway all in the loop).

  * ``evaluate(agent, model, evalset, ...)`` — compiles the agent
    in-process and runs eval against the compiled ChatAgent directly. No
    HTTP, no deploy. Used for fast feedback during agent authoring and CI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._agents import BaseAgent

# Top-level import so tests can monkeypatch it. Falls back to a lazy import
# stub when the langgraph extra isn't installed (the evaluate function
# raises a friendlier error in that case).
try:
    from ._chat_agent import compile_to_chat_agent
except ImportError:  # pragma: no cover — exercised only without langgraph
    compile_to_chat_agent = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Local evaluation — compile + run in-process
# ---------------------------------------------------------------------------


def _extract_messages(inputs: Any) -> list[dict[str, Any]]:
    """Normalise mlflow.genai eval inputs to a list of message dicts.

    Eval datasets vary: some have ``request``, some have ``input``, some
    have ``messages``, some are bare strings. This pulls the user prompt
    out without forcing the dataset shape.
    """
    if isinstance(inputs, str):
        return [{"role": "user", "content": inputs}]
    if isinstance(inputs, dict):
        if "messages" in inputs and isinstance(inputs["messages"], list):
            return inputs["messages"]
        for key in ("request", "input", "prompt", "query", "question"):
            if key in inputs and inputs[key] is not None:
                return [{"role": "user", "content": str(inputs[key])}]
        return [{"role": "user", "content": str(inputs)}]
    return [{"role": "user", "content": str(inputs)}]


def _extract_response_text(response: Any) -> str:
    """Pull the final assistant message text out of a ChatAgentResponse."""
    messages = getattr(response, "messages", None) or []
    # Walk from the end — the final assistant message is the response.
    for msg in reversed(messages):
        role = getattr(msg, "role", None)
        if role == "assistant":
            content = getattr(msg, "content", None)
            if content:
                return str(content)
    return ""


def _default_scorers() -> list[Any]:
    """Return a sensible default scorer bundle from Mosaic AI Agent Evaluation.

    Imported lazily so the eval extra is only required when actually
    running evaluation, not at apx-agent import time. Falls back to an
    empty list if the scorer classes aren't available in the installed
    mlflow version.
    """
    scorers: list[Any] = []
    try:
        from mlflow.genai.scorers import Correctness, RelevanceToQuery  # type: ignore[attr-defined]
        scorers.extend([Correctness(), RelevanceToQuery()])
    except Exception:
        logger.warning(
            "mlflow.genai.scorers.Correctness / RelevanceToQuery not available; "
            "callers should pass scorers=... explicitly."
        )
    return scorers


def evaluate(
    agent: "BaseAgent",
    *,
    model: str,
    evalset: Any,
    scorers: list[Any] | None = None,
    user_token: str | None = None,
    workspace_host: str | None = None,
    experiment: str | None = None,
    **mlflow_kwargs: Any,
) -> Any:
    """Run Mosaic AI Agent Evaluation against an apx-agent locally.

    Compiles ``agent`` to a ChatAgent once and dispatches each evalset
    entry through the compiled graph in-process. No deployment, no HTTP
    roundtrips — fast feedback during authoring and CI.

    Args:
        agent: The apx-agent ``BaseAgent`` to evaluate.
        model: Databricks serving endpoint name for the LLM.
        evalset: Eval dataset. Accepts anything ``mlflow.genai.evaluate``
            accepts: a pandas DataFrame, a list of dicts, a path/URI to a
            CSV/JSON/Parquet/MLflow dataset, etc. The wrapper tolerates
            varied column names (``request`` / ``input`` / ``prompt`` /
            ``messages``) when extracting the user prompt.
        scorers: List of Mosaic AI Agent Evaluation scorers to run.
            Defaults to ``[Correctness(), RelevanceToQuery()]`` when
            available in the installed mlflow.
        user_token: Optional OBO token. When provided, every evaluated
            request runs as that user — the compiled graph's tools see
            the user's UC grants. When omitted, evaluation runs as the
            default workspace identity (SP via env vars, or CLI auth).
        workspace_host: Optional workspace host for the OBO token. Required
            alongside ``user_token`` if ``DATABRICKS_HOST`` isn't in the
            environment.
        experiment: Optional MLflow experiment name (path or numeric id).
            When set, ``mlflow.set_experiment(experiment)`` is called
            before the eval run so results land in that experiment.
        **mlflow_kwargs: Forwarded verbatim to ``mlflow.genai.evaluate``.

    Returns:
        Whatever ``mlflow.genai.evaluate`` returns (typically a
        ``mlflow.models.EvaluationResult``).

    Requires the ``eval`` and ``langgraph`` extras::

        pip install 'apx-agent[eval,langgraph]'
    """
    try:
        import mlflow
    except ImportError as e:  # pragma: no cover — exercised only without extra
        raise ImportError(
            "evaluate requires mlflow. Install with: pip install 'apx-agent[eval]'"
        ) from e

    if experiment:
        try:
            mlflow.set_experiment(experiment)
        except Exception as e:
            raise RuntimeError(
                f"mlflow.set_experiment({experiment!r}) failed: {e}. "
                f"For Databricks-hosted MLflow, experiment names are workspace "
                f"paths (e.g. '/Users/you@company.com/agents/my_agent')."
            ) from e

    if compile_to_chat_agent is None:
        raise ImportError(
            "evaluate requires the langgraph extra. "
            "Install with: pip install 'apx-agent[eval,langgraph]'"
        )

    chat_agent = compile_to_chat_agent(agent, model=model)

    custom_inputs: dict[str, Any] | None = None
    if user_token:
        custom_inputs = {"user_token": user_token}
        if workspace_host:
            custom_inputs["workspace_host"] = workspace_host

    def _predict(inputs: Any) -> str:
        from mlflow.types.agent import ChatAgentMessage

        msg_dicts = _extract_messages(inputs)
        chat_messages = [
            ChatAgentMessage(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                id=m.get("id"),
            )
            for m in msg_dicts
        ]
        response = chat_agent.predict(chat_messages, custom_inputs=custom_inputs)
        return _extract_response_text(response)

    eval_scorers = scorers if scorers is not None else _default_scorers()

    return mlflow.genai.evaluate(  # type: ignore[attr-defined]
        data=evalset,
        predict_fn=_predict,
        scorers=eval_scorers,
        **mlflow_kwargs,
    )
