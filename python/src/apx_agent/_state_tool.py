"""Stateful tool adapter — builds StructuredTools that receive LangGraph state.

This module intentionally does NOT use ``from __future__ import annotations``.
Keeping annotations evaluated at definition time means ``InjectedState`` and
``InjectedToolCallId`` are real type objects when the inner wrappers are
created, so:

  * ``ToolNode._get_all_injected_args`` can resolve them via ``get_type_hints``
    without any ``__globals__`` patching.
  * ``StructuredTool._injected_args_keys`` reads ``inspect.signature`` directly
    and sees live annotations rather than lazy strings — no ``__annotations__``
    rewrite needed.

Both hacks previously required in ``_compile.py`` are eliminated here by
keeping this module annotation-eager.
"""

import asyncio
import concurrent.futures
import json
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from ._state_proxy import StateProxy


def _tool_message_text(value: Any) -> str:
    """Render a tool return for a ToolMessage body, matching ToolNode's default
    coercion: strings verbatim, everything else as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _make_stateful_langchain_tool(
    fn: Any,
    state_param: str,
    resolved_deps: dict[str, Any],
    input_model: Any,
    is_async: bool,
) -> Any:
    """Build a StructuredTool for a tool that declares ``Dependencies.State``.

    The wrapper takes LangGraph-injected ``state`` and ``tool_call_id`` params
    (hidden from the LLM), binds a StateProxy to the author's state param, and
    turns tracked writes into a Command state delta after the fn returns.

    For async tools, BOTH ``coroutine=`` and ``func=`` are registered so the
    tool works on langgraph's sync invocation path (Apps ``/invocations`` and
    the ChatAgent runtime call tools synchronously). The sync bridge delegates
    entirely to the async wrapper — no logic duplication.
    """

    def _finish(proxy: StateProxy, tool_call_id: str, ret: Any) -> Any:
        if not proxy.dirty:
            return ret
        return Command(
            update={
                "state": proxy.delta,
                "messages": [
                    ToolMessage(_tool_message_text(ret), tool_call_id=tool_call_id)
                ],
            }
        )

    if is_async:
        async def _async_wrapper(
            __apx_state: Annotated[dict, InjectedState("state")],
            tool_call_id: Annotated[str, InjectedToolCallId],
            **kwargs: Any,
        ) -> Any:
            proxy = StateProxy(__apx_state)
            ret = await fn(**kwargs, **resolved_deps, **{state_param: proxy})
            return _finish(proxy, tool_call_id, ret)

        def _sync_bridge(
            __apx_state: Annotated[dict, InjectedState("state")],
            tool_call_id: Annotated[str, InjectedToolCallId],
            **kwargs: Any,
        ) -> Any:
            # langgraph's sync graph.invoke() path calls tools synchronously.
            # Bridge the async wrapper here so async stateful tools work on the
            # Apps /invocations and ChatAgent sync paths too.
            async def _call() -> Any:
                return await _async_wrapper(
                    __apx_state,
                    tool_call_id,
                    **kwargs,
                )

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(_call())
            # Already inside a running loop — run the coroutine in a worker thread.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(lambda: asyncio.run(_call())).result()

        _async_wrapper.__name__ = _sync_bridge.__name__ = fn.__name__
        _async_wrapper.__doc__ = _sync_bridge.__doc__ = fn.__doc__
        return StructuredTool.from_function(
            func=_sync_bridge,
            coroutine=_async_wrapper,
            name=fn.__name__,
            description=(fn.__doc__ or fn.__name__).strip(),
            args_schema=input_model,
        )

    def _sync_wrapper(
        __apx_state: Annotated[dict, InjectedState("state")],
        tool_call_id: Annotated[str, InjectedToolCallId],
        **kwargs: Any,
    ) -> Any:
        proxy = StateProxy(__apx_state)
        ret = fn(**kwargs, **resolved_deps, **{state_param: proxy})
        return _finish(proxy, tool_call_id, ret)

    _sync_wrapper.__name__ = fn.__name__
    _sync_wrapper.__doc__ = fn.__doc__
    return StructuredTool.from_function(
        func=_sync_wrapper,
        name=fn.__name__,
        description=(fn.__doc__ or fn.__name__).strip(),
        args_schema=input_model,
    )
