"""Request/response contracts for the dev UI's ``/_apx/*`` routes.

This module is the shared home for the strict Pydantic models that type and
validate the dev UI's HTTP surface. It exists as a *separate* module (rather
than inline in :mod:`apx_agent._dev`) so the dev-UI hardening work can land as
parallel, per-route-group PRs without every one of them colliding in the same
region of ``_dev.py`` — each PR adds its models here and a single import line
there.

The pilot (PR #213) typed the three read-only ``GET`` routes with inline
response models. This module follows the same spirit for the **eval** routes,
adding the first strict *request* models (eval has ``POST`` bodies):

* :class:`EvalCaseIn` — one element of the ``POST /_apx/eval/data`` body.
* :class:`EvalDataSaveResponse` — the ``POST /_apx/eval/data`` success shape.
* :class:`EvalCaseResponse` — one row of the ``GET /_apx/eval/data`` list.
* :class:`JudgeRequest` — the ``POST /_apx/eval/judge`` body.
* :class:`JudgeResponse` — the ``POST /_apx/eval/judge`` success shape.

Design rule shared by every model here: **document reality, never reshape it.**
Request models reject genuinely-malformed bodies (missing required fields,
wrong container type → ``422``) while letting the UI's divergent-but-valid case
shapes pass untouched (``extra="ignore"``). Response models exist primarily for
the native OpenAPI schema; where a handler returns a ``JSONResponse`` directly
the model is bypassed at runtime, so it can never strip a field off the wire.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ── POST /_apx/eval/data ─────────────────────────────────────────────────────


class EvalCaseIn(BaseModel):
    """One eval case in the ``POST /_apx/eval/data`` body (a JSON list).

    The dev UI emits cases with **divergent** shapes depending on which panel
    saved them — some carry ``expected`` (keyword match), others
    ``expected_judge`` (LLM criterion), plus per-run metadata (``status``,
    ``response``, ``trace_id``, ``last_run_ms``, ``judge_verdict`` …). The only
    field every case-creation path in the embedded JS always sets is
    ``question`` (see ``_ui_chat.py`` ``rows.push`` / ``evalRows.push`` sites),
    so that is the one field modelled strictly.

    ``extra="ignore"`` lets the rest of each case through validation without a
    ``422``; the handler re-reads the **raw** request body for persistence, so
    those un-modelled fields are written to disk unchanged — this model is the
    shape *gate*, not the persisted projection.
    """

    model_config = ConfigDict(extra="ignore")

    question: str


class EvalDataSaveResponse(BaseModel):
    """Success shape of ``POST /_apx/eval/data``: ``{"ok": true, "count": N}``.

    ``count`` is the number of cases persisted. Error paths (503 when
    ``agent_router.py`` is not found, 500 on an OS write error) return a
    ``JSONResponse`` from the handler and so bypass this model.
    """

    ok: bool
    count: int


class EvalCaseResponse(BaseModel):
    """One row of the ``GET /_apx/eval/data`` list.

    Mirrors the persisted eval-case shape the dev UI reads back. The fields are
    all optional (cases diverge — see :class:`EvalCaseIn`) and ``extra="allow"``
    keeps any additional persisted keys. This model is **documentation only**:
    the ``GET`` handler returns the parsed JSON via ``JSONResponse``, so the
    bytes on the wire are the persisted file verbatim and this model never
    filters them.
    """

    model_config = ConfigDict(extra="allow")

    question: str | None = None
    expected: str | None = None
    expected_judge: str | None = None
    status: str | None = None
    response: str | None = None
    judge_verdict: str | None = None
    judge_reason: str | None = None
    trace_id: str | None = None
    last_run_ms: int | None = None
    duration_ms: int | None = None


# ── POST /_apx/eval/judge ────────────────────────────────────────────────────


class JudgeRequest(BaseModel):
    """Body of ``POST /_apx/eval/judge`` — LLM-as-judge scoring.

    ``question``/``response``/``criterion`` are required: a missing key or a
    wrong type yields ``422`` via this model (the handler additionally rejects
    blank-after-strip values). ``model`` is optional — the handler falls back to
    the served agent's configured model when it is omitted.
    """

    question: str
    response: str
    criterion: str
    model: str | None = None


class JudgeResponse(BaseModel):
    """Success shape of ``POST /_apx/eval/judge``.

    Mirrors the dict the handler returns on a completed judge call:
    ``{ok, pass, verdict, reason, duration_ms, model}``. ``pass`` is a Python
    keyword, so the field is named ``passed`` with an alias; FastAPI serialises
    response models by alias, so the wire key stays ``pass``.

    The judge's *error* paths — no agent context (503), blank fields (422),
    no model configured (400), and the LLM-call failure (200 with
    ``{ok: false, error}``) — all return a ``JSONResponse`` and bypass this
    model.
    """

    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    passed: bool = Field(alias="pass")
    verdict: str
    reason: str
    duration_ms: int
    model: str
