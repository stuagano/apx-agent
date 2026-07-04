"""agent_tool — wrap any BaseAgent as a callable tool.

Mirrors Google ADK's ``AgentTool`` pattern as a first-class composition
primitive. Lets one ``LlmAgent`` delegate to another agent (local or remote)
based on the LLM's tool-calling decision, rather than via a fixed workflow
position.

Workflow agents (``SequentialAgent``, ``ParallelAgent``, ``LoopAgent``, ...)
compose agents along *deterministic* edges. ``agent_tool`` composes along
*LLM-driven* edges — the parent's LLM picks when and with what input.

Both local in-process and remote agents are supported transparently:

    # Local in-process
    specialist = Agent(tools=[lookup_lineage])
    orchestrator = Agent(tools=[agent_tool(specialist, name="data_inspector",
                                            description="Inspect table lineage")])

    # Remote (via RemoteDatabricksAgent)
    remote_billing = await RemoteDatabricksAgent.from_app_name("billing-agent")
    orchestrator = Agent(tools=[agent_tool(remote_billing, name="billing",
                                            description="Answer billing questions")])

The same wrapper handles both — ``BaseAgent.run`` is the only contract.
"""

from __future__ import annotations

import inspect
import json as _json
import logging
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from ._defaults import Dependencies
from ._models import Message
from ._tool_factory import build_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request

    from ._agents import BaseAgent
    from ._defaults import DatabricksAppsHeaders
    from ._models import AgentCard

logger = logging.getLogger(__name__)

# JSON-schema primitive type → Python annotation for delegate parameters.
# Unknown/missing types degrade to ``str`` — the LLM still sees the named
# field; only the type hint loses precision.
_JSON_TYPE_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _strip_additional_properties(schema: dict[str, Any]) -> None:
    """In-place: drop every ``additionalProperties`` that is not ``False``.

    Applied to each stamped field's emitted JSON schema via
    ``json_schema_extra`` — pydantic renders a bare ``dict`` annotation as
    ``{"type": "object", "additionalProperties": true}``, which FMAPI rejects
    with 400 at the first LLM call (#439). Without this, an object-typed
    remote card property would reintroduce through the compile pipeline the
    exact failure the descriptor sanitization prevents (#442).
    """
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        del schema["additionalProperties"]
    for value in schema.values():
        if isinstance(value, dict):
            _strip_additional_properties(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _strip_additional_properties(item)


def _stamp_structured_signature(fn: Any, input_schema: dict[str, Any]) -> None:
    """Stamp *fn* with a signature derived from a JSON object schema (#442).

    The framework's whole pipeline — ``_inspect_tool_fn`` →
    ``_make_input_model`` → langchain ``StructuredTool`` / FastAPI route /
    OpenAI tool schema — reads ``inspect.signature`` and ``__annotations__``.
    Rewriting those on a ``**kwargs`` delegate makes the remote card's
    properties real, typed, LLM-visible parameters without touching any of
    those consumers. Parameters are keyword-only, so required/optional
    ordering never produces an invalid signature.

    Callers must pass a schema vetted by ``structured_input_schema`` (safe
    identifier property names, sanitized).
    """
    from pydantic import Field

    properties = input_schema["properties"]
    required_value = input_schema.get("required")
    required = set(required_value) if isinstance(required_value, list) else set()

    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for prop, spec in properties.items():
        json_type = spec.get("type") if isinstance(spec, dict) else None
        py_type: Any = _JSON_TYPE_TO_PY.get(json_type, str) if isinstance(json_type, str) else str
        description = spec.get("description") if isinstance(spec, dict) else None
        field_kwargs: dict[str, Any] = {
            # See _strip_additional_properties: FMAPI-invariant guard (#439).
            "json_schema_extra": _strip_additional_properties,
        }
        if isinstance(description, str) and description:
            field_kwargs["description"] = description
        if prop not in required:
            py_type = py_type | None
            field_kwargs["default"] = None
        annotations[prop] = py_type
        parameters.append(
            inspect.Parameter(
                prop,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=py_type,
                # A FieldInfo default flows through _inspect_tool_fn into
                # create_model, which reads required-ness and metadata off it.
                default=Field(**field_kwargs),
            )
        )
    parameters.append(
        inspect.Parameter(
            "headers", inspect.Parameter.KEYWORD_ONLY, annotation=Dependencies.Headers
        )
    )
    annotations["headers"] = Dependencies.Headers
    annotations["return"] = str

    fn.__signature__ = inspect.Signature(parameters)
    fn.__annotations__ = annotations


def _snake_case(name: str) -> str:
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _infer_name(agent: "BaseAgent") -> str:
    explicit = getattr(agent, "_name", None)
    if explicit:
        return _snake_case(explicit)
    return _snake_case(type(agent).__name__)


def agent_tool(
    agent: "BaseAgent",
    *,
    name: str | None = None,
    description: str | None = None,
):
    """Wrap an agent as a tool callable from another ``LlmAgent``.

    The returned object is a typed Python function suitable for the
    ``tools=[...]`` parameter of ``LlmAgent``. When the parent LLM picks
    this tool, the framework invokes ``agent.run(...)`` in-process (or over
    HTTP for ``RemoteDatabricksAgent``) and returns the result.

    Args:
        agent: Any ``BaseAgent``. Local (``LlmAgent``, ``SequentialAgent``,
            ``ParallelAgent``, ``LoopAgent``, ``RouterAgent``, ``HandoffAgent``)
            or remote (``RemoteDatabricksAgent``) — the wrapper doesn't care.
        name: Tool name the parent LLM sees. Defaults to ``agent._name`` or
            snake-cased class name. Required to be descriptive — the parent
            LLM picks tools by name and description.
        description: Tool description the parent LLM sees. Strongly recommend
            providing this explicitly; the default is a generic delegate
            message which won't help the LLM decide when to call this tool.

    Returns:
        A typed function with LLM-visible parameter ``message: str``.
    """
    tool_name = name or _infer_name(agent)
    tool_desc = description or (
        f"Delegate the user's request to the {tool_name} agent. "
        "Pass the relevant question or instruction as ``message``."
    )

    async def _wrapped(message: str, request: Dependencies.Request) -> str:
        result = await agent.run(
            [Message(role="user", content=message)],
            request,
        )
        return result

    return build_tool(_wrapped, name=tool_name, description=tool_desc)


def remote_agent_tool(
    url: str,
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
    on_card: "Callable[[AgentCard], None] | None" = None,
):
    """Wrap a remote sub-agent URL as a callable tool (compile-safe).

    The compile-path twin of ``agent_tool(RemoteDatabricksAgent(...))``: the
    wrapper declares ``Dependencies.Headers`` — which ``compile_to_langgraph``
    resolves from the per-request compile context — instead of
    ``Dependencies.Request``, which has no compile-time resolver. This is what
    makes a config-declared ``sub_agents`` URL actually callable from inside a
    compiled LangGraph (#436). The current user's OBO token/host, when
    present, are forwarded to the sub-agent.

    ``input_schema`` — a structured object schema (as vetted by
    ``structured_input_schema``, i.e. the peer card's real ``inputSchema``) —
    makes the delegate expose those properties as typed parameters instead of
    one free-text ``message`` (#442). The LLM's structured arguments are
    transmitted as a JSON object in the user message content: the wire
    payloads (Responses/ChatAgent) are unchanged, the remote agent's LLM sees
    named fields instead of prose. ``None`` keeps the ``message: str``
    wrapper.

    A failure to reach or invoke the sub-agent returns a clear error string
    (``"sub-agent at <url> unreachable: <err>"``) instead of raising, so one
    dead sub-agent degrades that tool call rather than killing the whole turn.

    ``on_card`` fires after any invocation once the peer's agent card is
    known. ``RemoteDatabricksAgent.run`` fetches the card on every call until
    one succeeds (``init`` is idempotent), so a peer that boots *after* the
    parent's startup is observed on the first tool use — with zero extra
    requests. ``LlmAgent`` uses this to repair a degraded registration whose
    startup card fetch failed (#440).
    """
    base_url = url.rstrip("/")

    from ._remote import RemoteDatabricksAgent  # noqa: PLC0415 — avoid import cycle

    remote = RemoteDatabricksAgent(f"{base_url}/.well-known/agent.json")

    async def _send(content: str, headers: "DatabricksAppsHeaders | None") -> str:
        forwarded: dict[str, str] = {}
        # The compile path resolves Headers to None outside Databricks Apps
        # (local dev / no user identity); forward OBO material only when real.
        if headers is not None:
            if headers.token is not None:
                token = headers.token.get_secret_value()
                forwarded["X-Forwarded-Access-Token"] = token
                # The Apps OAuth ingress authenticates on Authorization; with
                # only X-Forwarded-Access-Token it 302s to login (live-proven
                # app-to-app on *.databricksapps.com).
                forwarded["Authorization"] = f"Bearer {token}"
            if headers.host is not None:
                forwarded["X-Forwarded-Host"] = headers.host
        # RemoteDatabricksAgent only reads ``request.headers`` (see
        # ``_obo_headers``), so a headers-only shim satisfies its contract
        # without a served FastAPI Request in scope.
        shim = cast("Request", SimpleNamespace(headers=forwarded))
        try:
            return await remote.run([Message(role="user", content=content)], shim)
        except Exception as exc:
            logger.warning("sub-agent call to %s failed: %s", base_url, exc)
            return f"sub-agent at {base_url} unreachable: {exc}"
        finally:
            # run() fetched the card (init) even when the invocation itself
            # failed — report it so a degraded registration repairs (#440).
            if on_card is not None and remote.card is not None:
                on_card(remote.card)

    if input_schema is None:
        async def _delegate(message: str, headers: Dependencies.Headers) -> str:
            return await _send(message, headers)

        return build_tool(_delegate, name=name, description=description)

    async def _structured_delegate(headers: Dependencies.Headers, **kwargs: Any) -> str:
        # Optional parameters the LLM left unset arrive as None defaults —
        # drop them so the peer sees only the fields that were provided.
        args = {k: v for k, v in kwargs.items() if v is not None}
        return await _send(_json.dumps(args, ensure_ascii=False), headers)

    _stamp_structured_signature(_structured_delegate, input_schema)
    return build_tool(_structured_delegate, name=name, description=description)
