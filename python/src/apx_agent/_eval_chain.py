"""Cross-agent evaluation — eval a chain, see which sub-agents fired per case.

``evaluate()`` runs an evalset through the top-level agent's predict
path. That captures the supervisor's final answer, but it doesn't tell
you *which sub-agents handled which prompt* — and in a multi-agent
estate, that's exactly what you want to know when an eval result
regresses.

``evaluate_chain`` runs the eval the same way, then post-processes the
MLflow traces emitted during the run to correlate per-prompt outcomes
with the sub-agent endpoints that fired. The audit-attribute schema
(``apx.subagent.endpoint``, ``apx.subagent.name``, ``apx.tool.name``
when sub-agents are wired as agent-as-tool) gives the linkage.

Returns a ``ChainEvalReport`` per prompt: the supervisor's response,
the list of sub-agents invoked, the latencies, and the raw
``evaluate()`` result for downstream consumers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._eval import evaluate

if TYPE_CHECKING:
    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChainCaseResult:
    """Per-prompt chain-evaluation result."""

    request: str
    response: str
    sub_agents_invoked: tuple[str, ...]
    tool_calls: tuple[str, ...]
    duration_ms: int | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class ChainEvalReport:
    """Full chain-evaluation report."""

    cases: tuple[ChainCaseResult, ...]
    raw_eval_result: Any = None
    sub_agent_coverage: dict[str, int] = field(default_factory=dict)


def evaluate_chain(
    agent: "BaseAgent",
    *,
    model: str,
    evalset: Any,
    experiment: str,
    scorers: list[Any] | None = None,
    user_token: str | None = None,
    workspace_host: str | None = None,
    lookback_traces: int = 50,
) -> ChainEvalReport:
    """Run an evalset through ``agent`` and correlate sub-agent invocations.

    Calls ``apx_agent.evaluate`` (which compiles the agent and runs
    each evalset row through ``predict``), then queries MLflow for the
    traces emitted during that run and pulls ``apx.subagent.*`` /
    ``apx.tool.name`` attributes off each trace's spans to build the
    per-case chain report.

    Args:
        agent: The supervisor ``BaseAgent``.
        model: LLM endpoint for the supervisor.
        evalset: Eval dataset (anything ``mlflow.genai.evaluate``
            accepts).
        experiment: MLflow experiment to log into and read traces from.
            Required so trace correlation works — without it, this
            helper can't distinguish the supervisor's traces from
            unrelated ones.
        scorers: Optional Mosaic AI Agent Evaluation scorers.
        user_token: Optional OBO token threaded through eval.
        workspace_host: Optional workspace host for the OBO token.
        lookback_traces: How many recent traces to fetch when
            correlating. Default 50.

    Returns:
        ``ChainEvalReport`` with per-prompt results + aggregate
        sub-agent coverage counts.

    Requires the ``eval`` extra (mlflow).
    """
    try:
        import mlflow  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "evaluate_chain requires mlflow. "
            "Install with: pip install 'apx-agent[eval]'"
        ) from e

    raw_result = evaluate(
        agent,
        model=model,
        evalset=evalset,
        scorers=scorers,
        user_token=user_token,
        workspace_host=workspace_host,
        experiment=experiment,
    )

    # Pull traces emitted during the eval run.
    cases = _collect_chain_cases(
        experiment=experiment,
        evalset=evalset,
        lookback_traces=lookback_traces,
    )

    coverage: dict[str, int] = {}
    for case in cases:
        for sub in case.sub_agents_invoked:
            coverage[sub] = coverage.get(sub, 0) + 1

    return ChainEvalReport(
        cases=tuple(cases),
        raw_eval_result=raw_result,
        sub_agent_coverage=coverage,
    )


def _collect_chain_cases(
    *,
    experiment: str,
    evalset: Any,
    lookback_traces: int,
) -> list[ChainCaseResult]:
    """Walk recent MLflow traces and align them to evalset rows by request text."""
    import mlflow

    try:
        # This path needs span data (sub-agent / tool attributes live on
        # child spans), so include_spans stays default-True. We must NOT
        # swallow a read failure into an empty list — that reads as "no
        # sub-agents fired" and silently produces a wrong coverage report.
        traces_raw = mlflow.search_traces(  # type: ignore[attr-defined]
            experiment_names=[experiment],
            max_results=lookback_traces,
        )
    except Exception as e:
        raise RuntimeError(
            f"evaluate_chain: search_traces failed for experiment "
            f"{experiment!r}: {e}"
        ) from e

    # Normalise to a list of dicts the inspector can walk
    if hasattr(traces_raw, "to_dict"):
        try:
            traces_iter: Any = traces_raw.to_dict(orient="records")
        except Exception:
            traces_iter = []
    else:
        traces_iter = traces_raw or []

    traces_list = list(traces_iter or [])

    # Map evalset entries → their request text so we can pair by content.
    requests = _request_texts_from_evalset(evalset)

    # If the evalset is larger than the trace page we fetched, correlation
    # can only ever cover a prefix — warn rather than silently undercount.
    if len(requests) > len(traces_list):
        logger.warning(
            "evaluate_chain: %d evalset rows but only %d traces fetched "
            "(lookback_traces=%d) — coverage may be incomplete; raise "
            "lookback_traces to cover the full evalset.",
            len(requests), len(traces_list), lookback_traces,
        )

    # Consume each trace at most once so duplicate request strings don't all
    # re-match the same first trace (which double-counted sub-agent coverage).
    consumed: set[int] = set()
    cases: list[ChainCaseResult] = []
    for req in requests:
        case = _build_case_from_trace(req, traces_list, consumed)
        if case is not None:
            cases.append(case)
    return cases


def _request_texts_from_evalset(evalset: Any) -> list[str]:
    """Extract the request text from each evalset row.

    Tolerates dict / list / DataFrame shapes and the same key fallbacks
    ``evaluate`` uses (request / input / prompt / query / question).
    """
    rows: list[Any]
    if hasattr(evalset, "to_dict"):
        try:
            rows = evalset.to_dict(orient="records")  # type: ignore[union-attr]
        except Exception:
            rows = []
    elif isinstance(evalset, list):
        rows = evalset
    else:
        rows = []

    out: list[str] = []
    for r in rows:
        if isinstance(r, str):
            out.append(r)
            continue
        if not isinstance(r, dict):
            continue
        for key in ("request", "input", "prompt", "query", "question"):
            if key in r and r[key]:
                out.append(str(r[key]))
                break
    return out


def _build_case_from_trace(
    request_text: str,
    traces: list[Any],
    consumed: set[int],
) -> ChainCaseResult | None:
    """Match a request to an as-yet-unconsumed trace and pull its fields.

    Records the matched trace index in ``consumed`` so it can't be matched
    again — duplicate request strings then bind to distinct traces instead
    of all re-matching the first one (which double-counted coverage).
    """
    for idx, t in enumerate(traces or []):
        if idx in consumed:
            continue
        # Both dict and Trace-object access patterns
        attrs = _trace_attrs(t)
        spans = _trace_spans(t)
        # Match on the structured first user message — not a substring of
        # the stringified inputs (which collided across requests).
        if not _trace_matches_request(t, spans, request_text):
            continue
        consumed.add(idx)
        # Pull sub-agent + tool names off all child spans
        sub_agents: list[str] = []
        tool_calls: list[str] = []
        for span in spans:
            span_attrs = dict(getattr(span, "attributes", None) or {})
            if "apx.subagent.endpoint" in span_attrs:
                sub_agents.append(str(span_attrs["apx.subagent.endpoint"]))
            elif "apx.subagent.name" in span_attrs:
                sub_agents.append(str(span_attrs["apx.subagent.name"]))
            if "apx.tool.name" in span_attrs:
                tool_calls.append(str(span_attrs["apx.tool.name"]))
        # Trace-level info
        trace_id = _trace_id(t)
        duration_ms = _coerce_int(
            getattr(getattr(t, "info", None), "execution_time_ms", None)
            if not isinstance(t, dict)
            else t.get("execution_time_ms")
        )
        return ChainCaseResult(
            request=request_text,
            response=_response_text_from_trace(t, attrs),
            sub_agents_invoked=tuple(sub_agents),
            tool_calls=tuple(tool_calls),
            duration_ms=duration_ms,
            trace_id=trace_id,
        )
    return None


def _trace_attrs(t: Any) -> dict[str, Any]:
    if isinstance(t, dict):
        return t.get("tags") or t.get("attributes") or {}
    info = getattr(t, "info", None)
    return dict(getattr(info, "tags", {}) or {}) if info else {}


def _trace_spans(t: Any) -> list[Any]:
    if isinstance(t, dict):
        return t.get("spans") or []
    data = getattr(t, "data", None)
    return list(getattr(data, "spans", None) or [])


def _trace_matches_request(t: Any, spans: list[Any], request_text: str) -> bool:
    """Match on the root span's first user message, exactly.

    The earlier version did a substring check on the *stringified* inputs,
    which collided whenever one request string was a substring of another
    (or of serialized history). Here we pull the first user message out of
    the root span's structured inputs and compare it to the request text,
    falling back to a substring check only when the structured shape is
    unavailable.
    """
    if not spans:
        return False
    root = spans[0]
    inputs = getattr(root, "inputs", None)
    if inputs is None:
        return False
    first_user = _first_user_message(inputs)
    if first_user is not None:
        return first_user.strip() == request_text.strip()
    # Structured shape unavailable — fall back to the loose substring check.
    return request_text in str(inputs)


def _first_user_message(inputs: Any) -> str | None:
    """Extract the first user message's text from a span's structured inputs.

    Handles the common shapes: ``{"messages": [{"role": "user", ...}]}``,
    a bare list of message dicts, and message objects with ``.role`` /
    ``.content`` attributes. Returns ``None`` when no structured user
    message can be located (caller falls back to a substring match).
    """
    messages: Any = None
    if isinstance(inputs, dict):
        messages = inputs.get("messages")
        if messages is None and "role" in inputs:
            messages = [inputs]
    elif isinstance(inputs, list):
        messages = inputs
    if not isinstance(messages, list):
        return None
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if content is None:
                return None
            if isinstance(content, str):
                return content
            # Content may be a list of parts (e.g. [{"type": "text", "text": ...}]).
            if isinstance(content, list):
                texts = [
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                ]
                return "".join(texts)
            return str(content)
    return None


def _trace_id(t: Any) -> str | None:
    if isinstance(t, dict):
        return t.get("trace_id") or t.get("request_id")
    info = getattr(t, "info", None)
    if info is None:
        return None
    return getattr(info, "trace_id", None) or getattr(info, "request_id", None)


def _response_text_from_trace(t: Any, attrs: dict[str, Any]) -> str:
    """Pull the final assistant text out of the trace's root span outputs."""
    spans = _trace_spans(t)
    if not spans:
        return ""
    root = spans[0]
    outputs = getattr(root, "outputs", None) or {}
    if isinstance(outputs, dict):
        messages = outputs.get("messages") or []
        for m in reversed(messages):
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            if role == "assistant":
                content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
                if content:
                    return str(content)
    return ""


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
