"""Agent protocol models, type aliases, and context objects."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


class _ToolFn(Protocol):
    """Minimal protocol for tool functions — carries __name__ and __doc__."""

    __name__: str
    __doc__: str | None

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


# Hook callables — sync or async, both accepted
BeforeToolHook: TypeAlias = Callable[[str, dict[str, Any]], Any]
"""Called before each tool dispatch: ``hook(tool_name, arguments)``.
Return value is ignored; raising aborts the tool call and propagates."""
AfterToolHook: TypeAlias = Callable[[str, dict[str, Any], Any], Any]
"""Called after each tool dispatch: ``hook(tool_name, arguments, result)``.
Return value is ignored; raising propagates after the tool has run."""
BeforeModelHook: TypeAlias = Callable[[list[Any]], Any]
"""Called before each LLM invocation: ``hook(prompt_messages)``.
``prompt_messages`` is the langchain message list passed to the model.
Return value is ignored; raising aborts the model call."""
AfterModelHook: TypeAlias = Callable[[Any], Any]
"""Called after each LLM invocation: ``hook(response)``.
``response`` is the langchain LLMResult/AIMessage produced by the model.
Return value is ignored; raising propagates after the response is in hand."""

# Agent-level callbacks (ADK-compatible naming)
BeforeAgentCallback: TypeAlias = Callable[[list["Message"]], Any]
"""Called before the agent processes a request: ``callback(messages)``.
Return value is ignored; raising prevents the agent from running."""
AfterAgentCallback: TypeAlias = Callable[[str], Any]
"""Called after the agent produces its final response: ``callback(text)``.
Return value is ignored."""
OnModelErrorCallback: TypeAlias = Callable[[Exception], Any]
"""Called when the model invocation raises: ``callback(exception)``.
Return value is ignored; the original exception still propagates."""
OnToolErrorCallback: TypeAlias = Callable[[str, Exception], Any]
"""Called when a tool call raises: ``callback(tool_name, exception)``.
Return value is ignored; the original exception still propagates."""

# Guardrail callables — return None to pass, or a string to short-circuit
InputGuardrailFn: TypeAlias = Callable[[list["Message"]], "str | None"]
"""Called with the incoming messages before the LLM sees them.
Return ``None`` to let the request through, or a non-empty string to reject it
(the string is returned as the agent's response)."""
OutputGuardrailFn: TypeAlias = Callable[[str], "str | None"]
"""Called with the agent's final text response.
Return ``None`` to pass through, or a non-empty string to replace the output."""

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class GuardrailsConfig(BaseModel):
    """Data-only declaration of built-in guards.

    Maps to ``[tool.apx.agent.guardrails]`` in pyproject.toml.  All guards
    produced here are *additive* over code-defined guards — code hooks run
    first, then config gates.  See ``_guards.build_config_guards``.

    ``extra="forbid"`` is intentional: a typo'd guard key that silently
    disables protection is a security regression; fail loud at startup.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_tools: list[str] | None = None
    """Tool allowlist — ``ToolAllowlist(allowed_tools)``.  ``None`` = no
    allowlist (all tools permitted).  Applied as a ``before_tool`` gate."""

    blocked_tools: list[str] = []
    """Tool denylist — ``ToolDenylist(blocked_tools)``.  Applied as a
    ``before_tool`` gate.  Empty list = no denylist."""

    rate_limit: int | None = None
    """Global calls-per-minute cap — ``RateLimit(per_minute=rate_limit)``.
    ``None`` = no rate limit.  A single bucket shared across all callers
    (per-principal limiting requires a code-defined ``principal_key``)."""

    rate_limit_burst: int | None = None
    """Burst cap for the rate limiter — ``RateLimit(burst=rate_limit_burst)``.
    ``None`` defaults to ``rate_limit`` (one token per interval, no burst).
    Ignored when ``rate_limit`` is ``None``."""

    injection_detection: bool = False
    """When ``True``, appends ``prompt_injection_heuristic()`` to the agent's
    ``input_guardrails`` list to flag common injection attempts at message
    ingestion time."""


StoreType = Literal["inmemory", "delta", "lakebase", "managed"]


class MemoryBackendConfig(BaseModel):
    """Declarative memory backend — maps to ``[tool.apx.agent.memory]``."""

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"
    embedding_model: str | None = None
    embedding_dim: int | None = None
    table_name: str | None = None
    index_name: str | None = None
    auto_create: bool = True
    database: str | None = None
    host: str | None = None
    ensure_extension: bool = True
    store_name: str | None = None
    """For ``type='managed'``: the UC memory store's ``catalog.schema.name``."""
    namespace_default: str = "default"
    tool_prefix: str = ""
    include: list[str] | None = None
    validate_at_boot: bool = True


class ExampleBackendConfig(BaseModel):
    """Declarative example backend — maps to ``[tool.apx.agent.example]``."""

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"
    embedding_model: str | None = None
    embedding_dim: int | None = None
    table_name: str | None = None
    index_name: str | None = None
    auto_create: bool = True
    database: str | None = None
    host: str | None = None
    ensure_extension: bool = True
    agent_id: str | None = None
    """Partition key for example rows — defaults to ``config.name`` at attach time."""
    tool_prefix: str = ""
    include: list[str] | None = None
    validate_at_boot: bool = True


class SessionBackendConfig(BaseModel):
    """Declarative session backend — maps to ``[tool.apx.agent.session]``."""

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"
    table_name: str | None = None
    auto_create: bool = True
    database: str | None = None
    host: str | None = None
    warehouse_id: str | None = None
    validate_at_boot: bool = True


# Memory-knob helpers — shared by LlmAgent (base) and CoworkerAgent (default "persistent")
_KNOB_TO_TYPE: dict[str, StoreType | None] = {
    "off": None,
    "inmemory": "inmemory",
    "local": "inmemory",
    "persistent": "delta",
    "delta": "delta",
    "managed": "managed",
}


def normalize_memory_knob(
    value: str,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    name: str | None = None,
) -> "tuple[MemoryBackendConfig | None, SessionBackendConfig | None]":
    """Map a one-word memory tier to ``(MemoryBackendConfig, SessionBackendConfig)``.

    Returns ``(None, None)`` for ``"off"``.  Raises for ``"lakebase"`` (needs
    explicit TOML blocks) and for unknown values.

    When *catalog* and *schema* are provided (DataAgent / CoworkerAgent), the
    delta table names are derived from them so memory lands in the same UC
    location as the agent's data.  Falls back to ``main.default`` for bare
    ``LlmAgent(memory="persistent")`` callers that have no catalog/schema.
    """
    v = (value or "").strip().lower()
    if v == "lakebase":
        raise ValueError(
            "memory='lakebase' needs connection details the one-word knob can't "
            "carry — add explicit [tool.apx.agent.memory] and "
            "[tool.apx.agent.session] blocks with type='lakebase' "
            "(host, database, embedding_model, embedding_dim)."
        )
    if v not in _KNOB_TO_TYPE:
        raise ValueError(
            f"memory={value!r} is not a valid tier; use one of: off, inmemory "
            "(alias local), persistent (alias delta), or an explicit "
            "[tool.apx.agent.memory] block for lakebase."
        )
    tier = _KNOB_TO_TYPE[v]
    if tier is None:
        return (None, None)
    if tier == "managed":
        # Managed Agent Memory is a UC memory store named like the delta memory
        # table. It configures LONG-TERM memory only; short-term/session is
        # deferred to the LangGraph checkpointer (or an explicit
        # [tool.apx.agent.session] block) — so no session backend here.
        if catalog and schema:
            raw = (name or schema).lower()
            slug = re.sub(r"[^a-z0-9_]", "_", raw).strip("_") or "agent"
            store_name = f"{catalog}.{schema}.apx_{slug}_memory"
        else:
            store_name = "main.default.apx_memories"
        return (MemoryBackendConfig(type="managed", store_name=store_name), None)
    if tier == "delta":
        if catalog and schema:
            raw = (name or schema).lower()
            slug = re.sub(r"[^a-z0-9_]", "_", raw).strip("_") or "agent"
            table_name: str | None = f"{catalog}.{schema}.apx_{slug}_memory"
            session_table: str | None = f"{catalog}.{schema}.apx_{slug}_sessions"
        else:
            table_name = "main.default.apx_memories"
            session_table = "main.default.apx_sessions"
    else:
        table_name = None
        session_table = None
    return (
        MemoryBackendConfig(type=tier, table_name=table_name),
        SessionBackendConfig(type=tier, table_name=session_table),
    )


class SkillConfig(BaseModel):
    """A loadable skill — a markdown procedure file surfaced as an on-demand tool.

    Skills are declared in the ``skills:`` block of a YAML spec and compiled
    into ``[[tool.apx.tools]]`` entries by ``generate_project``.  At runtime
    the agent can call the skill by name to retrieve its markdown content.

    :param name: Tool name the LLM will call, e.g. ``"sql_analysis"``.
    :param description: One-sentence description shown in the tool schema,
        e.g. ``"Best practices for writing SQL against the sales schema"``.
    :param path: Path to the markdown file relative to the YAML spec,
        e.g. ``"skills/sql_analysis.md"``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    path: str


class AgentConfig(BaseModel):
    """Agent configuration — loaded from [tool.apx.agent] in pyproject.toml or constructed directly."""

    name: str
    description: str = ""
    model: str = "databricks-meta-llama-3-3-70b-instruct"
    instructions: str = ""  # system prompt prepended to every conversation
    temperature: float | None = None  # None = use model default
    max_tokens: int | None = None  # None = use model default
    max_iterations: int = 10  # safety cap on the tool-calling loop
    vector_search_index: str | None = None  # Used by dev UI; RAG runtime not yet implemented
    sub_agents: list[str] = []  # URLs (or $ENV_VAR refs) of remote agents to consume as tools
    url: str | None = None  # Public URL of this agent (supports $ENV_VAR); used for registry self-announcement
    registry: str | None = None  # URL of an agent registry to auto-register with on startup (supports $ENV_VAR)
    api_prefix: str = "/api"  # route prefix for tool endpoints
    examples: list[str] = []
    """Starter prompts shown on the dev-UI landing page (``[tool.apx.agent] examples``).

    UI-only metadata: surfaced to the chat landing as clickable starter chips;
    does not affect runtime agent behavior. Distinct from ``example`` (the
    declarative example *backend* config)."""
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    """Built-in guard configuration — see ``[tool.apx.agent.guardrails]``."""
    template: dict[str, Any] | None = None
    """Template-as-config: ``{ name = "data", catalog = "main", schema = "sales" }``.

    When set, ``resolve_agent`` builds the leaf agent from the named template
    via ``template_registry.build(name, spec, ws=ws)`` rather than importing a
    Python module. The ``name`` key selects the template; all other keys become
    the spec dict passed to the template's ``Spec.model_validate``. The
    ``[tool.apx.agent]`` envelope (instructions, model, knobs) is layered on top
    afterward via ``finalize_agent`` as usual — template builds the leaf, persona
    overlays.
    """
    knowledge: str | None = None
    """Path to an OKF bundle directory used to ground this agent.

    ``[tool.apx.agent] knowledge = "./.apx/okf"``. When set, grounding loads
    directly from this bundle (highest-priority baked source) instead of the
    cwd upward-walk. Relative to the project root."""

    memory: MemoryBackendConfig | None = None
    """Declarative memory backend — see ``[tool.apx.agent.memory]``."""

    example: ExampleBackendConfig | None = None
    """Declarative example backend — see ``[tool.apx.agent.example]``."""

    session: SessionBackendConfig | None = None
    """Declarative session backend — see ``[tool.apx.agent.session]``."""

    tools: list[dict[str, Any]] = []
    """Tool declarations from a YAML spec ``tools:`` block.

    Each entry mirrors a ``[[tool.apx.tools]]`` TOML table:
    ``{"type": "genie", "space_id": "abc", "name": "ask_sales"}``.
    ``generate_project`` serializes these into the generated pyproject.toml;
    they are not written under ``[tool.apx.agent]`` and are not read from
    pyproject at runtime (``merge_config_tools`` reads ``[[tool.apx.tools]]``
    directly).
    """

    skills: list[SkillConfig] = []
    """Skill declarations from a YAML spec ``skills:`` block.

    Each entry names a markdown file that the agent can load on demand.
    ``generate_project`` copies the file into the project and emits a
    ``[[tool.apx.tools]]`` entry with ``type = "skill"``.
    """

    executor: str = "langgraph"
    """Harness to use for this agent.

    ``'langgraph'`` (default) compiles the agent to a LangGraph
    ``CompiledStateGraph`` via :func:`~apx_agent._compile.compile_to_langgraph`.
    This supports all agent topologies (SequentialAgent, LoopAgent, etc.).

    ``'claude-sdk'`` uses :class:`~apx_agent._claude_sdk_executor.ClaudeSDKExecutor`
    — a native OpenAI-compatible agentic loop with no LangGraph dependency.
    Suitable for :class:`~apx_agent._agents.LlmAgent` instances with a flat
    tool list.  Agents with composite topologies fall back to ``'langgraph'``
    automatically with a warning.
    """


class AgentTool(BaseModel):
    """A tool derived from a plain Python function or a remote sub-agent."""

    name: str
    description: str
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    sub_agent_url: str | None = None  # set for sub-agent tools, None for local tools


# ---------------------------------------------------------------------------
# ResponsesAgent protocol models (MLflow/Databricks)
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """A single message in the conversation history."""

    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    id: str | None = None
    name: str | None = None
    tool_call_id: str | None = None


# ---------------------------------------------------------------------------
# A2A discovery card models
# ---------------------------------------------------------------------------


class A2ACapabilities(BaseModel):
    a2aVersion: str = "0.3.0"
    streaming: bool = True
    multiTurn: bool = True


class A2AProvider(BaseModel):
    name: str = "Databricks"
    url: str = "https://databricks.com"


class A2AAuthScheme(BaseModel):
    type: str = "bearer"
    name: str = "Databricks OBO token"


class A2ASkill(BaseModel):
    id: str
    name: str
    description: str
    inputSchema: dict[str, Any] | None = None
    outputSchema: dict[str, Any] | None = None


class AgentCard(BaseModel):
    """A2A discovery card served at /.well-known/agent.json."""

    schemaVersion: str = "1.0"
    name: str
    description: str
    url: str = ""  # populated at request time from request.base_url
    protocolVersion: str = "0.3.0"
    capabilities: A2ACapabilities = A2ACapabilities()
    provider: A2AProvider = A2AProvider()
    authSchemes: list[A2AAuthScheme] = [A2AAuthScheme()]
    skills: list[A2ASkill] = []
    mcpEndpoint: str | None = None  # SSE URL for MCP clients; populated at request time


class AgentContext:
    """Provides agent config, tool registry, and root agent to route handlers."""

    def __init__(
        self,
        config: AgentConfig,
        tools: list[AgentTool],
        card: AgentCard,
        agent: "BaseAgent",
        schema: "dict | None" = None,
    ):
        self.config = config
        self.tools = tools
        self.card = card
        self.agent = agent
        self.schema = schema
        self._tool_map: dict[str, AgentTool] = {t.name: t for t in tools}

    def get_tool(self, name: str) -> AgentTool | None:
        return self._tool_map.get(name)


