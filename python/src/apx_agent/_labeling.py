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
    from mlflow import set_experiment
    from mlflow.genai import (
        create_labeling_session,
        get_review_app,
    )
    from mlflow.genai.label_schemas import create_label_schema
    from mlflow.genai.scorers import get_scorer
except Exception:  # pragma: no cover
    set_experiment = None  # type: ignore[assignment]
    create_labeling_session = None  # type: ignore[assignment]
    get_review_app = None  # type: ignore[assignment]
    create_label_schema = None  # type: ignore[assignment]
    get_scorer = None  # type: ignore[assignment]


RUN_TAG = "apx.label.run"
EXPERIMENT_TAG = "apx.mlflow.experiment_id"


class LabelingError(Exception):
    """A user-facing labeling error (bad input, unmet precondition)."""


def make_run_id(judge_name: str, now: datetime) -> str:
    """Deterministic run id tying `start` to `align`."""
    return f"{judge_name}-{now:%Y%m%dT%H%M%SZ}"


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
    """Pull traces to be labeled. Fails fast if none carry the judge's score.

    Every trace in a labeling session must already carry the judge's score —
    SMEs cannot resolve alignment gaps without a baseline prediction to react to.

    Assessment schema: the pandas DataFrame has an ``assessments`` column where
    each cell is a list of dicts (from ``Assessment.to_dictionary()`` with
    ``preserving_proto_field_name=True``). The name key is ``assessment_name``
    (proto field name), not ``name``.

    Degradation contract: if the ``assessments`` column is absent or every cell
    is None/non-list we cannot affirmatively determine scoring status, so we
    return the DataFrame unchanged (degrade gracefully) rather than break the
    happy path on old/mock/unexpected DataFrame shapes. We only raise when we
    can positively confirm no trace carries the judge's score.
    """
    if search_traces_for_experiment is None:  # pragma: no cover
        raise LabelingError("labeling requires mlflow. pip install 'apx-agent[eval]'")
    # include_spans=False keeps this a metadata-only read, so it still returns
    # rows on FEVM/private-link workspaces where the trace blob store is blocked
    # (a span read otherwise makes search_traces silently return 0 rows). We
    # only need request/response/assessments here, never the execution spans.
    kwargs: dict[str, Any] = {"return_type": "pandas", "include_spans": False}
    if filter_string:
        kwargs["filter_string"] = filter_string
    if limit:
        kwargs["max_results"] = limit
    df = search_traces_for_experiment(experiment_id, **kwargs)
    if df is None or len(df) == 0:
        raise LabelingError(
            f"no traces found for experiment {experiment_id}. Score a sample first "
            f"with --evaluate <inputs.jsonl>, or widen --filter/--limit."
        )

    # Verify that at least one trace carries the judge's assessment.
    # Degrade gracefully (return df) if the column is absent or has an
    # unrecognised shape — wrong-schema rejection is worse than no check.
    if "assessments" in df.columns:
        saw_parseable = False
        found = False
        for cell in df["assessments"]:
            if not isinstance(cell, list):
                continue
            saw_parseable = True
            for assessment in cell:
                if not isinstance(assessment, dict):
                    continue
                # to_dictionary() uses preserving_proto_field_name=True → "assessment_name"
                aname = assessment.get("assessment_name") or assessment.get("name")
                if aname == judge_name:
                    found = True
                    break
            if found:
                break
        if saw_parseable and not found:
            raise LabelingError(
                f"none of the {len(df)} traces for experiment {experiment_id} carry a "
                f"'{judge_name}' score; score them first (run the judge / --evaluate) "
                f"before creating a labeling session."
            )
        # If not saw_parseable: column present but no recognisable list cells —
        # degrade gracefully; see docstring for rationale.

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
    # The mlflow.genai labeling APIs (create_label_schema, create_labeling_session,
    # review-app resolution) read the *active* MLflow experiment from ambient
    # context, not from an argument. Make experiment_id active up front so they
    # target the right experiment instead of failing "no active experiment".
    if set_experiment is not None:
        set_experiment(experiment_id=experiment_id)

    judge = get_scorer(name=judge_name, experiment_id=experiment_id)  # type: ignore[call]

    schema_kwargs = derive_label_schema(judge=judge, scale=scale, options=options)
    create_label_schema(**schema_kwargs)  # type: ignore[call]
    schema_name = schema_kwargs["name"]

    run_id = make_run_id(judge_name, now)
    traces = select_scored_traces(
        experiment_id=experiment_id, judge_name=judge_name,
        filter_string=filter_string, limit=limit,
    )
    trace_ids = [str(t) for t in traces["trace_id"].tolist()]
    tag_traces(trace_ids, run_id)

    if attach_agent and endpoint:
        review_app = get_review_app(experiment_id=experiment_id)  # type: ignore[call]
        if review_app is None:
            raise LabelingError(
                f"no review app found for experiment {experiment_id}; cannot attach agent "
                f"'{agent_name}'. Check the experiment id or omit --attach-agent."
            )
        review_app.add_agent(
            agent_name=agent_name, model_serving_endpoint=endpoint, overwrite=True,
        )

    session = create_labeling_session(  # type: ignore[call]
        name=session_name_for(run_id),
        assigned_users=assignees,
        label_schemas=[schema_name],
    )
    # Add the scored traces directly to the session. (The earlier UC-dataset
    # path required a catalog.schema.table name that the agent/run id can't
    # supply; add_traces takes the traces straight from search_traces.)
    session = session.add_traces(traces)

    return StartResult(
        run_id=run_id, session_url=str(getattr(session, "url", "")),
        trace_count=len(trace_ids), schema_name=schema_name,
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
    # include_spans=False: metadata-only read so the run-tagged traces still
    # come back on FEVM/private-link workspaces (blocked trace blob store);
    # MemAlign aligns from the labeled request/response + assessments, not spans.
    traces = search_traces_for_experiment(
        experiment_id, filter_string=f"tag.{RUN_TAG} = '{run_id}'",
        return_type="list", include_spans=False,
    )
    base = get_scorer(name=judge_name, experiment_id=experiment_id)  # type: ignore[call]
    from mlflow.exceptions import MlflowException
    try:
        aligned = base.align(traces=traces, optimizer=optimizer)  # type: ignore[union-attr]
    except MlflowException as e:
        # MemAlign aligns to *human* feedback; if SMEs haven't labeled the
        # run's traces yet it raises "No valid feedback records found". Turn
        # that into actionable guidance instead of a raw stack trace.
        if "feedback records" in str(e).lower():
            raise LabelingError(
                f"no SME labels found for run '{run_id}' yet. Have the assigned "
                f"reviewers label the traces in the Review App (the session URL "
                f"`label start` printed), then re-run `label align`."
            ) from e
        raise

    guidelines = [g.guideline_text for g in getattr(aligned, "_semantic_memory", []) or []]

    if new_version:
        from mlflow.genai.judges import make_judge
        new = make_judge(
            name=new_version, instructions=aligned.instructions,
            feedback_value_type=base.feedback_value_type,  # type: ignore[union-attr]
            model=base.model,  # type: ignore[union-attr]
        )
        new.register(experiment_id=experiment_id)
        registered_as = new_version
    else:
        updated = aligned.update(experiment_id=experiment_id)
        registered_as = str(getattr(updated, "name", judge_name))

    return AlignResult(judge_name=judge_name, guidelines=guidelines, registered_as=registered_as)
