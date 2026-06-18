# apx-agent label — Judge-Alignment Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-command `apx-agent label` CLI group that drives the MLflow 3 GenAI labeling + MemAlign judge-alignment loop against a deployed apx agent.

**Architecture:** A new pure-logic module `python/src/apx_agent/_labeling.py` holds independently-testable units (schema derivation, run-id/naming, experiment resolution, trace selection, two orchestrators). CLI wiring in `cli.py` adds a `label` group with `start` and `align` subcommands, reusing the existing `_fleet_resolve` selector, `_eval.app_predict_fn`, and `_mlflow_tracing.search_traces_for_experiment`. The user brings their own registered judge; apx derives the label schema from it so the schema name can't drift from the judge name.

**Tech Stack:** Python 3.12, Click, mlflow 3.12 (`mlflow.genai`), dspy (MemAlign backend, `align` only), pytest.

## Global Constraints

- **mlflow floor:** `mlflow>=3.6` (already the `eval` extra). All `mlflow.genai` calls assume 3.x `locations=` trace API.
- **dspy split:** `label start` must import only base mlflow. `label align` (MemAlign) requires a NEW optional extra `apx-agent[align]` = `["mlflow>=3.6", "dspy"]`. A missing `dspy` must surface as `pip install 'apx-agent[align]'`, never a raw `ImportError`/`MlflowException`.
- **Schema name coherence:** the label-schema `name` is always taken verbatim from the loaded judge's `.name` — never accepted as a CLI flag. This is load-bearing for `align()`.
- **No `InputBoolean`:** the API has none. Map `feedback_value_type` → input as: `bool` → `InputCategorical(options=["true", "false"])` (check `is bool` FIRST — `bool` is a subclass of `int`); `int`/`float` → `InputNumeric(min_value, max_value)` (requires `--scale`); `str` → `InputCategorical(options=[...])` (requires `--options`).
- **Embedding default:** MemAlign `embedding_model` defaults to `databricks:/databricks-gte-large-en` (avoids the `openai/text-embedding-3-small` gotcha).
- **Run-id handoff:** `start` mints `run_id = f"{judge_name}-{UTC %Y%m%dT%H%M%SZ}"` and scopes the trace tag (`apx.label.run`), dataset name, and session name to it so re-running `start` never cross-contaminates what `align` pulls.
- **Experiment resolution order:** `--experiment <id>` → `apx.mlflow.experiment_id` UC tag → clear error (the naming-convention path is deploy-time only and not available to the `label` command).
- **Tests:** run from the `python/` dir: `cd python && uv run pytest`. Fast isolated tests carry the `unit` marker. New tests live in `python/tests/`.
- **Verified API signatures (mlflow 3.12.0):**
  - `label_schemas.create_label_schema(name, *, type, title, input, instruction=None, enable_comment=False, overwrite=False) -> LabelSchema`
  - `label_schemas.InputNumeric(min_value=None, max_value=None)`, `label_schemas.InputCategorical(options: list[str])`
  - `create_labeling_session(name, *, assigned_users=None, agent=None, label_schemas=None, ...) -> LabelingSession`; the session has `.add_dataset(dataset_name=...)` and `.url`
  - `get_review_app(experiment_id=...)`; review app has `.add_agent(agent_name=..., model_serving_endpoint=..., overwrite=True)`
  - `get_scorer(*, name, experiment_id=None, version=None) -> Scorer`; a scorer exposes `.name`, `.instructions`, `.feedback_value_type`, `.align(traces=..., optimizer=...)`, `.update(experiment_id=..., ...)`
  - `from mlflow.genai.judges.optimizers import MemAlignOptimizer` (needs dspy)
  - `from mlflow.genai.datasets import create_dataset, get_dataset` (dataset has `.merge_records(df)`)

---

## File Structure

| File | Responsibility |
|------|----------------|
| `python/src/apx_agent/_labeling.py` (create) | Pure helpers + two orchestrators. mlflow.genai symbols imported at module top (monkeypatchable globals); dspy imported lazily in `align_judge`. |
| `python/src/apx_agent/cli.py` (modify) | New `label` group with `start`/`align` subcommands; reuse `_fleet_resolve`, `_fleet_select_options`, `_connect_workspace`. |
| `python/src/apx_agent/_watchdog.py` (modify) | `set_uc_tags_for_agent` gains an `experiment_id` param; emits `apx.mlflow.experiment_id` tag. |
| `python/pyproject.toml` (modify) | Add `align` optional-dependency extra; add to `all`. |
| `python/tests/test_labeling.py` (create) | Unit tests for the pure helpers + mocked orchestrators. |
| `python/tests/test_labeling_cli.py` (create) | CliRunner tests for `label start`/`label align`. |
| `docs/cli` / `README` (modify in Task 9) | Document the two commands + the `[align]` extra. |

---

## Task 1: `_labeling` module — errors, `parse_scale`, `derive_label_schema`

**Files:**
- Create: `python/src/apx_agent/_labeling.py`
- Test: `python/tests/test_labeling.py`

**Interfaces:**
- Consumes: nothing (entry task).
- Produces:
  - `class LabelingError(Exception)`
  - `parse_scale(scale: str) -> tuple[float, float]`
  - `derive_label_schema(*, judge: Any, scale: str | None, options: list[str] | None) -> dict[str, Any]` — returns kwargs for `label_schemas.create_label_schema`, with keys `name, type, title, input, instruction, enable_comment, overwrite`. `judge` is duck-typed with `.name: str`, `.instructions: str`, `.feedback_value_type: type`.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/test_labeling.py
import pytest
from types import SimpleNamespace
from mlflow.genai import label_schemas as ls
from apx_agent import _labeling


def _judge(name="domain_quality_base", ft=float, instr="rate {{ inputs }} {{ outputs }}"):
    return SimpleNamespace(name=name, instructions=instr, feedback_value_type=ft)


@pytest.mark.unit
def test_parse_scale_ok():
    assert _labeling.parse_scale("1-5") == (1.0, 5.0)
    assert _labeling.parse_scale("0.0 - 1.0") == (0.0, 1.0)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "5", "a-b", "1-2-3"])
def test_parse_scale_bad(bad):
    with pytest.raises(_labeling.LabelingError):
        _labeling.parse_scale(bad)


@pytest.mark.unit
def test_derive_numeric_schema_matches_judge():
    spec = _labeling.derive_label_schema(judge=_judge(ft=float), scale="1-5", options=None)
    assert spec["name"] == "domain_quality_base"          # verbatim from judge
    assert spec["instruction"] == "rate {{ inputs }} {{ outputs }}"
    assert spec["type"] == "feedback"
    assert spec["enable_comment"] is True
    assert spec["overwrite"] is True
    assert isinstance(spec["input"], ls.InputNumeric)
    assert (spec["input"].min_value, spec["input"].max_value) == (1.0, 5.0)


@pytest.mark.unit
def test_derive_numeric_requires_scale():
    with pytest.raises(_labeling.LabelingError, match="--scale"):
        _labeling.derive_label_schema(judge=_judge(ft=float), scale=None, options=None)


@pytest.mark.unit
def test_derive_bool_maps_to_categorical_true_false():
    spec = _labeling.derive_label_schema(judge=_judge(ft=bool), scale=None, options=None)
    assert isinstance(spec["input"], ls.InputCategorical)
    assert spec["input"].options == ["true", "false"]


@pytest.mark.unit
def test_derive_str_requires_options():
    with pytest.raises(_labeling.LabelingError, match="--options"):
        _labeling.derive_label_schema(judge=_judge(ft=str), scale=None, options=None)
    spec = _labeling.derive_label_schema(judge=_judge(ft=str), scale=None, options=["good", "bad"])
    assert isinstance(spec["input"], ls.InputCategorical)
    assert spec["input"].options == ["good", "bad"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run pytest tests/test_labeling.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent._labeling'`.

- [ ] **Step 3: Write the module**

```python
# python/src/apx_agent/_labeling.py
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

from typing import Any

try:  # mlflow is the `eval`/`align` extra
    from mlflow.genai import label_schemas as _label_schemas
except Exception:  # pragma: no cover — only without the extra
    _label_schemas = None  # type: ignore[assignment]


class LabelingError(Exception):
    """A user-facing labeling error (bad input, unmet precondition)."""


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_labeling.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_labeling.py python/tests/test_labeling.py
git commit -m "feat(labeling): derive_label_schema + parse_scale (schema name from judge)"
```

---

## Task 2: run-id and naming helpers

**Files:**
- Modify: `python/src/apx_agent/_labeling.py`
- Test: `python/tests/test_labeling.py`

**Interfaces:**
- Consumes: `LabelingError` (Task 1).
- Produces:
  - `make_run_id(judge_name: str, now: datetime) -> str` → `f"{judge_name}-{now:%Y%m%dT%H%M%SZ}"`
  - `dataset_name_for(agent_name: str, run_id: str) -> str` → `f"{agent_name}_label_{run_id}"`
  - `session_name_for(run_id: str) -> str` → `f"{run_id}_sme"`
  - `RUN_TAG = "apx.label.run"`

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_labeling.py
from datetime import datetime, timezone


@pytest.mark.unit
def test_make_run_id_is_deterministic():
    now = datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc)
    assert _labeling.make_run_id("domain_quality_base", now) == "domain_quality_base-20260617T190530Z"


@pytest.mark.unit
def test_name_helpers():
    rid = "domain_quality_base-20260617T190530Z"
    assert _labeling.dataset_name_for("payroll", rid) == f"payroll_label_{rid}"
    assert _labeling.session_name_for(rid) == f"{rid}_sme"
    assert _labeling.RUN_TAG == "apx.label.run"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run pytest tests/test_labeling.py -k "run_id or name_helpers" -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'make_run_id'`.

- [ ] **Step 3: Add the helpers**

```python
# add near the top of _labeling.py (after imports)
from datetime import datetime

RUN_TAG = "apx.label.run"


def make_run_id(judge_name: str, now: datetime) -> str:
    """Deterministic run id tying `start` to `align`."""
    return f"{judge_name}-{now:%Y%m%dT%H%M%SZ}"


def dataset_name_for(agent_name: str, run_id: str) -> str:
    return f"{agent_name}_label_{run_id}"


def session_name_for(run_id: str) -> str:
    return f"{run_id}_sme"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_labeling.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_labeling.py python/tests/test_labeling.py
git commit -m "feat(labeling): run-id + dataset/session naming helpers"
```

---

## Task 3: experiment resolution

**Files:**
- Modify: `python/src/apx_agent/_labeling.py`
- Test: `python/tests/test_labeling.py`

**Interfaces:**
- Consumes: `LabelingError` (Task 1).
- Produces: `resolve_experiment_id(*, explicit: str | None, agent_tags: dict[str, str]) -> str` — order: `explicit` → `agent_tags["apx.mlflow.experiment_id"]` → raise `LabelingError`. Also `EXPERIMENT_TAG = "apx.mlflow.experiment_id"`.

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_labeling.py
@pytest.mark.unit
def test_resolve_experiment_prefers_explicit():
    eid = _labeling.resolve_experiment_id(
        explicit="123", agent_tags={"apx.mlflow.experiment_id": "999"})
    assert eid == "123"


@pytest.mark.unit
def test_resolve_experiment_falls_back_to_tag():
    eid = _labeling.resolve_experiment_id(
        explicit=None, agent_tags={"apx.mlflow.experiment_id": "999"})
    assert eid == "999"
    assert _labeling.EXPERIMENT_TAG == "apx.mlflow.experiment_id"


@pytest.mark.unit
def test_resolve_experiment_raises_when_unresolved():
    with pytest.raises(_labeling.LabelingError, match="--experiment"):
        _labeling.resolve_experiment_id(explicit=None, agent_tags={})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run pytest tests/test_labeling.py -k experiment -q`
Expected: FAIL — `AttributeError: ... 'resolve_experiment_id'`.

- [ ] **Step 3: Add the resolver**

```python
# add to _labeling.py
EXPERIMENT_TAG = "apx.mlflow.experiment_id"


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_labeling.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_labeling.py python/tests/test_labeling.py
git commit -m "feat(labeling): experiment resolution (--experiment > UC tag > error)"
```

---

## Task 4: trace selection + tagging

**Files:**
- Modify: `python/src/apx_agent/_labeling.py`
- Test: `python/tests/test_labeling.py`

**Interfaces:**
- Consumes: `LabelingError`, `RUN_TAG`.
- Produces:
  - module globals (monkeypatchable): `search_traces_for_experiment`, `set_trace_tag` (bound at import to the real functions, or `None` without the extra).
  - `select_scored_traces(*, experiment_id: str, judge_name: str, filter_string: str | None, limit: int | None) -> Any` — returns a pandas DataFrame of traces; raises `LabelingError` if empty.
  - `tag_traces(trace_ids: list[str], run_id: str) -> int` — sets `apx.label.run=<run_id>` on each; returns count.

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_labeling.py
import pandas as pd


@pytest.mark.unit
def test_select_scored_traces_returns_df(monkeypatch):
    df = pd.DataFrame({"trace_id": ["t1", "t2"]})
    monkeypatch.setattr(_labeling, "search_traces_for_experiment", lambda exp, **kw: df)
    out = _labeling.select_scored_traces(
        experiment_id="123", judge_name="j", filter_string=None, limit=None)
    assert list(out["trace_id"]) == ["t1", "t2"]


@pytest.mark.unit
def test_select_scored_traces_empty_fails_fast(monkeypatch):
    monkeypatch.setattr(_labeling, "search_traces_for_experiment",
                        lambda exp, **kw: pd.DataFrame({"trace_id": []}))
    with pytest.raises(_labeling.LabelingError, match="--evaluate"):
        _labeling.select_scored_traces(
            experiment_id="123", judge_name="j", filter_string=None, limit=None)


@pytest.mark.unit
def test_tag_traces_sets_run_tag(monkeypatch):
    calls = []
    monkeypatch.setattr(_labeling, "set_trace_tag",
                        lambda **kw: calls.append(kw))
    n = _labeling.tag_traces(["t1", "t2"], "run-1")
    assert n == 2
    assert all(c["key"] == _labeling.RUN_TAG and c["value"] == "run-1" for c in calls)
    assert {c["trace_id"] for c in calls} == {"t1", "t2"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run pytest tests/test_labeling.py -k "select_scored or tag_traces" -q`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Add selection + tagging**

```python
# add near the mlflow imports at the top of _labeling.py
try:
    import mlflow as _mlflow
    from apx_agent._mlflow_tracing import search_traces_for_experiment
    set_trace_tag = _mlflow.set_trace_tag
except Exception:  # pragma: no cover
    search_traces_for_experiment = None  # type: ignore[assignment]
    set_trace_tag = None  # type: ignore[assignment]


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_labeling.py -q`
Expected: PASS (15 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_labeling.py python/tests/test_labeling.py
git commit -m "feat(labeling): trace selection (fail-fast) + run-tagging"
```

---

## Task 5: `start_session` orchestrator

**Files:**
- Modify: `python/src/apx_agent/_labeling.py`
- Test: `python/tests/test_labeling.py`

**Interfaces:**
- Consumes: `derive_label_schema`, `make_run_id`, `dataset_name_for`, `session_name_for`, `select_scored_traces`, `tag_traces`, `RUN_TAG`.
- Produces:
  - monkeypatchable globals: `create_label_schema`, `create_labeling_session`, `get_review_app`, `get_scorer`, `create_dataset`, `get_dataset` (bound at import or `None`).
  - `@dataclass StartResult { run_id: str, session_url: str, trace_count: int, dataset_name: str, schema_name: str }`
  - `start_session(*, experiment_id, agent_name, judge_name, scale, options, assignees, filter_string, limit, endpoint, attach_agent, now) -> StartResult`

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/test_labeling.py
@pytest.mark.unit
def test_start_session_creates_schema_with_judge_name(monkeypatch):
    judge = _judge(name="domain_quality_base", ft=float)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: judge)

    created = {}
    monkeypatch.setattr(_labeling, "create_label_schema",
                        lambda **kw: created.update(kw))
    monkeypatch.setattr(_labeling, "select_scored_traces",
                        lambda **kw: pd.DataFrame({"trace_id": ["t1", "t2"]}))
    monkeypatch.setattr(_labeling, "tag_traces", lambda ids, rid: len(ids))

    ds = SimpleNamespace(merge_records=lambda df: ds)
    monkeypatch.setattr(_labeling, "get_dataset", lambda name: (_ for _ in ()).throw(Exception()))
    monkeypatch.setattr(_labeling, "create_dataset", lambda name: ds)

    session = SimpleNamespace(add_dataset=lambda dataset_name: session,
                              url="https://x/sme")
    sess_kwargs = {}
    monkeypatch.setattr(_labeling, "create_labeling_session",
                        lambda **kw: (sess_kwargs.update(kw), session)[1])
    monkeypatch.setattr(_labeling, "get_review_app", lambda experiment_id: None)

    from datetime import datetime, timezone
    res = _labeling.start_session(
        experiment_id="123", agent_name="payroll", judge_name="domain_quality_base",
        scale="1-5", options=None, assignees=["sme@x.com"], filter_string=None,
        limit=None, endpoint=None, attach_agent=False,
        now=datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc),
    )
    # schema name MUST equal judge name; session references that schema name
    assert created["name"] == "domain_quality_base"
    assert sess_kwargs["label_schemas"] == ["domain_quality_base"]
    assert res.run_id == "domain_quality_base-20260617T190530Z"
    assert res.session_url == "https://x/sme"
    assert res.trace_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_labeling.py -k start_session -q`
Expected: FAIL — `start_session` not defined.

- [ ] **Step 3: Implement `start_session`**

```python
# add to _labeling.py (and extend the mlflow-import try block with these names)
from dataclasses import dataclass

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_labeling.py -q`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_labeling.py python/tests/test_labeling.py
git commit -m "feat(labeling): start_session orchestrator (schema+dataset+review-app+session)"
```

---

## Task 6: `align_judge` orchestrator + dspy guard

**Files:**
- Modify: `python/src/apx_agent/_labeling.py`
- Test: `python/tests/test_labeling.py`

**Interfaces:**
- Consumes: `RUN_TAG`, `get_scorer` global, `LabelingError`.
- Produces:
  - `@dataclass AlignResult { judge_name: str, guidelines: list[str], registered_as: str }`
  - `align_judge(*, experiment_id, judge_name, run_id, reflection_model, embedding_model, retrieval_k, new_version) -> AlignResult`
  - dspy missing → `LabelingError` with the `pip install 'apx-agent[align]'` message.

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_labeling.py
@pytest.mark.unit
def test_align_judge_missing_dspy_raises_friendly(monkeypatch):
    # Force the MemAlign import to fail like a missing dspy.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("mlflow.genai.judges.optimizers"):
            raise ImportError("DSPy library is required but not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(_labeling.LabelingError, match=r"apx-agent\[align\]"):
        _labeling.align_judge(
            experiment_id="123", judge_name="j", run_id="r1",
            reflection_model="databricks:/databricks-claude-sonnet-4-6",
            embedding_model="databricks:/databricks-gte-large-en",
            retrieval_k=5, new_version=None,
        )


@pytest.mark.unit
def test_align_judge_aligns_and_updates_in_place(monkeypatch):
    captured = {}

    aligned = SimpleNamespace(
        instructions="distilled...",
        _semantic_memory=[SimpleNamespace(guideline_text="be precise")],
        update=lambda **kw: captured.update(kw) or SimpleNamespace(name="j"),
    )
    base = SimpleNamespace(align=lambda **kw: (captured.update(align=kw), aligned)[1])
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: base)
    monkeypatch.setattr(_labeling, "_load_memalign",
                        lambda **kw: "OPT")  # bypass dspy import
    monkeypatch.setattr(_labeling, "search_traces_for_experiment",
                        lambda exp, **kw: ["trace-a", "trace-b"])

    res = _labeling.align_judge(
        experiment_id="123", judge_name="j", run_id="r1",
        reflection_model="databricks:/m", embedding_model="databricks:/e",
        retrieval_k=5, new_version=None,
    )
    assert res.guidelines == ["be precise"]
    assert captured["align"]["optimizer"] == "OPT"
    assert captured["align"]["traces"] == ["trace-a", "trace-b"]
    assert "experiment_id" in captured  # update() was called in-place
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run pytest tests/test_labeling.py -k align_judge -q`
Expected: FAIL — `align_judge` / `_load_memalign` not defined.

- [ ] **Step 3: Implement `align_judge` + `_load_memalign`**

```python
# add to _labeling.py
@dataclass
class AlignResult:
    judge_name: str
    guidelines: list[str]
    registered_as: str


def _load_memalign(*, reflection_model: str, embedding_model: str, retrieval_k: int) -> Any:
    """Build a MemAlignOptimizer, translating a missing dspy into guidance."""
    try:
        from mlflow.genai.judges.optimizers import MemAlignOptimizer
    except ImportError as e:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_labeling.py -q`
Expected: PASS (18 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_labeling.py python/tests/test_labeling.py
git commit -m "feat(labeling): align_judge orchestrator + dspy install guard"
```

---

## Task 7: CLI `label` group (`start` / `align`)

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add `label` group after the `fleet` group, ~line 7650)
- Test: `python/tests/test_labeling_cli.py` (create)

**Interfaces:**
- Consumes: `_connect_workspace`, `_fleet_resolve`, `_fleet_select_options` (existing in cli.py); `_labeling.start_session`, `_labeling.align_judge`, `_labeling.resolve_experiment_id`, `_labeling.LabelingError`.
- Produces: `apx-agent label start` and `apx-agent label align` commands.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/test_labeling_cli.py
import json
import pytest
from types import SimpleNamespace
from click.testing import CliRunner
from apx_agent import cli, _labeling


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.unit
def test_label_start_happy_path(runner, monkeypatch):
    agent = SimpleNamespace(uc_name="c.s.payroll", name="payroll",
                            tags={"apx.mlflow.experiment_id": "123"})
    monkeypatch.setattr(cli, "_connect_workspace", lambda p: (object(), object()))
    monkeypatch.setattr(cli, "_fleet_resolve", lambda ws, **kw: [agent])
    monkeypatch.setattr(_labeling, "start_session",
                        lambda **kw: _labeling.StartResult(
                            run_id="payroll_j-20260617T000000Z",
                            session_url="https://x/sme", trace_count=5,
                            dataset_name="payroll_label_x", schema_name="j"))
    res = runner.invoke(cli.main, [
        "label", "start", "--uc-name", "c.s.payroll",
        "--judge", "j", "--scale", "1-5", "--format", "json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["session_url"] == "https://x/sme"
    assert payload["run_id"] == "payroll_j-20260617T000000Z"


@pytest.mark.unit
def test_label_start_errors_when_not_one_agent(runner, monkeypatch):
    monkeypatch.setattr(cli, "_connect_workspace", lambda p: (object(), object()))
    monkeypatch.setattr(cli, "_fleet_resolve", lambda ws, **kw: [])
    res = runner.invoke(cli.main, ["label", "start", "--judge", "j", "--scale", "1-5"])
    assert res.exit_code != 0
    assert "exactly one" in res.output.lower() or "no agent" in res.output.lower()


@pytest.mark.unit
def test_label_align_happy_path(runner, monkeypatch):
    agent = SimpleNamespace(uc_name="c.s.payroll", name="payroll",
                            tags={"apx.mlflow.experiment_id": "123"})
    monkeypatch.setattr(cli, "_connect_workspace", lambda p: (object(), object()))
    monkeypatch.setattr(cli, "_fleet_resolve", lambda ws, **kw: [agent])
    monkeypatch.setattr(_labeling, "align_judge",
                        lambda **kw: _labeling.AlignResult(
                            judge_name="j", guidelines=["be precise"], registered_as="j"))
    res = runner.invoke(cli.main, [
        "label", "align", "--uc-name", "c.s.payroll",
        "--judge", "j", "--run", "payroll_j-20260617T000000Z", "--format", "json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["registered_as"] == "j"
    assert payload["guidelines"] == ["be precise"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run pytest tests/test_labeling_cli.py -q`
Expected: FAIL — `No such command 'label'`.

- [ ] **Step 3: Add the `label` group to cli.py**

Insert after the `fleet` command group (the block ending near line 7650, before the `canary` group):

```python
@main.group(cls=_ApxGroup)
def label() -> None:
    """Judge-alignment: create SME labeling sessions and align judges."""


def _label_one_agent(profile: str | None, **selectors: Any) -> Any:
    """Resolve exactly one apx agent or raise a UsageError."""
    ws, _ = _connect_workspace(profile)
    agents = _fleet_resolve(ws, **selectors)
    if len(agents) != 1:
        raise click.UsageError(
            f"selector resolved {len(agents)} agents; narrow it so exactly one matches "
            f"(use --uc-name catalog.schema.model)."
        )
    return agents[0]


@label.command("start")
@_fleet_select_options
@click.option("--judge", "judge_name", required=True, help="Registered judge (scorer) name.")
@click.option("--scale", default=None, help="Numeric judge scale, MIN-MAX (e.g. 1-5).")
@click.option("--options", "options_csv", default=None, help="Categorical judge options, comma-separated.")
@click.option("--experiment", "experiment", default=None, help="MLflow experiment id (overrides the agent tag).")
@click.option("--assignee", "assignees", multiple=True, help="SME email (repeatable). Defaults to you.")
@click.option("--filter", "filter_string", default=None, help="MLflow trace filter_string.")
@click.option("--limit", default=None, type=int, help="Max traces to include.")
@click.option("--endpoint", default=None, help="Serving endpoint for the Review App agent.")
@click.option("--no-review-agent", "attach_agent", flag_value=False, default=True,
              help="Do not attach the agent to the Review App.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def label_start_cmd(
    catalog: str | None, schema: str | None, name_glob: str | None,
    where_exprs: tuple[str, ...], uc_names: tuple[str, ...],
    judge_name: str, scale: str | None, options_csv: str | None,
    experiment: str | None, assignees: tuple[str, ...], filter_string: str | None,
    limit: int | None, endpoint: str | None, attach_agent: bool,
    profile: str | None, fmt: str,
) -> None:
    """Create an SME labeling session for a deployed agent's judge."""
    from datetime import datetime, timezone
    from apx_agent import _labeling

    agent = _label_one_agent(
        profile, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    try:
        eid = _labeling.resolve_experiment_id(explicit=experiment, agent_tags=agent.tags)
        result = _labeling.start_session(
            experiment_id=eid, agent_name=agent.name, judge_name=judge_name,
            scale=scale, options=(options_csv.split(",") if options_csv else None),
            assignees=list(assignees) or [],
            filter_string=filter_string, limit=limit, endpoint=endpoint,
            attach_agent=attach_agent, now=datetime.now(timezone.utc),
        )
    except _labeling.LabelingError as e:
        raise click.ClickException(str(e)) from e

    if fmt == "json":
        import json as _json
        click.echo(_json.dumps(result.__dict__))
    else:
        click.echo(f"Labeling session: {result.session_url}")
        click.echo(f"  run-id:  {result.run_id}   (pass to `label align --run`)")
        click.echo(f"  traces:  {result.trace_count}   schema/judge: {result.schema_name}")


@label.command("align")
@_fleet_select_options
@click.option("--judge", "judge_name", required=True, help="Registered judge (scorer) name.")
@click.option("--run", "run_id", required=True, help="Run id printed by `label start`.")
@click.option("--experiment", "experiment", default=None, help="MLflow experiment id (overrides the agent tag).")
@click.option("--reflection-model", default="databricks:/databricks-claude-sonnet-4-6",
              help="Model used by MemAlign to distill guidelines.")
@click.option("--embedding-model", default="databricks:/databricks-gte-large-en",
              help="Embedding model for MemAlign retrieval.")
@click.option("--retrieval-k", default=5, type=int, help="Examples retrieved per evaluation.")
@click.option("--new-version", default=None, help="Register the aligned judge under a new name (preserve the original).")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def label_align_cmd(
    catalog: str | None, schema: str | None, name_glob: str | None,
    where_exprs: tuple[str, ...], uc_names: tuple[str, ...],
    judge_name: str, run_id: str, experiment: str | None,
    reflection_model: str, embedding_model: str, retrieval_k: int,
    new_version: str | None, profile: str | None, fmt: str,
) -> None:
    """Align the judge from a finished labeling run (requires apx-agent[align])."""
    from apx_agent import _labeling

    agent = _label_one_agent(
        profile, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    try:
        eid = _labeling.resolve_experiment_id(explicit=experiment, agent_tags=agent.tags)
        result = _labeling.align_judge(
            experiment_id=eid, judge_name=judge_name, run_id=run_id,
            reflection_model=reflection_model, embedding_model=embedding_model,
            retrieval_k=retrieval_k, new_version=new_version,
        )
    except _labeling.LabelingError as e:
        raise click.ClickException(str(e)) from e

    if fmt == "json":
        import json as _json
        click.echo(_json.dumps(result.__dict__))
    else:
        click.echo(f"Aligned judge registered as: {result.registered_as}")
        for i, g in enumerate(result.guidelines, 1):
            click.echo(f"  {i}. {g}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_labeling_cli.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_labeling_cli.py
git commit -m "feat(cli): apx-agent label group — start + align subcommands"
```

---

## Task 8: deploy-time `apx.mlflow.experiment_id` tag (additive)

**Files:**
- Modify: `python/src/apx_agent/_watchdog.py` (`set_uc_tags_for_agent`, ~line 606)
- Modify: `python/src/apx_agent/cli.py` (deploy call site, ~line 3657 where `set_uc_tags_for_agent` is called)
- Test: `python/tests/test_labeling.py`

**Interfaces:**
- Consumes: existing `set_uc_tags_for_agent`.
- Produces: `set_uc_tags_for_agent(..., experiment_id: str | None = None)` writes `apx.mlflow.experiment_id` when given. Read back by `_labeling.resolve_experiment_id` (Task 3) via the agent tag.

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/test_labeling.py
@pytest.mark.unit
def test_set_uc_tags_includes_experiment_id():
    from apx_agent import _watchdog

    class FakeClient:
        def __init__(self):
            self.tags = {}
        def set_registered_model_tag(self, *, name, key, value):
            self.tags[key] = value

    class FakeAgent:
        _name = "payroll"

    client = FakeClient()
    # emit_agent_metadata needs a real-ish agent; patch it to a minimal dict.
    import apx_agent._watchdog as w
    orig = w.emit_agent_metadata
    w.emit_agent_metadata = lambda agent, name=None, model=None: {"name": "payroll", "model": "m"}
    try:
        _watchdog.set_uc_tags_for_agent(
            FakeAgent(), registered_model_name="c.s.payroll",
            experiment_id="555", mlflow_client=client,
        )
    finally:
        w.emit_agent_metadata = orig
    assert client.tags.get("apx.mlflow.experiment_id") == "555"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_labeling.py -k experiment_id_tag -q`
Expected: FAIL — `set_uc_tags_for_agent() got an unexpected keyword argument 'experiment_id'`.

- [ ] **Step 3: Add the param + tag write**

In `_watchdog.py`, add `experiment_id: str | None = None` to the `set_uc_tags_for_agent` signature (after `name`), then, right after `tags = _build_uc_tag_payload(metadata)`:

```python
    tags = _build_uc_tag_payload(metadata)
    if experiment_id:
        tags["apx.mlflow.experiment_id"] = str(experiment_id)
```

In `cli.py` at the deploy call site (~line 3657), thread the already-resolved experiment id (`eid` from `_ensure_experiment_id`) into the call:

```python
    written = set_uc_tags_for_agent(
        agent,
        registered_model_name=registered_model_name,
        model=model,
        name=effective_agent_name,
        experiment_id=eid,   # NEW — lets `apx-agent label` resolve traces without --experiment
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && uv run pytest tests/test_labeling.py -q && cd python && uv run pytest tests/test_fleet.py -q`
Expected: PASS (labeling 19 tests; fleet unchanged/green).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_watchdog.py python/src/apx_agent/cli.py python/tests/test_labeling.py
git commit -m "feat(deploy): record apx.mlflow.experiment_id UC tag for label trace resolution"
```

---

## Task 9: `[align]` extra + documentation

**Files:**
- Modify: `python/pyproject.toml` (optional-dependencies, ~line 28-39)
- Modify: `README.md` (CLI reference) and/or `docs/` CLI page
- Test: none (config + docs); a smoke import check serves as the gate.

**Interfaces:** none — packaging + docs.

- [ ] **Step 1: Add the `align` extra**

In `python/pyproject.toml` `[project.optional-dependencies]`, add `align` and include it in `all`:

```toml
eval = ["mlflow>=3.6"]
align = ["mlflow>=3.6", "dspy"]
uc = [
    "unitycatalog-ai>=0.1.0",
]
lakebase = [
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.1",
]
all = ["apx-agent[eval,uc,lakebase,align]"]
```

- [ ] **Step 2: Verify the lock resolves and `start`-side imports stay dspy-free**

Run:
```bash
cd python && uv lock && uv run python -c "import apx_agent._labeling as l; print('start-side import ok:', l.start_session is not None)"
```
Expected: lock resolves; prints `start-side import ok: True` (importing the module must NOT require dspy).

- [ ] **Step 3: Document the commands**

Add a `apx-agent label` subsection to the README CLI reference (mirror the `fleet` entry) covering: the two-command loop, BYO registered judge, `--scale`/`--options`, the run-id handoff, and that `align` needs `pip install 'apx-agent[align]'`. Keep it to a short example block:

```markdown
### `apx-agent label` — judge alignment

Collect SME ratings on a deployed agent's traces and align its LLM judge.

    # 1. (you) register a judge to the agent's experiment with make_judge().register()
    # 2. create the SME labeling session
    apx-agent label start --uc-name cat.sch.my_agent --judge domain_quality --scale 1-5 --assignee sme@co.com
    #    -> prints the Review App URL + a run-id; SMEs label out-of-band
    # 3. after labeling, align the judge (needs: pip install 'apx-agent[align]')
    apx-agent label align --uc-name cat.sch.my_agent --judge domain_quality --run <run-id>
```

- [ ] **Step 4: Run the full unit suite + pyright**

Run:
```bash
cd python && uv run pytest -m unit -q && uv run pyright src/apx_agent/_labeling.py
```
Expected: all unit tests PASS; pyright clean on the new module.

- [ ] **Step 5: Commit**

```bash
git add python/pyproject.toml python/uv.lock README.md
git commit -m "feat(labeling): add [align] extra (dspy) + document the label commands"
```

---

## Self-Review

**Spec coverage:**
- §2 two commands → Tasks 5/6 (orchestrators) + Task 7 (CLI). ✅
- §3 start flow steps 1-10 → selector (Task 7 `_label_one_agent`), experiment resolution (Task 3), judge load + schema derivation (Tasks 1/5), trace select/tag (Task 4), dataset + review-app + session (Task 5), output text/json (Task 7). ✅
- §3 `--evaluate` opt-in: **deferred** — see Deviations below.
- §4 align flow → Task 6 + Task 7. ✅
- §5 dependencies (`[align]`/dspy split) → Task 9 + the dspy guard in Task 6. ✅
- §6 additive experiment-id tag → Task 8. ✅
- §7 error handling → LabelingError paths across Tasks 1/3/4/6, `_label_one_agent` (Task 7), dspy guard (Task 6). ✅
- §8 testing → unit tests on every pure helper + mocked orchestrators + CliRunner; reality tests remain manual (not in this plan), matching the spec. ✅

**Placeholder scan:** every code step contains real, runnable code and exact commands. No TBD/TODO. ✅

**Type consistency:** `StartResult`/`AlignResult`/`LabelingError` names, `make_run_id`/`derive_label_schema`/`resolve_experiment_id`/`select_scored_traces`/`tag_traces`/`start_session`/`align_judge` signatures are consistent between their defining task and their consumers (Tasks 5/6/7). The monkeypatched globals (`get_scorer`, `create_label_schema`, `search_traces_for_experiment`, `set_trace_tag`, dataset/session/review-app fns) are all declared in Tasks 4/5. ✅

**Deviations from spec (intentional, low-risk):**
1. **`--evaluate <inputs>` cold-start path is deferred.** The spec lists it as opt-in; this plan ships the default pre-scored path + a fail-fast error that *names* `--evaluate` as the remedy, but does not implement the `mlflow.genai.evaluate` wiring (reusing `_eval.app_predict_fn`). Rationale: it is the one piece needing a live endpoint + input file, it roughly doubles the orchestration surface, and the default path is independently shippable. **Recommend a follow-up Task 10** to add `--evaluate` once the core loop is verified. Flag this to the user before execution so they can opt to fold it in now.
2. **Naming-convention experiment fallback excluded** from `resolve_experiment_id` (explicit → tag → error only), per the spec's own "never load-bearing" note — the bundle name/target aren't available to the `label` command.

These are the only gaps. If the user wants `--evaluate` in the initial cut, add Task 10 (write `score_traces` in `_labeling`, wire a `--evaluate <path>` option in `label start` that calls `mlflow.genai.evaluate(data=<jsonl>, predict_fn=app_predict_fn(endpoint_url, token), scorers=[judge])` then proceeds to selection) before starting.
