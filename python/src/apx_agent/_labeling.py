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

from datetime import datetime
from typing import Any

try:  # mlflow is the `eval`/`align` extra
    from mlflow.genai import label_schemas as _label_schemas
except Exception:  # pragma: no cover — only without the extra
    _label_schemas = None  # type: ignore[assignment]


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
