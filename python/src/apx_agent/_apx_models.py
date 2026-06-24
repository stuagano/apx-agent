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

from typing import Any

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


# ── Wave 1 / PR-R1: setup-discovery reads (GET, no request bodies) ────────────
#
# All of these are read-only ``GET`` routes the Setup/composer UI calls to
# enumerate workspace and source-file state. The pattern matches the eval
# routes above: the *success* path returns a raw dict/list so ``response_model``
# both validates the shape and publishes it to the native OpenAPI schema, while
# each handler's *error* path returns a ``JSONResponse`` (500 ``{error}`` on an
# SDK failure, or a 200 ``{ok: false}`` / ``[{error}]`` degrade) that bypasses
# the model. ``catalogs``/``schemas``/``tables`` need no model — they return a
# bare ``list[str]``.


class WarehouseInfo(BaseModel):
    """One row of ``GET /_apx/setup/warehouses``: ``{id, name, state}``.

    ``name`` falls back to ``id`` in the handler when the warehouse is unnamed,
    and ``state`` is the SDK enum rendered to its string value.
    """

    id: str
    name: str
    state: str


class AgentNodeInfo(BaseModel):
    """One agent parsed from the local ``agent_router.py`` for the composer.

    Shape produced by ``_parse_agent_nodes`` (AST scan): ``name`` is the
    assigned variable, ``wrapper`` is the orchestration class wrapping a bare
    ``Agent`` (``SequentialAgent`` …) or ``None`` for a direct agent, ``tools``
    is the list of tool function names, and ``instructions`` is the literal
    instructions string when present.
    """

    name: str
    wrapper: str | None = None
    tools: list[str]
    instructions: str | None = None


class ToolParam(BaseModel):
    """One parameter of a tool in ``GET /_apx/setup/tools``: ``{name, type}``."""

    name: str
    type: str


class ToolInfo(BaseModel):
    """One tool of ``GET /_apx/setup/tools``: ``{name, description, params}``.

    Sourced from the JSON-schema blocks mined out of ``agent_router.py``;
    ``params`` is flattened to ``name``/``type`` pairs for the UI's tool list.
    """

    name: str
    description: str
    params: list[ToolParam]


class SchemaColumn(BaseModel):
    """One column in a ``ToolSchemaResponse`` table: ``{name, type}``."""

    name: str
    type: str


class ToolSchemaResponse(BaseModel):
    """Success shape of ``GET /_apx/tools/schema`` — the grounding-schema
    context the agent uses to know its tables and columns.

    ``{ok, catalog, schema, tables}`` where ``tables`` maps a table name to its
    columns; ``source`` is ``"mined"`` only when the live ``information_schema``
    query returned nothing and the shape was recovered from source. ``schema``
    shadows :meth:`BaseModel.schema`, so the field is named ``schema_`` with an
    alias — FastAPI serialises response models by alias, keeping the wire key
    ``schema`` (same trick as :class:`JudgeResponse`).

    Both error paths (no agent/config, the catch-all) return a 200
    ``{ok: false, error}`` ``JSONResponse`` that bypasses this model.
    """

    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    catalog: str
    schema_: str = Field(alias="schema")
    tables: dict[str, list[SchemaColumn]]
    source: str | None = None


class VsIndexInfo(BaseModel):
    """One vector-search index of ``GET /_apx/setup/vs-indexes``.

    ``{endpoint, endpoint_state, index, source_table, ready, columns}`` — enough
    to pre-fill ``VS_INDEX`` / ``VS_COLUMNS`` in the composer. ``columns`` is the
    list of source column names. When listing endpoints fails the handler
    returns a single-element ``[{error}]`` via ``JSONResponse`` (200, bypassing
    this model) rather than raising.
    """

    endpoint: str
    endpoint_state: str
    index: str
    source_table: str
    ready: bool
    columns: list[str]


class AgentPatternResponse(BaseModel):
    """Shape of ``GET /_apx/setup/agent-pattern``: ``{type}``.

    The orchestration wrapper class of the ``agent`` node (``"Agent"`` when the
    source can't be read or no wrapper is present).
    """

    type: str


class ProbeResult(BaseModel):
    """Success shape of ``GET /_apx/setup/probe-json``: ``{status, latency_ms,
    url}``.

    A token-gated, side-effecting GET (it makes an outbound HTTP request behind
    the dev-UI write token and an SSRF allowlist), so it is published to OpenAPI
    for shape only — execution stays 403-gated by ``_dev_write_guard``. The
    bad-URL / SSRF-reject (400) and connection-failure (200 ``{error, …}``)
    paths return a ``JSONResponse`` that bypasses this model.
    """

    status: int
    latency_ms: int
    url: str


# ── Wave 1 / PR-R2: trace + approval reads ───────────────────────────────────
#
# Two route families. The **approval** routes are plain JSON (a list read plus
# two no-body POSTs that act on a path id — response models only, no request
# models). The **trace** routes do HTML/JSON content negotiation on a ``fmt``
# query param: ``response_model`` documents only the ``fmt=json`` branch, while
# the default HTML branch returns an ``HTMLResponse`` (a Response object, which
# FastAPI returns verbatim — never touched by the model) and the error branches
# return a ``JSONResponse`` that bypasses it.


class ApprovalInfo(BaseModel):
    """One pending approval in ``GET /_apx/approvals`` — the fields the chat
    banner renders: ``{id, tool_name, arguments, reason}``.

    ``arguments`` is the exact tool-call argument dict the approval covers;
    ``reason`` is the policy reason that triggered the ASK (``None`` when the
    gate raised without one). The empty-store and 503 error paths return a
    ``JSONResponse`` that bypasses this model.
    """

    id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str | None = None


class ApprovalActionResponse(BaseModel):
    """Success shape shared by ``POST /_apx/approvals/{id}/approve`` and
    ``…/deny``: ``{id, status}`` (status is ``"approved"`` | ``"denied"``).

    Both are no-body POSTs keyed by the path id, so there is no request model.
    The no-store and unknown-id paths (404) return a ``JSONResponse``.
    """

    id: str
    status: str


class TraceRow(BaseModel):
    """One row of ``GET /_apx/traces?fmt=json`` — the trace-list panel.

    Merges the MLflow tracking-store search with the in-process ring buffer
    (ring-buffer-only rows carry ``None`` timings and empty previews). The
    default (no ``fmt``) HTML rendering of the same route bypasses this model.
    """

    trace_id: str
    state: str
    request_time_ms: int | None = None
    duration_ms: int | None = None
    request_preview: str
    response_preview: str


class TraceDetailResponse(BaseModel):
    """Success shape of ``GET /_apx/traces/{id}?fmt=json``: ``{trace_id,
    spans}``.

    ``spans`` is the serialised span list (from the ring buffer or a
    ``mlflow.get_trace`` fetch). The artifact-egress-blocked (200 ``{error}``),
    not-found (404 ``{error}``), and HTML branches all bypass this model.
    """

    trace_id: str
    spans: list[dict[str, Any]]


# ── Wave 1 / PR-R3: orphan JSON reads ────────────────────────────────────────
#
# A grab-bag of read-only ``GET`` routes that aren't part of a larger family.
# Two have clean, stable shapes worth modelling strictly (workspace-context and
# the topology graph the React UI consumes); the other three (the curated
# ``/_apx/openapi.json`` document, the composite ``probe/checks`` health
# response, and the per-node ``topology/inspect`` detail) are large,
# heterogeneous, or standard-spec JSON objects where a strict model would add
# brittleness without value — they un-hide with ``response_model=dict[str,
# Any]`` (documents "a JSON object", validates nothing restrictive) in _dev.py.


class ResourceRef(BaseModel):
    """One agent-declared resource in ``WorkspaceContextResponse.resources``:
    ``{kind, identifier}`` (e.g. ``{"kind": "uc_table", "identifier":
    "main.sales.orders"}``)."""

    kind: str
    identifier: str


class WorkspaceContextResponse(BaseModel):
    """Shape of ``GET /_apx/workspace-context`` — the Context tab's workspace
    identity + agent resource summary.

    Always 200: ``user`` degrades to ``"unknown"`` and the resource lists to
    empty rather than erroring, so there is no bypass path.
    """

    host: str
    user: str
    resources: list[ResourceRef]
    used_catalogs: list[str]
    used_schemas: list[str]


class TopologyNode(BaseModel):
    """One node of ``TopologyResponse.nodes``: ``{id, type, label,
    description}``.

    ``extra="allow"`` so any node-type-specific keys a future ``build_topology``
    adds pass through untouched instead of being silently dropped on the wire.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    label: str
    description: str | None = None


class TopologyEdge(BaseModel):
    """One edge of ``TopologyResponse.edges``: ``{id, source, target, kind}``."""

    model_config = ConfigDict(extra="allow")

    id: str
    source: str
    target: str
    kind: str


class TopologyResponse(BaseModel):
    """Success shape of ``GET /_apx/topology.json`` — the agent graph the
    react-flow topology UI renders: ``{rootId, agentName, nodes, edges}``.

    ``rootId`` is the root agent node's id (the graph's entry point) and
    ``agentName`` is the served agent's name; both must be modelled (a raw
    return + ``response_model`` would otherwise strip them off the wire). The
    no-context error path (503 ``{error}``) returns a ``JSONResponse`` and
    bypasses this model.
    """

    rootId: str
    agentName: str
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
