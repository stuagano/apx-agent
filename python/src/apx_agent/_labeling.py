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

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

try:  # mlflow is the `eval`/`align` extra
    from mlflow.genai import label_schemas as _label_schemas
except Exception:  # pragma: no cover — only without the extra
    _label_schemas = None  # type: ignore[assignment]

try:
    import mlflow as _mlflow
    from apx_agent._mlflow_tracing import search_traces_for_experiment
    set_trace_tag = _mlflow.set_trace_tag
except Exception:  # pragma: no cover
    _mlflow = None  # type: ignore[assignment]
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
ALIGN_KIND_TAG = "apx.kind"
ALIGN_KIND_VALUE = "memalign"
ALIGN_GUIDELINES_ARTIFACT = "guidelines.json"
_ALIGN_PARAM_LIMIT = 500


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


def parse_scale(scale: str) -> "tuple[float, float]":  # noqa: PYI024  # ponytail: named fields would add noise for a 2-element coordinate pair
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
    # Add traces to the session by loading each one individually via
    # mlflow.get_trace() rather than passing the search_traces DataFrame.
    # search_traces with include_spans=False returns span-less objects that
    # break add_traces on private-link / FEVM workspaces (the copy path
    # requires a root span). mlflow.get_trace() fetches individual traces with
    # full span data and works on FEVM. Fixes #718.
    full_traces = []
    skipped: list[str] = []
    if _mlflow is not None:
        for tid in trace_ids:
            try:
                full_traces.append(_mlflow.get_trace(tid))
            except Exception as exc:
                skipped.append(tid)
                logger.warning("label start: skipping trace %s (could not fetch spans): %s", tid, exc)
    if skipped:
        logger.warning(
            "label start: %d of %d matched traces were unavailable and not added to the session",
            len(skipped), len(trace_ids),
        )
    if full_traces:
        session = session.add_traces(full_traces)

    # trace_count reflects what was actually added, not what was matched — a
    # session that silently received fewer traces must not report the full count.
    return StartResult(
        run_id=run_id, session_url=str(getattr(session, "url", "")),
        trace_count=len(full_traces), schema_name=schema_name,
    )


@dataclass
class AlignResult:
    judge_name: str
    guidelines: list[str]
    registered_as: str
    run_id: str | None = None


@dataclass
class AlignHistoryRun:
    run_id: str
    start_time: str | None
    judge_name: str
    registered_as: str
    trace_count: int
    guidelines: list[str]
    guideline_count: int


def _mlflow_api(explicit: Any | None) -> Any:
    if explicit is not None:
        return explicit
    if _mlflow is None:
        raise LabelingError(
            "alignment history requires mlflow. Install with: pip install 'apx-agent[eval]'"
        )
    return _mlflow


def _guidelines_param(guidelines: list[str]) -> str | None:
    payload = json.dumps(guidelines, ensure_ascii=False)
    if len(payload) <= _ALIGN_PARAM_LIMIT:
        return payload
    return None


def _start_time_str(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return str(value)


def _records_from_search(runs_df: Any) -> list[dict[str, Any]]:
    if hasattr(runs_df, "to_dict"):
        return list(runs_df.to_dict(orient="records"))
    records: list[dict[str, Any]] = []
    for item in runs_df or []:
        if hasattr(item, "info"):
            info = item.info
            data = item.data if hasattr(item, "data") else None
            params = data.params if data is not None and hasattr(data, "params") else {}
            tags = data.tags if data is not None and hasattr(data, "tags") else {}
            records.append({
                "run_id": info.run_id,
                "start_time": info.start_time if hasattr(info, "start_time") else None,
                "params.judge_name": params.get("judge_name"),
                "params.registered_as": params.get("registered_as"),
                "params.trace_count": params.get("trace_count"),
                "params.guideline_count": params.get("guideline_count"),
                "params.guidelines_json": params.get("guidelines_json"),
                "tags.apx.kind": tags.get(ALIGN_KIND_TAG),
            })
        elif isinstance(item, dict):
            records.append(item)
    return records


def _guidelines_from_record(rec: dict[str, Any], api: Any) -> list[str]:
    raw = rec.get("params.guidelines_json")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    run_id = rec.get("run_id")
    if not run_id:
        return []
    artifacts = api.artifacts if hasattr(api, "artifacts") else None
    if artifacts is None or not hasattr(artifacts, "load_dict"):
        return []
    try:
        loaded = artifacts.load_dict(f"runs:/{run_id}/{ALIGN_GUIDELINES_ARTIFACT}")
    except Exception as exc:
        logger.warning("label history: could not load guidelines for run %s: %s", run_id, exc)
        return []
    if isinstance(loaded, dict):
        parsed = loaded.get("guidelines")
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    if isinstance(loaded, list):
        return [str(item) for item in loaded]
    return []


def log_align_run(
    *,
    experiment_id: str,
    judge_name: str,
    registered_as: str,
    trace_count: int,
    guidelines: list[str],
    mlflow_api: Any | None = None,
) -> str | None:
    """Persist a successful MemAlign as a filterable MLflow run.

    Tags the run ``apx.kind=memalign`` so eval history can find it without
    mixing in ``eval run`` metric rows. Long guideline lists live in the
    ``guidelines.json`` artifact because MLflow params are capped at 500
    characters; a short JSON copy is also logged as a param when it fits.
    Persistence failures are logged and return None — they must not undo
    an alignment that already succeeded.
    """
    try:
        api = _mlflow_api(mlflow_api)
    except LabelingError as exc:
        logger.warning("label align: not persisted (%s)", exc)
        return None
    params: dict[str, str] = {
        "judge_name": judge_name,
        "registered_as": registered_as,
        "trace_count": str(trace_count),
        "guideline_count": str(len(guidelines)),
    }
    compact = _guidelines_param(guidelines)
    if compact is not None:
        params["guidelines_json"] = compact
    start_kw: dict[str, Any] = {
        "experiment_id": experiment_id,
        "run_name": f"memalign-{judge_name}",
    }
    if hasattr(api, "active_run") and api.active_run() is not None:
        start_kw["nested"] = True
    try:
        with api.start_run(**start_kw) as run:
            if hasattr(api, "set_tag"):
                api.set_tag(ALIGN_KIND_TAG, ALIGN_KIND_VALUE)
            if hasattr(api, "log_params"):
                api.log_params(params)
            if hasattr(api, "log_dict"):
                api.log_dict({"guidelines": guidelines}, ALIGN_GUIDELINES_ARTIFACT)
            info = run.info if hasattr(run, "info") else None
            if info is not None and hasattr(info, "run_id"):
                return str(info.run_id)
    except Exception as exc:
        logger.warning("label align: failed to persist memalign run: %s", exc)
        return None
    return None


def list_align_runs(
    *,
    experiment_id: str,
    judge_name: str | None = None,
    max_results: int = 50,
    mlflow_api: Any | None = None,
) -> list[AlignHistoryRun]:
    """Return MemAlign runs newest-first, optionally filtered by judge."""
    api = _mlflow_api(mlflow_api)
    filters = [f'tags.{ALIGN_KIND_TAG} = "{ALIGN_KIND_VALUE}"']
    if judge_name:
        safe = judge_name.replace("\\", "\\\\").replace('"', '\\"')
        filters.append(f'params.judge_name = "{safe}"')
    if not hasattr(api, "search_runs"):
        raise LabelingError("mlflow.search_runs is not available")
    runs_df = api.search_runs(
        experiment_ids=[experiment_id],
        filter_string=" and ".join(filters),
        order_by=["start_time DESC"],
        max_results=max_results,
    )
    history: list[AlignHistoryRun] = []
    for rec in _records_from_search(runs_df):
        run_id = rec.get("run_id")
        if not run_id:
            continue
        guidelines = _guidelines_from_record(rec, api)
        count_raw = rec.get("params.guideline_count")
        try:
            guideline_count = int(count_raw) if count_raw is not None and count_raw != "" else len(guidelines)
        except (TypeError, ValueError):
            guideline_count = len(guidelines)
        trace_raw = rec.get("params.trace_count")
        try:
            trace_count = int(trace_raw) if trace_raw is not None and trace_raw != "" else 0
        except (TypeError, ValueError):
            trace_count = 0
        judge = rec.get("params.judge_name")
        registered = rec.get("params.registered_as")
        history.append(
            AlignHistoryRun(
                run_id=str(run_id),
                start_time=_start_time_str(rec.get("start_time")),
                judge_name=str(judge) if judge is not None else "",
                registered_as=str(registered) if registered is not None else "",
                trace_count=trace_count,
                guidelines=guidelines,
                guideline_count=guideline_count if guideline_count else len(guidelines),
            )
        )
    return history


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
    # Load traces with full spans via mlflow.get_trace() — MemAlign needs the
    # root span's inputs/outputs to generate guidelines. search_traces with
    # include_spans=False returns span-less objects that MemAlign cannot read.
    # mlflow.get_trace() works on FEVM/private-link workspaces. Fixes #718.
    thin = search_traces_for_experiment(
        experiment_id, filter_string=f"tag.{RUN_TAG} = '{run_id}'",
        return_type="list", include_spans=False,
    )
    traces = []
    skipped_align: list[str] = []
    if _mlflow is not None:
        for t in thin:
            try:
                traces.append(_mlflow.get_trace(t.info.trace_id))
            except Exception as exc:
                skipped_align.append(t.info.trace_id)
                logger.warning("label align: skipping trace %s (could not fetch spans): %s", t.info.trace_id, exc)
    if skipped_align:
        logger.warning(
            "label align: %d of %d traces were unavailable and excluded from alignment",
            len(skipped_align), len(thin),
        )
    if not traces:
        raise LabelingError(
            f"no traces found for run '{run_id}'. Check the run id or re-run `label start`."
        )

    base = get_scorer(name=judge_name, experiment_id=experiment_id)  # type: ignore[call]
    from mlflow.exceptions import MlflowException

    # Session-level scorers (instructions use {{ conversation }}) do not support
    # .align(). Fall back to a trace-level judge with the same instructions but
    # using {{ inputs }} / {{ outputs }} template variables. Fixes #719.
    if getattr(base, "is_session_level_scorer", False):
        from mlflow.genai.judges import make_judge
        base = make_judge(
            name=judge_name,
            instructions=(
                "Input: {{ inputs }}\nOutput: {{ outputs }}\n\n"
                + (getattr(base, "instructions", None) or "")
            ),
            feedback_value_type=base.feedback_value_type,  # type: ignore[union-attr]
            model=base.model,  # type: ignore[union-attr]
        )

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
        from mlflow.genai.scorers import ScorerSamplingConfig
        updated = aligned.update(
            experiment_id=experiment_id,
            sampling_config=ScorerSamplingConfig(sample_rate=1.0),
        )
        registered_as = str(getattr(updated, "name", judge_name))

    logged_run_id = log_align_run(
        experiment_id=experiment_id,
        judge_name=judge_name,
        registered_as=registered_as,
        trace_count=len(traces),
        guidelines=guidelines,
    )
    return AlignResult(
        judge_name=judge_name,
        guidelines=guidelines,
        registered_as=registered_as,
        run_id=logged_run_id,
    )
