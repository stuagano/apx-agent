"""knowledge_assistant_tool — query an Agent Bricks Knowledge Assistant as a tool.

An Agent Bricks Knowledge Assistant (KA) is grounded on a document corpus (e.g.
SEC 10-K filings) and exposes a standard Model Serving endpoint. This factory
wraps that endpoint as an apx-agent tool so an agent can ask it a question
mid-conversation and get back a *grounded* answer with citations — the same
shape ``foundation_model_tool`` uses to wrap a plain model, but the returned
dict carries the KA's ``citations`` so downstream steps can attribute claims to
source documents.

Auth runs as the calling user via the standard OBO path (``ws:
UserClientDependency``), so the KA's per-user access policies apply and the
endpoint shows up in the declared resources list, letting the platform mint a
scoped token with governance / cost flowing through Mosaic AI Gateway.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time
and ``get_type_hints()`` in ``_inspection.py`` sees the real ``Annotated[...]``
type, not a string.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Common field names a KA citation record may use for its source document URI.
# The exact shape is confirmed against the live endpoint during the integration
# gate (AC-5); this list is the single place to correct if it differs.
_DOC_URI_KEYS = ("doc_uri", "source_uri", "source", "url", "uri")


def _normalize_citation(raw: Any) -> dict[str, Any]:
    """Coerce one KA citation record into a dict carrying a ``doc_uri``.

    Accepts either a dict or an SDK object; pulls the source URI from whichever
    of ``_DOC_URI_KEYS`` is present so the contract (``citations[].doc_uri``)
    holds regardless of the KA's exact field name.
    """
    fields = raw if isinstance(raw, dict) else getattr(raw, "__dict__", {})
    out = dict(fields)
    if "doc_uri" not in out:
        for key in _DOC_URI_KEYS:
            val = out.get(key) if isinstance(out.get(key), str) else getattr(raw, key, None)
            if val:
                out["doc_uri"] = val
                break
    return out


def _extract_citations(response: Any, message: Any) -> list[dict[str, Any]]:
    """Pull citation records off the KA response, defensively.

    KA endpoints have carried citations either at the response top level, on the
    answer message, or (Responses API) as ``annotations`` inside the message's
    output_text parts. Check all three, accepting dicts or SDK objects, and
    normalize each to a ``doc_uri`` dict.
    """
    def _get(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    raw = _get(response, "citations") or _get(message, "citations") or []
    if not raw and isinstance(message, dict):
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("annotations"):
                raw = part["annotations"]
                break
    return [_normalize_citation(c) for c in raw]


def _extract_answer(raw: Any) -> "tuple[str, Any]":
    """Return ``(answer_text, message)`` from a KA invocation payload.

    Handles the Responses API shape first (``output[].content[].output_text`` —
    what current Agent Bricks KAs return) and falls back to the chat-completions
    shape (``choices[0].message.content``) for older endpoints.
    """
    if not isinstance(raw, dict):
        as_dict = getattr(raw, "as_dict", None)
        raw = as_dict() if callable(as_dict) else getattr(raw, "__dict__", {})
    for item in raw.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            texts = [
                c.get("text", "")
                for c in item.get("content") or []
                if isinstance(c, dict) and c.get("type") == "output_text"
            ]
            if texts:
                return "".join(texts), item
    choices = raw.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        return (message.get("content") or ""), message
    return "", None


def knowledge_assistant_tool(
    endpoint_name: str,
    *,
    name: str = "ask_knowledge_assistant",
    description: str | None = None,
) -> Any:
    """Return a tool that asks an Agent Bricks Knowledge Assistant a question.

    The LLM sees one parameter — ``question: str`` — and the endpoint is fixed
    at factory time. The tool returns a grounded-result dict::

        {"question": ..., "answer": ..., "citations": [{"doc_uri": ...}, ...]}

    On a KA / serving failure it degrades to ``{"question": ..., "error": ...}``
    (matching ``genie_query_tool``) rather than raising a pipeline-fatal 500.

    Usage — ground the first stage of a sequential flow on 10-K filings::

        import os
        from apx_agent import Agent, SequentialAgent, knowledge_assistant_tool

        research = Agent(
            instructions="Answer using the knowledge assistant; cite sources.",
            tools=[knowledge_assistant_tool(os.environ["APX_KA_ENDPOINT_NAME"])],
        )
        flow = SequentialAgent([research, Agent(instructions="Summarize.")])

    Args:
        endpoint_name: Databricks Model Serving endpoint name of the KA. Supply
            it from config/env (e.g. ``APX_KA_ENDPOINT_NAME``) — never hardcode.
        name: Tool name shown to the calling LLM. Defaults to
            ``"ask_knowledge_assistant"``.
        description: Tool description shown to the calling LLM. When omitted,
            generated from the endpoint name.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    _desc = description or (
        f"Ask the `{endpoint_name}` knowledge assistant a question. Returns a "
        f"grounded answer with citations to the source documents it used."
    )

    async def _ask_knowledge_assistant(
        question: str,
        ws: UserClientDependency,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Placeholder doc — overwritten below."""
        # Agent Bricks KAs are ``agent/v1/responses`` endpoints: they require the
        # ``input`` field (reject ``messages``), and the SDK's typed
        # ``serving_endpoints.query`` drops the ``output`` array for them — so we
        # POST to ``/invocations`` directly and parse the raw payload. Runs under
        # the OBO user client (``ws``) so per-user KA access policies apply.
        try:
            response = ws.api_client.do(
                "POST",
                f"/serving-endpoints/{endpoint_name}/invocations",
                body={"input": [{"role": "user", "content": question}]},
            )
        except Exception as exc:
            logger.warning("Knowledge assistant query failed on %s: %s", endpoint_name, exc)
            return {"question": question, "error": f"Knowledge assistant query failed: {exc}"}

        answer, message = _extract_answer(response)
        return {
            "question": question,
            "answer": answer,
            "citations": _extract_citations(response, message),
        }

    return build_tool(
        _ask_knowledge_assistant,
        name=name,
        description=_desc,
        resources=[ResourceSpec("serving_endpoint", endpoint_name)],
    )
