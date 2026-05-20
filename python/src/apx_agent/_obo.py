"""Unified OBO (on-behalf-of) header extraction.

The apx-agent SDK supports two Databricks runtime targets:

  * **Model Serving** (``compile_to_chat_agent``): the OBO token is plumbed
    through ``custom_inputs={"user_token": ..., "workspace_host": ...}``. A
    FastAPI front door extracts the ``X-Forwarded-Access-Token`` header and
    forwards it as a ``custom_inputs`` field so the deployed pyfunc has access
    to it through the request payload.

  * **Databricks Apps** (``compile_to_responses_agent``): the Apps proxy
    auto-injects ``X-Forwarded-Access-Token``, ``X-Forwarded-User``, and
    ``X-Forwarded-Email`` per request. The agent reads them directly from the
    incoming HTTP headers via ``mlflow.genai.agent_server.get_request_headers``.

Both runtimes must end up at the same place — a per-request user-scoped
``WorkspaceClient`` whose tools see the calling user's grants. The single
source of truth is :func:`extract_obo_headers`, which normalizes BOTH input
shapes into a uniform dict::

    {"user_token": "...", "workspace_host": "...", "user_id": "...",
     "user_email": "..."}

Any field may be missing. Downstream consumers can plug ``user_token`` and
``workspace_host`` straight into ``WorkspaceClient(token=..., host=...)``.

Precedence rule (matters when both conventions are present in one request):

  1. ``custom_inputs.user_token`` — caller-supplied wins. This preserves the
     existing semantics from ``_invocations.py`` and the eval bridge, where a
     batch caller may want to override the route-injected token.
  2. ``X-Forwarded-Access-Token`` HTTP header — Apps runtime fallback.

The same precedence applies to ``workspace_host`` (``custom_inputs.workspace_host``
beats ``DATABRICKS_HOST`` env var), ``user_id`` (``custom_inputs.user_id``
beats ``X-Forwarded-User``), and ``user_email``.
"""

from __future__ import annotations

import os
from typing import Any, Mapping


__all__ = ["extract_obo_headers"]


def _coerce_headers(headers: Any) -> Mapping[str, str]:
    """Return a mapping-like view of ``headers``.

    Accepts:
      * ``None`` → ``{}``
      * ``dict[str, str]``
      * Any object with both ``.get`` and ``.items`` (``starlette.Headers``,
        ``requests.structures.CaseInsensitiveDict``, etc.).
      * Any object with just ``.get`` (treated as opaque getter).
      * Iterable of ``(key, value)`` tuples.
    """
    if headers is None:
        return {}
    if isinstance(headers, Mapping):
        return headers

    has_get = hasattr(headers, "get")
    has_items = hasattr(headers, "items")

    if has_get and has_items:
        class _GetItemsAdapter(Mapping):  # type: ignore[type-arg]
            """Wrap an object that has ``.get`` + ``.items`` but isn't a Mapping."""

            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getitem__(self, k: str) -> str:
                v = self._inner.get(k)
                if v is None:
                    raise KeyError(k)
                return v

            def get(self, k: str, default: Any = None) -> Any:
                return self._inner.get(k, default)

            def items(self):  # type: ignore[override]
                return list(self._inner.items())

            def __iter__(self):
                return iter(k for k, _ in self._inner.items())

            def __len__(self) -> int:
                return len(list(self._inner.items()))

        return _GetItemsAdapter(headers)

    if has_get:
        class _GetOnly(Mapping):  # type: ignore[type-arg]
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getitem__(self, k: str) -> str:
                v = self._inner.get(k)
                if v is None:
                    raise KeyError(k)
                return v

            def get(self, k: str, default: Any = None) -> Any:
                return self._inner.get(k, default)

            def __iter__(self):  # pragma: no cover — never iterated
                return iter(())

            def __len__(self) -> int:  # pragma: no cover
                return 0

        return _GetOnly(headers)

    try:
        return dict(headers)  # iterable of pairs as last resort
    except TypeError:
        return {}


def _header_lookup(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup with hyphen/underscore tolerance."""
    if not headers:
        return None

    # Direct hit
    if name in headers:
        v = headers.get(name)
        if v:
            return v

    # Case-insensitive / underscore-tolerant scan
    candidates = {
        name.lower(),
        name.replace("-", "_").lower(),
        name.replace("_", "-").lower(),
    }
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except AttributeError:
        # The _GetOnly adapter has no items(). Try direct get on case variants.
        for variant in candidates:
            v = headers.get(variant, None)
            if v:
                return v
            v = headers.get(variant.upper(), None)
            if v:
                return v
            v = headers.get("-".join(p.capitalize() for p in variant.split("-")), None)
            if v:
                return v
        return None
    for k, v in items:
        if k and k.lower() in candidates and v:
            return v
    return None


def extract_obo_headers(
    *,
    custom_inputs: Mapping[str, Any] | None = None,
    headers: Any = None,
) -> dict[str, str]:
    """Normalize OBO context from either runtime convention.

    Args:
        custom_inputs: The ``custom_inputs`` dict on a ``ChatAgentRequest`` or
            ``ResponsesAgentRequest`` (Model Serving and Apps both pass these
            verbatim through to the agent). May contain ``user_token``,
            ``workspace_host``, ``user_id``, ``user_email``.
        headers: Per-request HTTP headers. Accepts dicts, starlette
            ``Headers``, or any object exposing ``.get(name, default)``. The
            recognized header names are:

              * ``X-Forwarded-Access-Token`` — OBO token injected by the
                Databricks Apps proxy.
              * ``X-Forwarded-User`` — Databricks user id.
              * ``X-Forwarded-Email`` — User email.

    Returns:
        Dict with keys ``user_token``, ``workspace_host``, ``user_id``, and
        ``user_email``. Keys whose value is unknown are omitted. The output
        is suitable to spread into a ``WorkspaceClient`` call via::

            obo = extract_obo_headers(custom_inputs=..., headers=...)
            ws = WorkspaceClient(token=obo["user_token"], host=obo["workspace_host"])

    Precedence: ``custom_inputs`` wins over headers. ``workspace_host``
    additionally falls back to the ``DATABRICKS_HOST`` env var when neither
    source provides it.
    """
    ci = dict(custom_inputs) if custom_inputs else {}
    hdrs = _coerce_headers(headers)
    out: dict[str, str] = {}

    # --- user_token (load-bearing field) -----------------------------------
    token = ci.get("user_token")
    if not token:
        token = _header_lookup(hdrs, "X-Forwarded-Access-Token")
    if token:
        out["user_token"] = str(token)

    # --- workspace_host ----------------------------------------------------
    host = ci.get("workspace_host")
    if not host:
        host = _header_lookup(hdrs, "X-Forwarded-Host")
    if not host:
        host = os.environ.get("DATABRICKS_HOST")
    if host:
        out["workspace_host"] = str(host)

    # --- user_id -----------------------------------------------------------
    user_id = ci.get("user_id")
    if not user_id:
        user_id = _header_lookup(hdrs, "X-Forwarded-User")
    if user_id:
        out["user_id"] = str(user_id)

    # --- user_email --------------------------------------------------------
    user_email = ci.get("user_email")
    if not user_email:
        user_email = _header_lookup(hdrs, "X-Forwarded-Email")
    if user_email:
        out["user_email"] = str(user_email)

    return out
