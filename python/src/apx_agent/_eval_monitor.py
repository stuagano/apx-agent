"""apx-agent eval monitor — MLflow production monitoring for a registered scorer.

Wraps the native ``mlflow.genai.scorers`` lifecycle (``register`` / ``start`` /
``stop`` / ``list_scorers``) plus ``set_databricks_monitoring_sql_warehouse_id``
for one scorer on the project's experiment. The Databricks monitoring service
does the sampling and writes assessments back onto traces; apx adds no
scheduler or job of its own.

Production monitoring is Beta and ``Scorer.start()`` is experimental in the
supported ``mlflow>=3.14,<3.15`` boundary — revalidate before bumping MLflow.

mlflow symbols are module globals so tests can monkeypatch them.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apx_agent._doctor import _TRACE_DESTINATION_TAG, _local_experiment_id
from apx_agent._labeling import list_align_runs
from apx_agent._mlflow_tracing import _resolve_experiment_id

try:  # mlflow is the `eval` extra
    import mlflow as _mlflow
    from mlflow.genai.scorers import ScorerSamplingConfig, get_scorer, list_scorers
    from mlflow.tracing import set_databricks_monitoring_sql_warehouse_id
except Exception:  # pragma: no cover — only without the extra
    _mlflow = None  # type: ignore[assignment]
    ScorerSamplingConfig = None  # type: ignore[assignment,misc]
    get_scorer = None  # type: ignore[assignment]
    list_scorers = None  # type: ignore[assignment]
    set_databricks_monitoring_sql_warehouse_id = None  # type: ignore[assignment]

WAREHOUSE_TAG = "mlflow.monitoring.sqlWarehouseId"

WAREHOUSE_GUIDANCE = (
    "this experiment stores traces in Unity Catalog ({destination}), so production "
    "monitoring needs a SQL warehouse. Pass --warehouse-id <id> (it runs "
    "set_databricks_monitoring_sql_warehouse_id on the experiment). The identity "
    "running this command needs CAN USE on the warehouse and CAN EDIT on the experiment; "
    "the monitoring job runs as the identity that first registered a scorer."
)


class MonitorError(Exception):
    """A user-facing monitoring error (bad input, unmet precondition)."""


@dataclass
class ScorerState:
    name: str
    status: str
    sample_rate: float | None
    filter_string: str | None


@dataclass
class MonitorReport:
    experiment_id: str
    trace_destination: str | None
    warehouse_id: str | None
    scorers: list[ScorerState] = field(default_factory=list)


def _require_mlflow() -> Any:
    if _mlflow is None:  # pragma: no cover
        raise MonitorError("eval monitor requires mlflow. Install with: pip install 'apx-agent[eval]'")
    return _mlflow


def resolve_experiment(explicit: str | None) -> str:
    """Experiment id from --experiment / [tool.apx] experiment, else MLFLOW_EXPERIMENT_ID."""
    _require_mlflow()
    ref = explicit or _local_experiment_id()
    if not ref:
        raise MonitorError(
            "no experiment: pass --experiment, set experiment in [tool.apx], "
            "or set MLFLOW_EXPERIMENT_ID."
        )
    try:
        return _resolve_experiment_id(ref)
    except ValueError as e:
        raise MonitorError(str(e)) from e


def default_scorer_name(experiment_id: str) -> str:
    """The judge registered by the newest `label align` run on the experiment."""
    runs = list_align_runs(experiment_id=experiment_id, max_results=1)
    if not runs:
        raise MonitorError(
            f"no `label align` run found on experiment {experiment_id}; pass --scorer <name>."
        )
    return runs[0].registered_as


def load_scorer(spec: str) -> Any:
    """Import a ``module:attr`` scorer object (for registering a scorer defined in code)."""
    _require_mlflow()
    from mlflow.genai.scorers import Scorer

    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise MonitorError(f"--from must be 'module:attr'; got {spec!r}")
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        obj = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as e:
        raise MonitorError(f"could not load scorer {spec!r}: {e}") from e
    if not isinstance(obj, Scorer):
        raise MonitorError(f"{spec!r} is not an mlflow.genai Scorer (got {type(obj).__name__}).")
    return obj


def _experiment_tags(experiment_id: str) -> dict[str, str]:
    return dict(_require_mlflow().get_experiment(experiment_id).tags or {})


def _state(scorer: Any) -> ScorerState:
    return ScorerState(
        name=str(scorer.name),
        status=scorer.status.value,
        sample_rate=scorer.sample_rate,
        filter_string=scorer.filter_string,
    )


def _registered(experiment_id: str, name: str) -> Any:
    try:
        return get_scorer(name=name, experiment_id=experiment_id)  # type: ignore[misc]
    except Exception as e:
        raise MonitorError(
            f"scorer {name!r} is not registered on experiment {experiment_id} ({e}). "
            "Register it with `apx-agent label align`, or pass --from module:attr to "
            "register a scorer defined in code."
        ) from e


def start(
    *,
    experiment_id: str,
    scorer_name: str,
    sample_rate: float,
    filter_string: str | None,
    warehouse_id: str | None,
    scorer_obj: Any | None,
) -> MonitorReport:
    """Set the monitoring warehouse if needed, register if needed, then start sampling."""
    tags = _experiment_tags(experiment_id)
    destination = tags.get(_TRACE_DESTINATION_TAG)
    if warehouse_id:
        set_databricks_monitoring_sql_warehouse_id(warehouse_id, experiment_id=experiment_id)  # type: ignore[misc]
    elif destination and not tags.get(WAREHOUSE_TAG):
        raise MonitorError(WAREHOUSE_GUIDANCE.format(destination=destination))

    registered = {s.name for s in list_scorers(experiment_id=experiment_id)}  # type: ignore[misc]
    if scorer_obj is not None and scorer_name not in registered:
        scorer = scorer_obj.register(name=scorer_name, experiment_id=experiment_id)
    else:
        scorer = _registered(experiment_id, scorer_name)
    active = scorer.start(
        name=scorer_name,
        experiment_id=experiment_id,
        sampling_config=ScorerSamplingConfig(sample_rate=sample_rate, filter_string=filter_string),  # type: ignore[misc]
    )
    return MonitorReport(
        experiment_id=experiment_id,
        trace_destination=destination,
        warehouse_id=warehouse_id or tags.get(WAREHOUSE_TAG),
        scorers=[_state(active)],
    )


def stop(*, experiment_id: str, scorer_name: str) -> MonitorReport:
    """Set the scorer's sample rate to 0. The judge and its assessments are kept."""
    tags = _experiment_tags(experiment_id)
    stopped = _registered(experiment_id, scorer_name).stop(name=scorer_name, experiment_id=experiment_id)
    return MonitorReport(
        experiment_id=experiment_id,
        trace_destination=tags.get(_TRACE_DESTINATION_TAG),
        warehouse_id=tags.get(WAREHOUSE_TAG),
        scorers=[_state(stopped)],
    )


def status(*, experiment_id: str, scorer_name: str | None) -> MonitorReport:
    """List registered scorers (or one) with their sampling configuration."""
    tags = _experiment_tags(experiment_id)
    scorers = [
        _state(s) for s in list_scorers(experiment_id=experiment_id)  # type: ignore[misc]
        if scorer_name is None or s.name == scorer_name
    ]
    if scorer_name is not None and not scorers:
        raise MonitorError(f"scorer {scorer_name!r} is not registered on experiment {experiment_id}.")
    return MonitorReport(
        experiment_id=experiment_id,
        trace_destination=tags.get(_TRACE_DESTINATION_TAG),
        warehouse_id=tags.get(WAREHOUSE_TAG),
        scorers=scorers,
    )
