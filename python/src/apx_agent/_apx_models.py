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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._ui_edit import _require_python_binding

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
    ``…/deny``: ``{id, status, decided_by, decided_at}`` (status is
    ``"approved"`` | ``"denied"``). ``decided_by`` is the forwarded identity of
    the decider (``None`` in local dev with no forwarded user).

    Both are no-body POSTs keyed by the path id, so there is no request model.
    The no-store and unknown-id paths (404) return a ``JSONResponse``.
    """

    id: str
    status: str
    decided_by: str | None = None
    decided_at: str | None = None


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


class WorkspaceDiscoveredAgent(BaseModel):
    """One agent from ``GET /_apx/workspace-agents`` (Apps A2A and/or UC tags)."""

    name: str
    source: str  # "app" | "uc"
    app_name: str | None = None
    url: str | None = None
    description: str | None = None
    tools: list[str] = []
    tool_count: int = 0
    state: str = "unknown"
    uc_name: str | None = None
    model_endpoint: str | None = None


class WorkspaceAgentsResponse(BaseModel):
    """Shape of ``GET /_apx/workspace-agents`` — workspace peer discovery."""

    agents: list[WorkspaceDiscoveredAgent]


class WorkspaceFunctionInfo(BaseModel):
    """One UC function from ``GET /_apx/workspace-functions``."""

    full_name: str
    catalog: str
    schema_name: str
    name: str
    comment: str | None = None


class WorkspaceFunctionsResponse(BaseModel):
    """Shape of ``GET /_apx/workspace-functions``."""

    catalog: str
    schema_name: str
    functions: list[WorkspaceFunctionInfo]


class WorkspaceApiInfo(BaseModel):
    """One API surface from ``GET /_apx/workspace-apis``.

    ``kind`` is ``serving_endpoint``, ``genie_space``, or ``vector_search_index``.
    Genie and Vector Search include Managed MCP URLs when the workspace host
    is known; serving endpoints expose the HTTP invocations URL instead.
    """

    kind: str
    name: str
    state: str | None = None
    description: str | None = None
    url: str | None = None
    mcp_url: str | None = None
    extra: dict[str, Any] | None = None


class WorkspaceApisResponse(BaseModel):
    """Shape of ``GET /_apx/workspace-apis``."""

    apis: list[WorkspaceApiInfo]


class DiscoverTargetInfo(BaseModel):
    """One agent assignment from ``GET /_apx/discover/targets``."""

    name: str
    kind: str
    eligible: bool
    reason: str | None = None
    sub_agents: list[str] = []


class DiscoverTargetsResponse(BaseModel):
    """Shape of ``GET /_apx/discover/targets``."""

    targets: list[DiscoverTargetInfo]
    source_path: str | None = None


class DiscoverWireAgentRequest(BaseModel):
    """Body for ``POST /_apx/discover/wire-agent`` / ``unwire-agent``.

    ``url`` is required for wire; unwire may omit it when ``ref`` is set.
    """

    url: str = ""
    name: str | None = None
    app_name: str | None = None
    target: str = "agent"
    use_env: bool = True
    ref: str | None = None  # for unwire: exact sub_agents entry to remove


class DiscoverWireToolRequest(BaseModel):
    """Body for ``POST /_apx/discover/wire-tool`` / ``unwire-tool``."""

    kind: str  # uc_function | genie_space | vector_search_index
    target: str = "agent"
    full_name: str | None = None  # uc_function
    space_id: str | None = None  # genie_space
    title: str | None = None  # genie display name for slug
    index_name: str | None = None  # vector_search_index
    columns: list[str] | None = None
    binding_name: str | None = None  # override / unwire key

    @field_validator("binding_name")
    @classmethod
    def _binding_name_is_identifier(cls, v: str | None) -> str | None:
        """#630: reject non-identifier / keyword names before the handler runs."""
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        return _require_python_binding(stripped)


class DiscoverWireResponse(BaseModel):
    """Shared success shape for Discover wire/unwire mutators."""

    ok: bool
    restart_required: bool = False
    applied_live: bool = False
    target: str | None = None
    ref: str | None = None
    binding_name: str | None = None
    already_present: bool = False
    error: str | None = None


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


class TopologyTracingResponse(BaseModel):
    """``GET /_apx/topology/tracing`` — where this agent writes MLflow traces."""

    experiment_id: str | None = None
    experiment_name: str | None = None
    workspace_host: str | None = None
    experiment_url: str | None = None
    configured: bool = False


class TopologyTracingSetRequest(BaseModel):
    """Body of ``POST /_apx/topology/tracing`` — set ``MLFLOW_EXPERIMENT_ID``."""

    experiment_id: str


class TopologyTracingSetResponse(BaseModel):
    """Success shape of ``POST /_apx/topology/tracing``."""

    ok: bool
    experiment_id: str
    experiment_name: str | None = None
    experiment_url: str | None = None
    restart_hint: str | None = None


class LastRouteResponse(BaseModel):
    """``GET /_apx/traces/last-route`` — topology nodes/edges hit by the latest turn."""

    trace_id: str | None = None
    node_ids: list[str] = []
    edge_ids: list[str] = []
    tool_names: list[str] = []
    span_count: int = 0


# ── Wave 2 / PR-W2a: codegen file-ops writes (edit / preview / delete) ────────
#
# The first *request* models on source-mutating routes. These are un-hidden per
# the locked policy (#2 — full honest swagger): execution stays token-gated by
# ``_dev_write_guard`` (403 without ``APX_DEV_UI_TOKEN``), so documenting the
# body shape exposes nothing exploitable. A missing required field now yields
# ``422`` from FastAPI (policy #1); the handlers' own error paths (syntax error,
# file-not-found, tool-not-found) stay ``JSONResponse`` and bypass the response
# models. These three routes are the file-ops half of PR-W2, split from the
# LLM+ASGI codegen chain (suggest/new/create-tool/generate-tools/wire-agent).


class EditSaveRequest(BaseModel):
    """Body of ``POST /_apx/edit`` — the full new ``agent_router.py`` source.

    ``content`` is required (the editor always sends it); an empty string is a
    valid save. The handler compiles it first and returns a 200 ``{ok: false,
    error}`` on ``SyntaxError`` (bypassing :class:`EditSaveResponse`).
    """

    content: str


class EditSaveResponse(BaseModel):
    """Success shape of ``POST /_apx/edit``: ``{ok: true, restart_required}``.

    ``restart_required`` is ``True`` when the source was also written back to
    the workspace (a deployed app needs a restart to load it).
    """

    ok: bool
    restart_required: bool


class EditPreviewRequest(BaseModel):
    """Body of ``POST /_apx/edit/preview`` — candidate source to extract tool
    schemas from, without writing anything. ``source`` is required."""

    source: str


class ToolDeleteResponse(BaseModel):
    """Success shape of ``DELETE /_apx/tools/{fn_name}``: ``{ok: true}``.

    The not-found and post-removal syntax-error paths return a 200
    ``{ok: false, error}`` ``JSONResponse`` that bypasses this model.
    """

    ok: bool


# ── Wave 2 / PR-W2b: codegen LLM+ASGI chain ──────────────────────────────────
#
# The five coupled codegen-write routes. ``create-tool`` and ``generate-tools``
# do no work themselves — they ``httpx.ASGITransport``-POST internally into
# ``/_apx/tools/suggest`` then ``/_apx/tools/new``, and that internal POST
# RE-RUNS FastAPI body validation. So ``ToolNewRequest`` is load-bearing: it
# must accept whatever ``suggest``'s LLM emits, hence **every field optional +
# ``extra="ignore"``** — a required field would 422 the *internal* chain, not
# just bad UI input. The conditional success shapes (``tools/new``'s
# ``wired``/``agents``/``note``) use ``dict[str, Any]`` responses so no key is
# stripped off the wire (the rootId/agentName lesson from #276). Un-hidden per
# policy #2; execution stays token-gated by ``_dev_write_guard``.


class ToolSuggestRequest(BaseModel):
    """Body of ``POST /_apx/tools/suggest``: ``{prompt}`` (required; blank-after-
    strip is rejected by the handler with a 200 ``{ok: false}``)."""

    prompt: str


class ToolSuggestResponse(BaseModel):
    """Success shape of ``POST /_apx/tools/suggest``: ``{ok, spec}`` where
    ``spec`` is the LLM-generated tool scaffold (arbitrary JSON object). The
    no-prompt / no-agent / non-JSON paths return a 200 ``{ok: false, error}``
    ``JSONResponse`` that bypasses this model."""

    ok: bool
    spec: dict[str, Any]


class ToolNewRequest(BaseModel):
    """Body of ``POST /_apx/tools/new`` — a tool spec. **Fully permissive**:
    every field optional, ``extra="ignore"``. This is the spec ``suggest``
    emits and the ASGI chain re-POSTs, so it must never 422 on a missing or
    extra field. ``params`` is a list of ``{name, type, desc}`` dicts.
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    description: str | None = None
    params: list[dict[str, Any]] = []
    returns: str | None = None
    body: str | None = None
    agent: str | None = None


class ToolFromDescriptionRequest(BaseModel):
    """Shared body of ``POST /_apx/setup/create-tool`` AND
    ``POST /_apx/wizard/generate-tools`` (functionally identical): ``{description}``.

    ``extra="ignore"`` because the wizard UI also sends
    ``table``/``catalog``/``schema``/``warehouse_id`` that the handler doesn't
    read (the extra-fields trap). Blank-after-strip → handler 400.
    """

    model_config = ConfigDict(extra="ignore")

    description: str


class ToolFromDescriptionResponse(BaseModel):
    """Success shape of create-tool / generate-tools: ``{ok, tool_name}``.

    The chained-failure passthroughs (``suggest``/``new`` ``{ok: false}``) and
    the no-description 400 return a ``JSONResponse`` and bypass this model.
    """

    ok: bool
    tool_name: str


class WireAgentRequest(BaseModel):
    """Body of ``POST /_apx/setup/wire-agent``: ``{behavior, agent_name}``.

    ``behavior`` required (blank-after-strip → handler 400); ``agent_name``
    defaults to ``"agent"``.
    """

    behavior: str
    agent_name: str = "agent"


class WireAgentResponse(BaseModel):
    """Success shape of ``POST /_apx/setup/wire-agent``: ``{ok, tools,
    instructions}`` — the LLM-selected tool names and generated instructions
    (``tools`` empty when the agent has no tools yet). The no-agent (503),
    no-model / no-behavior (400), import-failure (500), and LLM-failure (200)
    paths return a ``JSONResponse`` and bypass this model.
    """

    ok: bool
    tools: list[str]
    instructions: str


# ── Field-description curation: GET/POST /_apx/grounding/columns (#292) ───────


class ColumnCuration(BaseModel):
    """One column's curation row: ``{column, type, current, suggested}``.

    ``current`` is the description in the OKF bundle now; ``suggested`` is the
    Unity Catalog COMMENT when it is non-empty and differs from ``current``
    (else ``""``).
    """

    column: str
    type: str
    current: str
    suggested: str


class TableColumns(BaseModel):
    """One table's columns in the curation view."""

    table: str
    columns: list[ColumnCuration]


class GroundingColumnsResponse(BaseModel):
    """Success shape of ``GET /_apx/grounding/columns`` — the per-column current-
    vs-suggested curation state for the agent's OKF bundle. ``tables`` is empty
    when the project has no OKF bundle; then ``can_generate`` / ``generate_from``
    drive the empty-state Generate-pack CTA when a catalog.schema is known.
    ``schema`` shadows ``BaseModel.schema`` so the field is aliased (same trick
    as :class:`ToolSchemaResponse`).
    """

    model_config = ConfigDict(populate_by_name=True)

    catalog: str
    schema_: str = Field(alias="schema")
    tables: list[TableColumns]
    can_generate: bool = False
    generate_from: str = ""


class ColumnDescriptionsRequest(BaseModel):
    """Body of ``POST /_apx/grounding/columns`` — accepted descriptions as
    ``{table: {column: description}}``. Only listed columns are written; blank
    descriptions are no-ops (reject = don't include it)."""

    accepted: dict[str, dict[str, str]]


class ColumnDescriptionsSaveResponse(BaseModel):
    """Success shape of ``POST /_apx/grounding/columns``: ``{ok, modified}``
    (number of tables whose bundle file was rewritten). The no-bundle path
    returns a 404 ``JSONResponse`` that bypasses this model."""

    ok: bool
    modified: int


class GroundingGenerateRequest(BaseModel):
    """Body of ``POST /_apx/grounding/generate`` — optional catalog/schema
    override (defaults to the live DataAgent / agent.py / env source) and
    ``force`` to overwrite an existing ``.apx/okf`` pack."""

    model_config = ConfigDict(populate_by_name=True)

    catalog: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    force: bool = False


class GroundingGenerateResponse(BaseModel):
    """Success shape of ``POST /_apx/grounding/generate``: pack written + whether
    ``knowledge=`` was wired into project config. Error paths return a
    ``JSONResponse`` and bypass this model."""

    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    catalog: str
    schema_: str = Field(alias="schema")
    table_count: int
    knowledge_wired: bool
    restart_required: bool = True


class ColumnSuggestRequest(BaseModel):
    """Body of ``POST /_apx/grounding/suggest`` (#292 phase C) — the table to
    LLM-generate column descriptions for."""

    table: str


class ColumnSuggestResponse(BaseModel):
    """Success shape of ``POST /_apx/grounding/suggest``: ``{ok, suggestions}``
    where ``suggestions`` maps column → AI-generated description (empty when the
    LLM/parse failed). The no-model (400) / no-bundle (404) paths return a
    ``JSONResponse`` and bypass this model."""

    ok: bool
    suggestions: dict[str, str]


# ── Wave 2 / PR-W3: setup writes / composition (#280) ─────────────────────────
#
# Source-mutating setup/composer writes. Strict request models (missing required
# field → 422; blank-after-strip stays the handler's 200 ``{ok: false}``). The
# composer routes (agents / agent-pattern / compose) return conditional-key
# shapes, so they use ``dict[str, Any]`` responses to avoid stripping keys (the
# #276 lesson). Un-hidden per policy; the POSTs are token-gated by the dev guard.


class SetupSaveRequest(BaseModel):
    """Body of ``POST /_apx/setup``: ``{catalog, schema, warehouse_id,
    generate_instructions?}``. Required fields present-but-blank are rejected by
    the handler (200 ``{ok: false}``). ``schema`` is aliased (shadows
    ``BaseModel.schema``)."""

    model_config = ConfigDict(populate_by_name=True)

    catalog: str
    schema_: str = Field(alias="schema")
    warehouse_id: str
    generate_instructions: bool = False


class GenerateInstructionsRequest(BaseModel):
    """Body of ``POST /_apx/setup/generate-instructions``: ``{catalog, schema,
    warehouse_id}``."""

    model_config = ConfigDict(populate_by_name=True)

    catalog: str
    schema_: str = Field(alias="schema")
    warehouse_id: str


class SetupInstructionsResponse(BaseModel):
    """Success shape of ``POST /_apx/setup`` and
    ``/_apx/setup/generate-instructions``: ``{ok, instructions}`` (the generated
    text, or ``None`` when not regenerated). Error paths (missing fields,
    generator failure) return a ``JSONResponse`` and bypass this model."""

    ok: bool
    instructions: str | None = None


class ApplyInstructionsRequest(BaseModel):
    """Body of ``POST /_apx/setup/apply-instructions``: ``{instructions}``
    (blank-after-strip → handler 200 ``{ok: false}``)."""

    instructions: str


class SaveAgentsRequest(BaseModel):
    """Body of ``POST /_apx/setup/agents``: ``{nodes}``. Composer nodes carry
    extra metadata, so each node is a permissive dict."""

    nodes: list[dict[str, Any]]


class SetAgentPatternRequest(BaseModel):
    """Body of ``POST /_apx/setup/agent-pattern``: ``{pattern}``."""

    pattern: str


class ComposeRequest(BaseModel):
    """Body of ``POST /_apx/setup/compose``: ``{pattern, nodes, start?}``. Nodes
    are permissive dicts (``route_key`` / ``route_description`` extras)."""

    pattern: str
    nodes: list[dict[str, Any]]
    start: str | None = None


# ── Wave 3 / PR-P1: replay (type but KEEP HIDDEN) (#281) ──────────────────────
#
# The one exception to policy #2 (un-hide everything). These two routes stay
# ``include_in_schema=False``: ``replay/tool`` executes an arbitrary registered
# tool with the caller's forwarded OBO credentials, and ``replay/llm`` invokes
# the configured model directly — the most-privileged pair on the surface. We
# type their bodies for strict validation but do NOT advertise their shape in
# Scalar/OpenAPI, even to authenticated users. There is no UI wiring; the
# request models + their tests ARE the contract.
#
# Permissiveness (policy #3): ``args`` and ``messages`` carry arbitrary tool
# arguments / chat messages, so their *contents* stay untyped (``dict[str, Any]``
# / ``list[dict[str, Any]]``). Only the envelope is strict — a missing
# ``tool_name`` or a missing/empty ``messages`` now yields ``422`` from FastAPI
# (policy #1), replacing the handlers' former typed ``400``. Semantic failures
# stay in the handler and keep their codes: tool-not-found ``404``, no model
# configured ``400``, no agent context ``503``.


class ReplayToolRequest(BaseModel):
    """Body of ``POST /_apx/replay/tool``: ``{tool_name, args?}``.

    ``tool_name`` is required (missing → ``422``); the handler still returns
    ``404`` when it names no registered tool. ``args`` is the tool's keyword
    arguments — an arbitrary JSON object forwarded verbatim to the internal
    ``/tools/{name}`` POST, which re-runs that tool's own validation — so it is
    intentionally permissive and defaults to ``{}``.
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ReplayLlmRequest(BaseModel):
    """Body of ``POST /_apx/replay/llm``: ``{messages, model?}``.

    ``messages`` is a non-empty list of chat-message dicts (missing or empty →
    ``422``); its elements stay permissive (``dict[str, Any]``) since they are
    forwarded straight to the model. ``model`` optionally overrides the agent's
    configured endpoint; when omitted the handler falls back to it and returns a
    semantic ``400`` if none is configured.
    """

    model_config = ConfigDict(extra="ignore")

    messages: list[dict[str, Any]] = Field(min_length=1)
    model: str | None = None
