"""apx-agent labeling-service support — MLflow judge-alignment loop.

Pure helpers + two orchestrators (start_session / align_judge) behind the
`apx-agent label` CLI group. The user brings their own registered judge; we
derive the label schema from it so the schema name cannot drift from the
judge name (the #1 documented MemAlign failure mode).

mlflow.genai symbols are imported at module load and exposed as module
globals so tests can monkeypatch them. dspy (MemAlign) is imported lazily in
align_judge so `label start` never requires the [align] extra.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:  # mlflow is the `eval`/`align` extra
    from mlflow.genai import label_schemas as _label_schemas
except Exception:  # pragma: no cover — only without the extra
    _label_schemas = None  # type: ignore[assignment]

try:
    import mlflow as _mlflow
    from apx_agent._mlflow_tracing import search_traces_for_experiment
    set_trace_tag = _mlflow.set_trace_tag
except Exception:  # pragma: no cover
    search_traces_for_experiment = None  # type: ignore[assignment]
    set_trace_tag = None  # type: ignore[assignment]

try:
    from mlflow.genai import (
        create_labeling_session,
        get_review_app,
    )
    from mlflow.genai.label_schemas import create_label_schema
    from mlflow.genai.scorers import get_scorer
    from mlflow.genai.datasets import create_dataset, get_dataset
except Exception:  # pragma: no cover
    create_labeling_session = None  # type: ignore[assignment]
    get_review_app = None  # type: ignore[assignment]
    create_label_schema = None  # type: ignore[assignment]
    get_scorer = None  # type: ignore[assignment]
    create_dataset = None  # type: ignore[assignment]
    get_dataset = None  # type: ignore[assignment]


RUN_TAG = "apx.label.run"
EXPERIMENT_TAG = "apx.mlflow.experiment_id"


class LabelingError(Exception):
    """A user-facing labeling error (bad input, unmet precondition)."""


def make_run_id(judge_name: str, now: datetime) -> str:
    """Deterministic run id tying `start` to `align`."""
    return f"{judge_name}-{now:%Y%m%dT%H%M%SZ}"


def dataset_name_for(agent_name: str, run_id: str) -> str:
    return f"{agent_name}_label_{run_id}"


def session_name_for(run_id: str) -> str:
    return f"{run_id}_sme"


def _require_mlflow() -> Any:
    if _label_schemas is None:  # pragma: no cover
        raise LabelingError(
            "labeling requires mlflow. Install with: pip install 'apx-agent[eval]'"
        )
    return _label_schemas


def parse_scale(scale: str) -> tuple[float, float]:
    """Parse a ``MIN-MAX`` scale string into ``(min, max)`` floats."""
    parts = [p.strip() for p in (scale or "").split("-")]
    if len(parts) != 2 or not all(parts):
        raise LabelingError(f"--scale must be 'MIN-MAX' (e.g. '1-5'); got {scale!r}")
    try:
        lo, hi = float(parts[0]), float(parts[1])
    except ValueError as e:
        raise LabelingError(f"--scale bounds must be numbers; got {scale!r}") from e
    return lo, hi


def derive_label_schema(
    *, judge: Any, scale: str | None, options: list[str] | None
) -> dict[str, Any]:
    """Build ``create_label_schema`` kwargs from a loaded judge.

    Name and instruction come from the judge verbatim. The input family is
    derived from ``judge.feedback_value_type``; numeric needs ``scale`` and
    string needs ``options``.
    """
    ls = _require_mlflow()
    name = str(judge.name)
    ft = judge.feedback_value_type

    if ft is bool:  # check before int — bool is a subclass of int
        schema_input: Any = ls.InputCategorical(options=["true", "false"])
    elif ft in (int, float):
        if not scale:
            raise LabelingError(
                f"judge {name!r} uses a numeric scale; pass --scale MIN-MAX (e.g. 1-5)"
            )
        lo, hi = parse_scale(scale)
        schema_input = ls.InputNumeric(min_value=lo, max_value=hi)
    elif ft is str:
        if not options:
            raise LabelingError(
                f"judge {name!r} uses a categorical scale; pass --options a,b,c"
            )
        schema_input = ls.InputCategorical(options=list(options))
    else:
        raise LabelingError(f"unsupported judge feedback_value_type: {ft!r}")

    return {
        "name": name,
        "type": "feedback",
        "title": name,
        "input": schema_input,
        "instruction": str(judge.instructions),
        "enable_comment": True,
        "overwrite": True,
    }


def resolve_experiment_id(*, explicit: str | None, agent_tags: dict[str, str]) -> str:
    """Resolve the MLflow experiment id for a deployed agent.

    Order: --experiment override, then the apx.mlflow.experiment_id UC tag.
    The naming-convention path is deploy-time only and not available here.
    """
    if explicit:
        return explicit
    tagged = agent_tags.get(EXPERIMENT_TAG)
    if tagged:
        return tagged
    raise LabelingError(
        "could not resolve the agent's MLflow experiment. Pass --experiment <id> "
        "(or redeploy so the apx.mlflow.experiment_id tag is recorded)."
    )


def select_scored_traces(
    *, experiment_id: str, judge_name: str, filter_string: str | None, limit: int | None
) -> Any:
    """Pull traces to be labeled. Fails fast if none match."""
    if search_traces_for_experiment is None:  # pragma: no cover
        raise LabelingError("labeling requires mlflow. pip install 'apx-agent[eval]'")
    kwargs: dict[str, Any] = {"return_type": "pandas"}
    if filter_string:
        kwargs["filter_string"] = filter_string
    if limit:
        kwargs["max_results"] = limit
    df = search_traces_for_experiment(experiment_id, **kwargs)
    if df is None or len(df) == 0:
        raise LabelingError(
            f"no traces found for experiment {experiment_id}. Score a sample first "
            f"with --evaluate <inputs.jsonl>, or widen --filter/--since/--limit."
        )
    return df


def tag_traces(trace_ids: list[str], run_id: str) -> int:
    """Tag each trace with apx.label.run=<run_id> for the start->align handoff."""
    if set_trace_tag is None:  # pragma: no cover
        raise LabelingError("labeling requires mlflow. pip install 'apx-agent[eval]'")
    n = 0
    for tid in trace_ids:
        set_trace_tag(trace_id=tid, key=RUN_TAG, value=run_id)
        n += 1
    return n


@dataclass
class StartResult:
    run_id: str
    session_url: str
    trace_count: int
    dataset_name: str
    schema_name: str


def start_session(
    *,
    experiment_id: str,
    agent_name: str,
    judge_name: str,
    scale: str | None,
    options: list[str] | None,
    assignees: list[str],
    filter_string: str | None,
    limit: int | None,
    endpoint: str | None,
    attach_agent: bool,
    now: datetime,
) -> StartResult:
    """Provision a labeling session for a deployed agent's judge."""
    judge = get_scorer(name=judge_name, experiment_id=experiment_id)

    schema_kwargs = derive_label_schema(judge=judge, scale=scale, options=options)
    create_label_schema(**schema_kwargs)
    schema_name = schema_kwargs["name"]

    run_id = make_run_id(judge_name, now)
    traces = select_scored_traces(
        experiment_id=experiment_id, judge_name=judge_name,
        filter_string=filter_string, limit=limit,
    )
    trace_ids = [str(t) for t in traces["trace_id"].tolist()]
    tag_traces(trace_ids, run_id)

    dataset_name = dataset_name_for(agent_name, run_id)
    try:
        dataset = get_dataset(name=dataset_name)
    except Exception:
        dataset = create_dataset(name=dataset_name)
    # search_traces returns request/response; merge_records wants inputs/outputs
    renamed = traces.rename(columns={"request": "inputs", "response": "outputs"})
    dataset.merge_records(renamed)

    if attach_agent and endpoint:
        review_app = get_review_app(experiment_id=experiment_id)
        if review_app is None:
            raise LabelingError(
                f"no review app found for experiment {experiment_id}; cannot attach agent "
                f"'{agent_name}'. Check the experiment id or omit --attach-agent."
            )
        review_app.add_agent(
            agent_name=agent_name, model_serving_endpoint=endpoint, overwrite=True,
        )

    session = create_labeling_session(
        name=session_name_for(run_id),
        assigned_users=assignees,
        label_schemas=[schema_name],
    )
    session = session.add_dataset(dataset_name=dataset_name)

    return StartResult(
        run_id=run_id, session_url=str(getattr(session, "url", "")),
        trace_count=len(trace_ids), dataset_name=dataset_name, schema_name=schema_name,
    )


@dataclass
class AlignResult:
    judge_name: str
    guidelines: list[str]
    registered_as: str


def _load_memalign(*, reflection_model: str, embedding_model: str, retrieval_k: int) -> Any:
    """Build a MemAlignOptimizer, translating a missing dspy into guidance."""
    from mlflow.exceptions import MlflowException
    try:
        from mlflow.genai.judges.optimizers import MemAlignOptimizer
    except (ImportError, MlflowException) as e:
        raise LabelingError(
            "judge alignment (MemAlign) requires dspy. "
            "Install with: pip install 'apx-agent[align]'"
        ) from e
    return MemAlignOptimizer(
        reflection_lm=reflection_model,
        retrieval_k=retrieval_k,
        embedding_model=embedding_model,
    )


def align_judge(
    *,
    experiment_id: str,
    judge_name: str,
    run_id: str,
    reflection_model: str,
    embedding_model: str,
    retrieval_k: int,
    new_version: str | None,
) -> AlignResult:
    """Align a judge from a finished labeling run's SME-labeled traces."""
    if search_traces_for_experiment is None:  # pragma: no cover
        raise LabelingError("alignment requires mlflow. pip install 'apx-agent[align]'")
    optimizer = _load_memalign(
        reflection_model=reflection_model,
        embedding_model=embedding_model,
        retrieval_k=retrieval_k,
    )
    traces = search_traces_for_experiment(
        experiment_id, filter_string=f"tag.{RUN_TAG} = '{run_id}'", return_type="list",
    )
    base = get_scorer(name=judge_name, experiment_id=experiment_id)
    aligned = base.align(traces=traces, optimizer=optimizer)

    guidelines = [g.guideline_text for g in getattr(aligned, "_semantic_memory", []) or []]

    if new_version:
        from mlflow.genai.judges import make_judge
        new = make_judge(
            name=new_version, instructions=aligned.instructions,
            feedback_value_type=base.feedback_value_type, model=base.model,
        )
        new.register(experiment_id=experiment_id)
        registered_as = new_version
    else:
        updated = aligned.update(experiment_id=experiment_id)
        registered_as = str(getattr(updated, "name", judge_name))

    return AlignResult(judge_name=judge_name, guidelines=guidelines, registered_as=registered_as)
