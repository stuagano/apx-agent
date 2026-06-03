"""Agent protocol models, type aliases, and context objects."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias

from fastapi import Request
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


StoreType = Literal["inmemory", "delta", "lakebase"]


class MemoryBackendConfig(BaseModel):
    """Declarative memory backend — maps to ``[tool.apx.agent.memory]``."""

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"
    embedding_model: str | None = None
    embedding_dim: int | None = None
    table_name: str | None = None
    index_name: str | None = None
    auto_create: bool = True
    instance_name: str | None = None
    database: str | None = None
    host: str | None = None
    ensure_extension: bool = True
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
    instance_name: str | None = None
    database: str | None = None
    host: str | None = None
    ensure_extension: bool = True
    agent_id: str | None = None
    """Partition key for example rows — defaults to ``config.name`` at attach time."""
    tool_prefix: str = ""
    include: list[str] | None = None
    validate_at_boot: bool = True


class SessionBackendConfig(BaseModel):
    """Declarative session backend — maps to ``[tool.apx.agent.session]``.

    DeltaSessionStore takes ``table_path`` (not ``table_name``); the wiring maps
    ``table_name`` → ``table_path`` when building a delta session store.
    """

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"
    table_name: str | None = None
    auto_create: bool = True
    instance_name: str | None = None
    database: str | None = None
    host: str | None = None
    warehouse_id: str | None = None
    validate_at_boot: bool = True


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
    memory: MemoryBackendConfig | None = None
    """Declarative memory backend — see ``[tool.apx.agent.memory]``."""

    example: ExampleBackendConfig | None = None
    """Declarative example backend — see ``[tool.apx.agent.example]``."""

    session: SessionBackendConfig | None = None
    """Declarative session backend — see ``[tool.apx.agent.session]``."""


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
    ):
        self.config = config
        self.tools = tools
        self.card = card
        self.agent = agent
        self._tool_map: dict[str, AgentTool] = {t.name: t for t in tools}

    def get_tool(self, name: str) -> AgentTool | None:
        return self._tool_map.get(name)


