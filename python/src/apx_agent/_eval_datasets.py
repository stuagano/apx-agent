"""MLflow GenAI evaluation datasets for ``apx-agent eval`` (#1247).

A managed evaluation dataset is a Unity Catalog table that
``mlflow.genai.datasets`` creates and versions. APX uses the upstream API
unchanged, and adds three things on top:

* ``get_eval_dataset`` and ``add_eval_records`` turn a missing table or
  missing grants into an ``EvalDatasetError`` that says what to fix;
* ``eval_rows_from_file`` and ``traces_for_dataset`` read the sources a
  dataset is built from: local JSON/JSONL eval files, and traces with their
  labeled assessments;
* ``record_eval_dataset`` tags an eval run with the dataset it scored, as
  ``apx.eval_dataset.key`` / ``.uri`` / ``.version``. These are the same
  ``apx.<kind>.*`` tags ``_resolved_versions`` writes. The digest is read
  back from the run input ``mlflow.genai.evaluate`` logged, so the tag
  matches what MLflow recorded.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ._resolved_versions import ResolvedVersion, version_tags

# catalog.schema.table, without backticks. Anything else is a file path.
_UC_NAME = re.compile(r"^[^./\\\s]+\.[^./\\\s]+\.[^./\\\s]+$")

_GRANTS = {
    "read": "USE CATALOG, USE SCHEMA and SELECT on the table",
    "create": "USE CATALOG, USE SCHEMA and CREATE TABLE on the schema",
    "write": "USE CATALOG, USE SCHEMA and MODIFY on the table",
}


class EvalDatasetError(RuntimeError):
    """A dataset call failed. The message names the dataset and the fix."""


def is_dataset_name(reference: str) -> bool:
    """Whether ``reference`` names a UC table (``catalog.schema.table``)."""
    return bool(_UC_NAME.match(reference))


def _status(exc: BaseException) -> str | None:
    """``not_found`` / ``permission`` for the error shapes the dataset stack raises."""
    code = getattr(exc, "error_code", None)
    response = getattr(exc, "response", None)
    http_status = getattr(response, "status_code", None)
    kind = type(exc).__name__
    if code in ("RESOURCE_DOES_NOT_EXIST", "NOT_FOUND") or http_status == 404 or kind in ("NotFound", "ResourceDoesNotExist"):
        return "not_found"
    if code in ("PERMISSION_DENIED", "UNAUTHORIZED") or http_status in (401, 403) or kind in ("PermissionDenied", "Unauthenticated"):
        return "permission"
    return None


def _explain(name: str, action: str, exc: BaseException) -> EvalDatasetError:
    status = _status(exc)
    if status == "not_found":
        return EvalDatasetError(
            f"Eval dataset {name} not found: {exc}. Create it with "
            f"`apx-agent eval dataset add {name} <evalset.jsonl>`, or check the catalog and schema exist."
        )
    if status == "permission":
        return EvalDatasetError(
            f"Permission denied to {action} eval dataset {name}: {exc}. "
            f"The caller needs {_GRANTS[action]}."
        )
    return EvalDatasetError(f"Could not {action} eval dataset {name}: {exc}")


def get_eval_dataset(name: str) -> Any:
    """``mlflow.genai.datasets.get_dataset(name=...)``, with errors that say what to fix."""
    from mlflow.genai.datasets import get_dataset

    try:
        return get_dataset(name=name)
    except Exception as exc:
        raise _explain(name, "read", exc) from exc


def add_eval_records(name: str, records: Any, *, experiment_id: str | None) -> Any:
    """Merge ``records`` into dataset ``name``, creating it first when it does not exist.

    ``records`` is anything ``EvaluationDataset.merge_records`` takes: a list of
    ``{"inputs", "expectations"}`` dicts or a ``mlflow.search_traces`` DataFrame.
    Records with the same inputs merge into one. Returns the updated dataset.
    """
    from mlflow.genai.datasets import create_dataset, get_dataset

    try:
        dataset = get_dataset(name=name)
    except Exception as exc:
        if _status(exc) != "not_found":
            raise _explain(name, "read", exc) from exc
        try:
            dataset = create_dataset(name=name, experiment_id=experiment_id)
        except Exception as create_exc:
            raise _explain(name, "create", create_exc) from create_exc
    try:
        return dataset.merge_records(records)
    except Exception as exc:
        raise _explain(name, "write", exc) from exc


def eval_rows_from_file(path: Path) -> list[dict[str, Any]]:
    """Read a JSON or JSONL eval file as dataset records.

    ``apx-agent eval run`` accepts ``inputs`` nested once or twice; a dataset
    stores it once, so ``{"inputs": {"inputs": {...}}}`` is unwrapped.
    """
    text = path.read_text()
    try:
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvalDatasetError(f"Cannot parse {path} as JSON/JSONL: {exc}") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise EvalDatasetError(f"{path} must hold a list of eval rows (objects).")
    unwrapped: list[dict[str, Any]] = []
    for row in rows:
        inputs = row.get("inputs")
        if isinstance(inputs, dict) and set(inputs) == {"inputs"} and isinstance(inputs["inputs"], dict):
            row = {**row, "inputs": inputs["inputs"]}
        unwrapped.append(row)
    return unwrapped


def traces_for_dataset(
    experiment: str,
    *,
    filter_string: str | None,
    max_results: int | None,
    labeled: bool,
) -> Any:
    """Traces from ``experiment`` as a DataFrame ``merge_records`` accepts.

    The ``trace`` column carries each trace's inputs, and its expectation
    assessments become the record's expectations. ``labeled`` keeps only
    traces with a human assessment (labeling-session or SME feedback).
    """
    from ._labeling import _assessment_mapping, _is_human_assessment
    from ._mlflow_tracing import search_traces_for_experiment

    kwargs: dict[str, Any] = {"return_type": "pandas"}
    if filter_string:
        kwargs["filter_string"] = filter_string
    if max_results:
        kwargs["max_results"] = max_results
    df = search_traces_for_experiment(experiment, **kwargs)
    if not labeled:
        return df
    if "assessments" not in df.columns:
        return df.iloc[0:0]

    def has_human(cell: Any) -> bool:
        mappings = [_assessment_mapping(item) for item in cell] if isinstance(cell, list) else []
        return any(raw is not None and _is_human_assessment(raw) for raw in mappings)

    return df[[has_human(cell) for cell in df["assessments"]]]


def record_eval_dataset(run_id: str, *, key: str, uri: str) -> ResolvedVersion | None:
    """Tag eval run ``run_id`` with the dataset it scored. ``None`` when MLflow logged no input.

    ``key`` is what the caller passed (a UC name or a file path); ``uri`` is
    the dataset id for a managed dataset, else the path. The version is the
    digest of the dataset input ``mlflow.genai.evaluate`` logged on the run.
    """
    from mlflow import MlflowClient

    client = MlflowClient()
    dataset_inputs = client.get_run(run_id).inputs.dataset_inputs
    if not dataset_inputs:
        return None
    logged = dataset_inputs[0].dataset
    version = ResolvedVersion(kind="eval_dataset", key=key, name=logged.name, uri=uri, version=logged.digest)
    for tag, value in version_tags([version]).items():
        client.set_tag(run_id, tag, value)
    return version
