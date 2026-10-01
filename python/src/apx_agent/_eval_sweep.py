"""Compare the existing evaluators across models without inventing scores or prices."""

from __future__ import annotations

import math
import os
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeGuard

from ._canary import _percentile
from ._eval import evaluate
from ._eval_chain import evaluate_chain

if TYPE_CHECKING:
    from ._agents import BaseAgent


@dataclass(frozen=True)
class SweepResult:
    """One model's scores and telemetry; cost is a trace estimate, not a bill."""

    model: str
    case_count: int
    metrics: dict[str, float | None] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    latency_cases: int = 0
    cost_usd: float | None = None
    cost_cases: int = 0
    run_id: str | None = None
    errors: tuple[str, ...] = ()


def _nonnegative(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _trace_cost(trace: Any) -> float | None:
    """Only count a trace with prices for every instrumented LLM span."""
    cost = trace.info.cost
    total = cost.get("total_cost") if isinstance(cost, dict) else None
    llm_spans = [s for s in trace.data.spans if s.span_type in ("LLM", "CHAT_MODEL")]
    if not _nonnegative(total) or not llm_spans:
        return None
    for span in llm_spans:
        value = span.attributes.get("mlflow.llm.cost")
        if not isinstance(value, dict) or not _nonnegative(value.get("total_cost")):
            return None
    return float(total)


def _summarize_live(model: str, count: int, result: Any) -> SweepResult:
    from mlflow.entities import Trace

    metrics = {
        key: float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None
        for key, value in result.metrics.items()
    }
    durations: list[int] = []
    costs: list[float] = []
    errors = [f"Metric {key}: unavailable" for key, value in metrics.items() if value is None]
    seen: set[str] = set()
    frame = result.result_df
    traces = list(frame["trace"]) if frame is not None and "trace" in frame else []
    if frame is not None:
        for column in frame.columns:
            if isinstance(column, str) and column.endswith("/error_message"):
                for index, message in enumerate(frame[column]):
                    if isinstance(message, str) and message:
                        errors.append(f"Case {index + 1}: scorer {column.removesuffix('/error_message')} failed")
    if len(traces) != count:
        errors.append(f"Expected {count} cases; received {len(traces)} result rows")
    for index, value in enumerate(traces):
        try:
            # MLflow 3.14 serializes this column for Spark compatibility.
            trace = Trace.from_json(value) if isinstance(value, str) else value
            trace_id = trace.info.trace_id
            if not trace_id or trace_id in seen:
                errors.append(f"Case {index + 1}: missing or duplicate trace ID")
                continue
            seen.add(trace_id)
            if trace.info.state != "OK":
                errors.append(f"Case {index + 1}: trace did not complete successfully")
            duration = trace.info.execution_duration
            if _nonnegative(duration):
                durations.append(int(duration))
            else:
                errors.append(f"Case {index + 1}: missing or invalid latency")
            cost = _trace_cost(trace)
            if cost is not None:
                costs.append(cost)
        except (AttributeError, TypeError, ValueError, KeyError):
            errors.append(f"Case {index + 1}: missing or invalid trace telemetry")
    durations.sort()
    return SweepResult(
        model=model, case_count=count, metrics=metrics,
        latency_p50_ms=_percentile(durations, 50), latency_p95_ms=_percentile(durations, 95),
        latency_cases=len(durations), cost_usd=sum(costs) if len(costs) == count and len(traces) == count else None,
        cost_cases=len(costs), run_id=result.run_id, errors=tuple(errors),
    )


def evaluate_sweep(
    agent: BaseAgent,
    *,
    models: list[str],
    evalset: Any = None,
    fixtures: list[dict[str, Any]] | None = None,
    scorers: list[Any] | None = None,
    judge_model: str | None = None,
    experiment: str | None = None,
    user_token: str | None = None,
    workspace_host: str | None = None,
) -> list[SweepResult]:
    """Run one dataset sequentially across at least two distinct model names.

    Supply either an evalset (nonempty list of input rows or a DataFrame) or
    recorded-tool fixtures. Live mode reuses ``evaluate`` and its scorers;
    fixture mode reuses ``evaluate_chain`` and its three outcomes. Precomputed
    outputs/traces are rejected so each model actually predicts. Each model
    receives a deep copy of the same data. Models use the caller's existing
    authentication; this helper never selects a Databricks profile.

    Latency percentiles use per-case milliseconds and the existing comparison
    percentile helper. Coverage counts expose missing telemetry. Cost is the
    sum of MLflow's trace-level LLM estimates only when every case is priced;
    it excludes scorer, tool, and infrastructure charges. Fixture cost is
    unavailable. Endpoint lookback billing is never used as run cost.

    Invalid shared inputs raise before any model runs. Individual evaluation
    failures become safe error rows, allowing the remaining models to run.
    KeyboardInterrupt and other BaseException cancellations propagate.
    """
    if not isinstance(models, list) or len(models) < 2 or any(not isinstance(m, str) or not m.strip() or m != m.strip() for m in models):
        raise ValueError("models must be a list of at least two nonempty model names without surrounding whitespace")
    if len(set(models)) != len(models):
        raise ValueError("models must be distinct")
    if (evalset is None) == (fixtures is None):
        raise ValueError("Supply exactly one of evalset or fixtures")
    if fixtures is not None:
        if any(v is not None for v in (scorers, judge_model, experiment, user_token, workspace_host)):
            raise ValueError("fixtures cannot be combined with live evaluation options")
        from ._eval_fixtures import _validate

        data = deepcopy(_validate(agent, fixtures))
    else:
        if os.environ.get("APX_AGENT_MODEL_OVERRIDE"):
            raise ValueError("Unset APX_AGENT_MODEL_OVERRIDE before a live sweep so each requested model is evaluated")
        data = evalset.to_dict(orient="records") if hasattr(evalset, "to_dict") else evalset
        if not isinstance(data, list) or not data or any(
            not isinstance(row, dict) or "inputs" not in row or "outputs" in row or "trace" in row or "trace_id" in row
            for row in data
        ):
            raise ValueError("evalset must be nonempty input rows without precomputed outputs or traces")
        data = deepcopy(data)
    results: list[SweepResult] = []
    for model in models:
        try:
            if fixtures is not None:
                report = evaluate_chain(agent, model=model, fixtures=deepcopy(data))
                durations = sorted(c.duration_ms for c in report.cases if c.duration_ms is not None)
                results.append(SweepResult(
                    model=model, case_count=len(data), outcome_counts=report.outcome_counts,
                    latency_p50_ms=_percentile(durations, 50), latency_p95_ms=_percentile(durations, 95),
                    latency_cases=len(durations),
                    errors=tuple(f"Case {case.case_id}: {error}" for case in report.cases for error in case.errors if error == "Chain execution failed"),
                ))
            else:
                result = evaluate(
                    agent, model=model, evalset=deepcopy(data), scorers=scorers,
                    judge_model=judge_model, experiment=experiment,
                    user_token=user_token, workspace_host=workspace_host,
                )
                results.append(_summarize_live(model, len(data), result))
        except Exception as exc:
            # Preserve failures in the comparison without leaking endpoint/token text.
            results.append(SweepResult(model=model, case_count=len(data), errors=(f"Evaluation failed ({type(exc).__name__})",)))
    return results
