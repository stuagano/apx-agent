"""Eval bridge — run Mosaic AI Agent Evaluation against apx-agent agents.

Three entry points:

  * ``app_predict_fn(url, token)`` — returns a predict function that drives
    a *deployed* Databricks App over HTTP. Use when you want to evaluate
    the production endpoint as-is (auth, network, gateway all in the loop).

  * ``evaluate(agent, model, evalset, ...)`` — compiles the agent
    in-process and runs eval against the compiled ChatAgent directly. No
    HTTP, no deploy. Used for fast feedback during agent authoring and CI.

  * ``eval_against_endpoint(endpoint_url, evalset, ...)`` — same surface as
    ``evaluate()`` but routes every case through a deployed Databricks App's
    ``/invocations`` endpoint. Streams by default to avoid proxy timeouts on
    long pipelines. Pairs with ``apx-agent eval --endpoint-url <url>``.
"""

from __future__ import annotations

import json as _json
import logging
import os as _os
import subprocess as _subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._agents import BaseAgent

# Top-level import so tests can monkeypatch it. Falls back to a lazy import
# stub when mlflow (eval extra) isn't installed — the evaluate function
# raises a friendlier error in that case.
try:
    from ._chat_agent import compile_to_chat_agent
except ImportError:  # pragma: no cover — defensive
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
    dicts) or a plain string, posts to the agent's /invocations endpoint, and
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
            f"{base}/invocations",
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
    # Prefer the final assistant message that actually has text content.
    for msg in reversed(messages):
        if getattr(msg, "role", None) == "assistant":
            content = getattr(msg, "content", None)
            if content:
                return str(content)
    # Fallback: the agent terminated on a tool call with no assistant text — e.g.
    # a LoopAgent ending via finish_loop, whose handler returns the result as the
    # tool message ("Signal that the iterative task is complete and return the
    # final result"). Surface the most recent tool output instead of dropping to
    # "" (which eval scores as a silent failure). See issue #264.
    for msg in reversed(messages):
        if getattr(msg, "role", None) == "tool":
            content = getattr(msg, "content", None)
            if content:
                return str(content)
    return ""


def _normalize_evalset(evalset: Any) -> Any:
    """Normalise evalset rows so single- and double-nested ``inputs`` both work.

    ``evaluate`` builds the in-process predict as ``_predict(inputs)`` — the
    parameter is named ``inputs``. ``mlflow.genai.evaluate`` therefore requires
    each row's ``inputs`` dict to be keyed by the predict param name, i.e.
    double-nested::

        {"inputs": {"inputs": {"question": "..."}}, "expectations": {...}}

    But the docs and ``examples/data-triage-agent/eval/eval_dataset.py`` show the
    single-nested form::

        {"inputs": {"question": "..."}, "expectations": {...}}

    This wraps the single-nested form into the double-nested shape mlflow needs,
    while leaving the already-wrapped form untouched (idempotent). Only list-of-
    dicts evalsets are touched; DataFrames, paths/URIs, and other shapes pass
    through unchanged so nothing else breaks.
    """
    if not isinstance(evalset, list):
        return evalset
    normalized: list[Any] = []
    for row in evalset:
        if not isinstance(row, dict) or "inputs" not in row:
            normalized.append(row)
            continue
        row_inputs = row["inputs"]
        already_wrapped = isinstance(row_inputs, dict) and set(row_inputs) == {"inputs"}
        if already_wrapped:
            normalized.append(row)
        else:
            normalized.append({**row, "inputs": {"inputs": row_inputs}})
    return normalized


_OUTPUT_COLUMN_CANDIDATES = ("outputs", "response", "predictions")


def _count_empty_predictions(result: Any) -> int | None:
    """Count eval rows whose predicted output is None/empty.

    Under MLflow's parallel eval harness, some predictions come back None on
    transient FMAPI/warehouse failures (sequential runs succeed). The mean
    score then reads as a bad agent when it's really a concurrency artifact.
    Surfacing the empty-prediction count lets users tell the two apart.

    Looks at ``result.result_df`` (a pandas DataFrame) and inspects the first
    of ``outputs`` / ``response`` / ``predictions`` that's present. Returns the
    number of empty rows, or ``None`` when there's no result_df or none of the
    candidate columns exist (caller skips the warning gracefully).
    """
    import math

    result_df = getattr(result, "result_df", None)
    if result_df is None:
        return None

    columns = getattr(result_df, "columns", None)
    if columns is None:
        return None

    column = None
    for candidate in _OUTPUT_COLUMN_CANDIDATES:
        if candidate in columns:
            column = candidate
            break
    if column is None:
        return None

    empty = 0
    for value in result_df[column]:
        if value is None:
            empty += 1
        elif isinstance(value, float) and math.isnan(value):
            empty += 1
        elif isinstance(value, str) and not value.strip():
            empty += 1
    return empty


def _default_scorers(model: str | None = None) -> list[Any]:
    """Return a sensible default scorer bundle from Mosaic AI Agent Evaluation.

    Imported lazily so the eval extra is only required when actually
    running evaluation, not at apx-agent import time. Falls back to an
    empty list if the scorer classes aren't available in the installed
    mlflow version.

    ``model`` optionally overrides the LLM judge backing each scorer. The
    default judge (via MLflow's ``get_default_model()``) is
    ``openai:/gpt-4.1-mini``, which needs ``OPENAI_API_KEY``. Pass
    ``"databricks"`` to use the workspace default Databricks judge, or a
    ``"<provider>:/<model>"`` URI (e.g. ``"databricks:/databricks-claude-sonnet-4-6"``)
    to point the judge at a specific Databricks/Anthropic model. When
    ``None``, the scorer's own default applies (unchanged behaviour).
    """
    scorers: list[Any] = []
    try:
        from mlflow.genai.scorers import Correctness, RelevanceToQuery, Safety  # type: ignore[attr-defined]
        if model is not None:
            scorers.extend([Correctness(model=model), RelevanceToQuery(model=model), Safety(model=model)])
        else:
            scorers.extend([Correctness(), RelevanceToQuery(), Safety()])
    except Exception:
        logger.warning(
            "mlflow.genai.scorers.Correctness / RelevanceToQuery / Safety not available; "
            "callers should pass scorers=... explicitly."
        )
    return scorers


def evaluate(
    agent: "BaseAgent",
    *,
    model: str,
    evalset: Any,
    scorers: list[Any] | None = None,
    judge_model: str | None = None,
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
        judge_model: Optional LLM judge override for the *default* scorers.
            Ignored when ``scorers`` is passed explicitly. The default judge
            needs ``OPENAI_API_KEY``; pass ``"databricks"`` or a
            ``"<provider>:/<model>"`` URI to use a Databricks/Anthropic judge
            instead.
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

    Requires the ``eval`` extra (mlflow)::

        pip install 'apx-agent[eval]'
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
            "evaluate requires mlflow (eval extra). "
            "Install with: pip install 'apx-agent[eval]'"
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

    eval_scorers = scorers if scorers is not None else _default_scorers(judge_model)

    return mlflow.genai.evaluate(  # type: ignore[attr-defined]
        data=_normalize_evalset(evalset),
        predict_fn=_predict,
        scorers=eval_scorers,
        **mlflow_kwargs,
    )


# ---------------------------------------------------------------------------
# HTTP-endpoint evaluation — drive a deployed Databricks App via /invocations
# ---------------------------------------------------------------------------


def _extract_question(inputs: Any) -> str:
    """Pull a single user prompt out of varied eval-row shapes.

    Accepts: bare strings, ``{"question": ...}``, ``{"request": ...}``,
    ``{"input": ...}``, ``{"prompt": ...}``, ``{"query": ...}``, or
    ``{"messages": [...]}`` (uses the last user message).
    """
    if isinstance(inputs, str):
        return inputs
    if isinstance(inputs, dict):
        if "messages" in inputs and isinstance(inputs["messages"], list):
            for msg in reversed(inputs["messages"]):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    return str(msg.get("content", ""))
            return str(inputs["messages"])
        for key in ("question", "request", "input", "prompt", "query"):
            if key in inputs and inputs[key] is not None:
                return str(inputs[key])
        return str(inputs)
    return str(inputs)


def _resolve_endpoint_token(
    token: str | None = None,
    *,
    profile: str | None = None,
) -> str:
    """Resolve a Databricks bearer token for the endpoint call.

    Fallback chain:

      1. Explicit ``token`` argument.
      2. ``DATABRICKS_TOKEN`` env var.
      3. ``databricks auth token --profile <profile>`` via subprocess, where
         ``<profile>`` is the explicit ``profile`` arg or
         ``$DATABRICKS_CONFIG_PROFILE``.

    Raises ``RuntimeError`` with an actionable message if none of the above
    yield a token.
    """
    if token:
        return token
    env_token = _os.environ.get("DATABRICKS_TOKEN")
    if env_token:
        return env_token
    resolved_profile = profile or _os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if resolved_profile:
        try:
            result = _subprocess.run(
                ["databricks", "auth", "token", "--profile", resolved_profile],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (FileNotFoundError, _subprocess.CalledProcessError, _subprocess.TimeoutExpired) as e:
            raise RuntimeError(
                f"Could not resolve a Databricks token. The `databricks auth token "
                f"--profile {resolved_profile}` fallback failed: {e}. "
                "Pass `token=...` explicitly, set `DATABRICKS_TOKEN`, or fix the CLI profile."
            ) from e
        try:
            payload = _json.loads(result.stdout)
            access = payload.get("access_token")
            if access:
                return str(access)
        except _json.JSONDecodeError:
            pass
        raise RuntimeError(
            f"`databricks auth token --profile {resolved_profile}` did not return a "
            "parseable access_token. Pass `token=...` explicitly or set `DATABRICKS_TOKEN`."
        )
    raise RuntimeError(
        "Could not resolve a Databricks token for the endpoint. Tried: "
        "explicit `token=...`, $DATABRICKS_TOKEN, and `databricks auth token` "
        "(no $DATABRICKS_CONFIG_PROFILE set). Pass one of these."
    )


def endpoint_predict_fn(
    endpoint_url: str,
    *,
    token: str | None = None,
    profile: str | None = None,
    stream: bool = True,
    timeout: float = 300.0,
) -> Callable[[Any], str]:
    """Return a predict function that POSTs to a deployed App's ``/invocations``.

    Mirrors the pattern from ``examples/data-triage-agent/eval/eval_dataset.py``.
    When ``stream=True`` (the default) the predict function consumes the
    server-sent-events stream and accumulates ``text`` chunks. When
    ``stream=False`` it expects a single JSON body and extracts the response
    text from the standard ResponsesAgent envelope.

    ``token`` resolves via the fallback chain in :func:`_resolve_endpoint_token`.
    """
    resolved_token = _resolve_endpoint_token(token, profile=profile)
    base = endpoint_url.rstrip("/")
    url = f"{base}/invocations"
    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "Content-Type": "application/json",
    }

    def predict(inputs: Any) -> str:
        import urllib.request

        question = _extract_question(inputs)
        # ``/invocations`` (ResponsesAgent) takes the list form of ``input``.
        body = _json.dumps(
            {"input": [{"role": "user", "content": question}], "stream": stream}
        ).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        if stream:
            full_text = ""
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        chunk = _json.loads(line[6:])
                    except (_json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    text = chunk.get("text", "")
                    if text:
                        full_text += text
            return full_text

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        try:
            return str(data["output"][0]["content"][0]["text"])
        except (KeyError, IndexError, TypeError):
            return str(data)

    return predict


def eval_against_endpoint(
    endpoint_url: str,
    evalset: Any,
    *,
    token: str | None = None,
    profile: str | None = None,
    scorers: list[Any] | None = None,
    experiment: str | None = None,
    stream: bool = True,
    timeout: float = 300.0,
    **mlflow_kwargs: Any,
) -> Any:
    """Run ``mlflow.genai.evaluate`` against a deployed apx-agent endpoint.

    Drives each evalset row through the App's ``/invocations`` endpoint over
    HTTP. Returns whatever ``mlflow.genai.evaluate`` returns — the same
    shape as :func:`evaluate`.

    Args:
        endpoint_url: Base URL of the deployed Databricks App (e.g.
            ``https://my-agent.my-workspace.databricksapps.com``). The
            ``/invocations`` suffix is appended automatically.
        evalset: Eval dataset. Same shape ``mlflow.genai.evaluate`` accepts —
            list of dicts, DataFrame, or path-like.
        token: Databricks bearer token. Falls back to ``$DATABRICKS_TOKEN``
            then ``databricks auth token --profile $DATABRICKS_CONFIG_PROFILE``.
        profile: Optional Databricks CLI profile name. Used as part of the
            token-resolution fallback chain.
        scorers: List of Mosaic AI Agent Evaluation scorers. Defaults to
            ``[Correctness(), RelevanceToQuery()]`` when available.
        experiment: Optional MLflow experiment name/path.
        stream: Whether to use the streaming branch (default ``True``) —
            matches the data-triage-agent example, avoids proxy timeouts.
        timeout: Per-request timeout in seconds (default 300).
        **mlflow_kwargs: Forwarded verbatim to ``mlflow.genai.evaluate``.

    Requires ``mlflow`` (the ``eval`` extra). Does NOT require the
    ``langgraph`` extra — there's no in-process compile.
    """
    try:
        import mlflow
    except ImportError as e:  # pragma: no cover — exercised only without extra
        raise ImportError(
            "eval_against_endpoint requires mlflow. Install with: "
            "pip install 'apx-agent[eval]'"
        ) from e

    if experiment:
        try:
            mlflow.set_experiment(experiment)
        except Exception as e:
            raise RuntimeError(
                f"mlflow.set_experiment({experiment!r}) failed: {e}."
            ) from e

    predict_fn = endpoint_predict_fn(
        endpoint_url,
        token=token,
        profile=profile,
        stream=stream,
        timeout=timeout,
    )

    eval_scorers = scorers if scorers is not None else _default_scorers()

    return mlflow.genai.evaluate(  # type: ignore[attr-defined]
        data=evalset,
        predict_fn=predict_fn,
        scorers=eval_scorers,
        **mlflow_kwargs,
    )
