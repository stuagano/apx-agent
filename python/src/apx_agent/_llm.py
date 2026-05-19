"""Provider compatibility layer for Databricks Model Serving.

Why this module exists
----------------------
``ChatDatabricks`` exposes a single LangChain interface over many providers
(Anthropic, OpenAI, Google, Meta, ...) routed through Databricks Model Serving.
The base client assumes the OpenAI chat-completions request shape — but each
provider accepts a different *subset* of that shape, and the working subsets
drift over time as providers ship new model generations and the
``databricks-langchain`` adapter releases new versions.

Rather than scatter provider-specific branches through user code (tool
implementations, eval scripts, jobs), this module owns the normalization
and exposes a ``get_llm(endpoint)`` factory so callers stay
provider-agnostic. The framework's compile path uses the same factory, so
agents authored in apx-agent get the protection automatically. Tools that
need to make their own internal LLM calls (synthesis steps, classifiers,
embedded judges) get the same protection by going through the factory.

Cross-language note: the TypeScript framework solves the same problem
structurally — ``typescript/src/agent/runner.ts`` builds a minimal request
body (``{ model, messages, [tools, tool_choice, stream] }``) that never
includes provider-rejectable fields like ``temperature`` or ``top_p``. This
Python module is the equivalent guarantee for the LangChain runtime.

Verified provider behavior
--------------------------
Probed against the ``fe-shared-builder`` workspace on 2026-05-18 with a
5-token ``"hi"`` prompt via ``databricks-langchain==0.19.0``:

| Provider family               | Plain ChatDatabricks (no kwargs) | With temperature=0.5 |
|-------------------------------|----------------------------------|----------------------|
| Anthropic Claude (claude-*)   | OK                               | OK |
| Meta Llama (meta-llama-*)     | OK                               | OK |
| Google Gemini (gemini-*)      | OK                               | OK |
| OpenAI GPT-5 (gpt-5*)         | OK (no temp sent)                | **FAIL** — only default temp accepted |
| OpenAI GPT-5.5-Pro            | FAIL — Responses API only        | (use ``use_responses_api=True``) |
| GPT-OSS (gpt-oss-*)           | Unprobed                         | Unknown |

A historical Claude-rejects-temperature workaround was *deleted* on the basis
of these probes — preserving dead workarounds is itself a footgun.

Why the subclass still exists
-----------------------------
``databricks-langchain >= 0.19`` only sends ``temperature`` when non-None, so
plain ``ChatDatabricks`` works for GPT-5 today *as long as no caller passes
temperature*. The subclass strips ``temperature`` and ``top_p`` defensively
so the rule "GPT-5 endpoints don't accept tuning knobs" is enforced
structurally — not by every caller remembering. This is defense-in-depth,
not a corrective fix, and that's worth keeping precisely because the
package's default behavior could change again.

Extending this module
---------------------
Add a new subclass + a routing rule in ``get_llm`` when:
  * a provider rejects a field the base client always sends, OR
  * a provider requires a field the base client omits, OR
  * two providers disagree on the schema/units of the same field.

Do NOT add provider-routing logic to callers — fix it once, here.
Do NOT add subclasses for hypothetical quirks — probe first, then implement.
When a probe contradicts the historical reason for a workaround, *delete the
workaround*; don't preserve it "just in case."
"""

from __future__ import annotations

from typing import Any


class ChatDatabricksGptReasoning:
    """For GPT-5 family endpoints, which only accept the default temperature.

    Verified against ``databricks-gpt-5-mini`` on 2026-05-18: the endpoint
    returns 400 for any ``temperature`` other than the default (1) and for
    any ``top_p`` value. ``databricks-langchain >= 0.19`` omits these by
    default, but this subclass strips them defensively in case a caller
    passes them via ``extra_params`` or kwargs.

    Accepted by these endpoints (no strip needed): ``frequency_penalty``,
    ``presence_penalty``, ``max_tokens``.

    Constructed lazily — the class body is materialized at first use so
    ``databricks-langchain`` doesn't have to be installed unless someone
    actually calls ``get_llm``.
    """

    # The real class is built lazily; this stub exists only so callers can
    # ``isinstance()``-check or import the name without triggering the
    # databricks-langchain import. See ``_make_gpt_reasoning_class``.
    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        real_cls = _make_gpt_reasoning_class()
        return real_cls(*args, **kwargs)


def _make_gpt_reasoning_class() -> type:
    """Build the real ``ChatDatabricksGptReasoning`` subclass on first use."""
    from databricks_langchain import ChatDatabricks

    class _ChatDatabricksGptReasoning(ChatDatabricks):
        def _prepare_inputs(  # type: ignore[override]
            self,
            messages: Any,
            stop: Any = None,
            stream: bool = False,
            *,
            custom_inputs: Any = None,
            **kwargs: Any,
        ) -> Any:
            data = super()._prepare_inputs(
                messages, stop, stream, custom_inputs=custom_inputs, **kwargs
            )
            data.pop("temperature", None)
            data.pop("top_p", None)
            return data

    _ChatDatabricksGptReasoning.__name__ = "ChatDatabricksGptReasoning"
    _ChatDatabricksGptReasoning.__qualname__ = "ChatDatabricksGptReasoning"
    return _ChatDatabricksGptReasoning


# Endpoint-name prefixes that require the reasoning-model request shape.
GPT_REASONING_PREFIXES: tuple[str, ...] = ("databricks-gpt-5",)


def get_llm(endpoint: str, **kwargs: Any) -> Any:
    """Return the right ChatDatabricks variant for a given serving endpoint.

    Routing is by endpoint-name prefix, not by config — the caller passes the
    exact endpoint they want to hit, and this function picks the wrapper that
    knows that endpoint's quirks.

    Verified rules (2026-05-18, ``databricks-langchain==0.19.0``):
      * ``databricks-gpt-5*``         → ``ChatDatabricksGptReasoning``
      * everything else (Claude/Llama/Gemini) → plain ``ChatDatabricks``

    Not currently handled (extend here when a caller needs it):
      * ``databricks-gpt-5-5-pro``: chat-completions unsupported; the package
        supports it via ``use_responses_api=True`` but no caller routes there
        yet. Add a third subclass / routing rule when one does.

    Example::

        from apx_agent import get_llm
        from langchain_core.messages import HumanMessage

        llm = get_llm("databricks-claude-sonnet-4-6")
        result = llm.invoke([HumanMessage(content="Summarize this report...")])
    """
    if any(endpoint.startswith(p) for p in GPT_REASONING_PREFIXES):
        return _make_gpt_reasoning_class()(model=endpoint, **kwargs)
    from databricks_langchain import ChatDatabricks
    return ChatDatabricks(model=endpoint, **kwargs)
