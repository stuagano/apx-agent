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
"""Called before each tool dispatch: ``hook(tool_name, arguments)``."""
AfterToolHook: TypeAlias = Callable[[str, dict[str, Any], Any], Any]
"""Called after each tool dispatch: ``hook(tool_name, arguments, result)``."""

# Guardrail callables — return None to pass, or a string to short-circuit
InputGuardrailFn: TypeAlias = Callable[[list["Message"]], "str | None"]
"""Called with the incoming messages before the LLM sees them.
Return ``None`` to let the request through, or a non-empty string to reject it
(the string is returned as the agent's response)."""
OutputGuardrailFn: TypeAlias = Callable[[str], "str | None"]
"""Called with the agent's final text response.
Return ``None`` to pass through, or a non-empty string to replace the output."""

# Module-level Agent instance, set when user calls Agent(tools=[...])
_agent_instance: "BaseAgent | None" = None


def _get_agent_instance() -> "BaseAgent | None":
    return _agent_instance


def _set_agent_instance(agent: "BaseAgent | None") -> None:
    global _agent_instance
    _agent_instance = agent


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


class InvocationRequest(BaseModel):
    """MLflow ResponsesAgent /invocations request format.

    ``input`` accepts either a list of message dicts or a plain string.
    A plain string is coerced to ``[{"role": "user", "content": <str>}]``.

    ``custom_inputs`` supports the following recognised keys:

    * ``"instructions"`` — per-request system prompt override; takes
      precedence over the agent's own ``instructions`` setting.
    """

    input: list[Message] | str
    custom_inputs: dict[str, Any] = {}
    stream: bool = False

    def messages(self) -> list[Message]:
        """Return input normalised to a list of Messages."""
        if isinstance(self.input, str):
            return [Message(role="user", content=self.input)]
        return self.input

    def instructions_override(self) -> str:
        """Return a per-request instructions override from custom_inputs, or ''."""
        return str(self.custom_inputs.get("instructions", ""))


class OutputTextContent(BaseModel):
    type: str = "output_text"
    text: str


class OutputItem(BaseModel):
    type: str = "message"
    role: str = "assistant"
    id: str | None = None
    status: str = "completed"
    content: list[OutputTextContent]


class InvocationResponse(BaseModel):
    """MLflow ResponsesAgent /invocations response format."""

    output: list[OutputItem]
    custom_outputs: dict[str, Any] = {}


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


def set_custom_output(request: Request, key: str, value: Any) -> None:
    """Set a value in ``InvocationResponse.custom_outputs`` for the current invocation.

    Call from within a tool function to surface structured data alongside the
    agent's text response::

        def search(query: str, request: Request) -> str:
            results = do_search(query)
            set_custom_output(request, "sources", [r.url for r in results])
            return results[0].snippet

    Multiple tools can set different keys; all are merged into ``custom_outputs``.
    """
    if not hasattr(request.state, "custom_outputs"):
        request.state.custom_outputs = {}
    request.state.custom_outputs[key] = value
