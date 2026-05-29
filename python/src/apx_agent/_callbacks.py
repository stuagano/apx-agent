"""LangChain CallbackHandler bridge — fires apx-agent hooks during compile-path execution.

When an ``LlmAgent`` defines any of ``before_tool``, ``after_tool``,
``before_model``, ``after_model``, the compile path attaches a
``_AgentCallbackHandler`` to the LangGraph chain via
``with_config(callbacks=[handler])``. LangChain's runtime invokes the
handler at the appropriate lifecycle events, and the handler invokes the
user-supplied hook.

Sync and async hooks are both supported. When a hook is a coroutine
function, the handler runs it to completion (via ``asyncio.run`` from sync
code, or on a dedicated worker-thread loop when already inside a running
event loop) so its exception propagates synchronously. Exceptions in hooks
propagate — that's the contract: ``before_*`` raises abort the call,
``after_*`` raises don't undo the side effect but do propagate.

Requires ``langchain-core`` (already pulled in by the ``langgraph`` extra).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from ._audit import (
    hash_for_audit,
    input_keys_summary,
    output_summary,
    set_audit_attrs,
)
from ._mlflow_tracing import current_active_span

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult

try:
    from langchain_core.callbacks import BaseCallbackHandler
    _LC_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only without langchain
    _LC_AVAILABLE = False

    class BaseCallbackHandler:  # type: ignore[no-redef]
        """Stub so the rest of the module imports even without langchain."""
        pass


logger = logging.getLogger(__name__)


def _run_hook(hook: Any, *args: Any) -> None:
    """Invoke a sync or async hook with ``args``, running it to completion.

    LangChain's ``on_tool_start``/``on_chat_model_start`` callbacks (where
    ``before_*`` hooks fire) are *synchronous*, so the only way a hook's
    exception can abort the call — the documented contract: "``before_*``
    raises abort the call" — is to run the hook to completion before
    returning. A coroutine scheduled fire-and-forget on the running loop
    would let the call proceed and surface its exception too late to abort.

    For async hooks we therefore always run the coroutine to completion:

      * No running loop → ``asyncio.run``.
      * Running loop (the normal uvicorn case) → run the coroutine in a
        dedicated worker thread with its own event loop and block on the
        result. This mirrors the sync-bridge offload in
        ``_compile._make_langchain_tool``.

    Either way the hook's exception propagates synchronously to the caller,
    so the LangChain runtime surfaces it as a chain failure and the call is
    aborted.
    """
    if hook is None:
        return
    if inspect.iscoroutinefunction(hook):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # Already inside a running loop — can't call asyncio.run here.
            # Run the coroutine to completion on a worker thread so its
            # exception propagates synchronously and can abort the call.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(lambda: asyncio.run(hook(*args))).result()
        else:
            asyncio.run(hook(*args))
    else:
        hook(*args)


class _AgentCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler bridging to apx-agent's LlmAgent hooks.

    Args:
        before_tool: Optional ``hook(tool_name, arguments)`` called before
            each tool invocation.
        after_tool: Optional ``hook(tool_name, arguments, output)`` called
            after each tool invocation.
        before_model: Optional ``hook(prompts)`` called before each LLM
            invocation.
        after_model: Optional ``hook(response)`` called after each LLM
            invocation.

    The handler is constructed per ``predict()`` call so per-request state
    (the active LlmAgent's hook closures, OBO context) is captured
    correctly. The hook callables themselves can be sync or async.
    """

    def __init__(
        self,
        *,
        before_tool: Any | None = None,
        after_tool: Any | None = None,
        before_model: Any | None = None,
        after_model: Any | None = None,
    ) -> None:
        super().__init__()
        self._before_tool = before_tool
        self._after_tool = after_tool
        self._before_model = before_model
        self._after_model = after_model
        # Track input arguments per run_id so on_tool_end can pass them to
        # after_tool. LangChain provides run_id in **kwargs of both events.
        self._tool_inputs: dict[Any, tuple[str, dict[str, Any]]] = {}
        # Track tool-call start times per run_id for duration_ms audit attr.
        self._tool_starts: dict[Any, float] = {}

    # -- LLM lifecycle -----------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        if self._before_model is None:
            return
        try:
            _run_hook(self._before_model, prompts)
        except Exception:
            logger.exception("before_model hook raised")
            raise

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        # Chat models route through a separate event in LangChain. We treat
        # it identically to on_llm_start; the hook receives the messages
        # directly rather than serialized prompts.
        set_audit_attrs(
            current_active_span(),
            operation="model_call",
            model_input_messages=sum(len(batch) for batch in messages if isinstance(batch, list)),
        )
        if self._before_model is None:
            return
        try:
            _run_hook(self._before_model, messages)
        except Exception:
            logger.exception("before_model hook raised")
            raise

    def on_llm_end(self, response: "LLMResult", **kwargs: Any) -> None:
        # Record token counts on the active span when LangChain attaches
        # llm_output usage info to the response.
        usage = _extract_llm_usage(response)
        if usage:
            set_audit_attrs(
                current_active_span(),
                model_input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
                model_output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
            )
        if self._after_model is None:
            return
        try:
            _run_hook(self._after_model, response)
        except Exception:
            logger.exception("after_model hook raised")
            raise

    # -- Tool lifecycle ----------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        tool_name = serialized.get("name", "") if isinstance(serialized, dict) else ""
        # LangChain passes the tool input as a string for on_tool_start.
        # The inputs dict (when available) lives in kwargs.
        inputs = kwargs.get("inputs", {}) or {"input": input_str}
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._tool_inputs[run_id] = (tool_name, inputs)
            self._tool_starts[run_id] = time.time()
        set_audit_attrs(
            current_active_span(),
            operation="tool_call",
            tool_name=tool_name,
            tool_input_keys=input_keys_summary(inputs),
            tool_input_hash=hash_for_audit(inputs),
        )
        if self._before_tool is None:
            return
        try:
            _run_hook(self._before_tool, tool_name, inputs)
        except Exception:
            logger.exception("before_tool hook raised for %s", tool_name)
            raise

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        tool_name = ""
        inputs: dict[str, Any] = {}
        start_time: float | None = None
        if run_id is not None:
            if run_id in self._tool_inputs:
                tool_name, inputs = self._tool_inputs.pop(run_id)
            start_time = self._tool_starts.pop(run_id, None)

        # Record output shape + duration on the active span.
        out_type, out_size = output_summary(output)
        attrs: dict[str, Any] = {
            "tool_output_type": out_type,
            "tool_output_size": out_size,
        }
        if start_time is not None:
            attrs["tool_duration_ms"] = int((time.time() - start_time) * 1000)
        set_audit_attrs(current_active_span(), **attrs)

        if self._after_tool is None:
            return
        try:
            _run_hook(self._after_tool, tool_name, inputs, output)
        except Exception:
            logger.exception("after_tool hook raised for %s", tool_name)
            raise

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        # Clean up the tracked input on tool failure so we don't leak state.
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._tool_inputs.pop(run_id, None)
            self._tool_starts.pop(run_id, None)


def _extract_llm_usage(response: Any) -> dict[str, Any] | None:
    """Pull token usage out of a LangChain LLMResult.

    LangChain attaches usage under either ``llm_output["token_usage"]`` or
    ``llm_output["usage"]`` depending on the provider. Returns ``None`` when
    no usage info is available.
    """
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        for key in ("token_usage", "usage"):
            usage = llm_output.get(key)
            if isinstance(usage, dict):
                return usage
    return None


def build_callback_handler(agent: Any) -> _AgentCallbackHandler | None:
    """Return a callback handler for ``agent`` if any hook is set; else None.

    Reads the four hook fields stored on an ``LlmAgent`` and constructs a
    handler that bridges LangChain lifecycle events to them. Returns
    ``None`` when no hooks are set so the compile path can skip attaching
    a handler entirely (avoids any overhead from passing through an
    inactive handler).
    """
    if not _LC_AVAILABLE:
        return None
    before_tool = getattr(agent, "_before_tool", None)
    after_tool = getattr(agent, "_after_tool", None)
    before_model = getattr(agent, "_before_model", None)
    after_model = getattr(agent, "_after_model", None)
    if not any([before_tool, after_tool, before_model, after_model]):
        return None
    return _AgentCallbackHandler(
        before_tool=before_tool,
        after_tool=after_tool,
        before_model=before_model,
        after_model=after_model,
    )
