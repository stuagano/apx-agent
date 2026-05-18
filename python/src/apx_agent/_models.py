"""Agent protocol models, type aliases, and context objects."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

from fastapi import Request
from pydantic import BaseModel

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


