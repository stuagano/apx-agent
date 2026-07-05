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

import logging
import os
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "ApxIdentityError",
    "extract_obo_headers",
    "make_obo_workspace_client",
    "resolve_no_obo_or_raise",
    "scope_session_key",
]


def scope_session_key(
    custom_inputs: "dict[str, Any] | None", principal: str | None
) -> "dict[str, Any] | None":
    """Namespace the client ``session_id`` / ``thread_id`` by *principal* (#491).

    The checkpoint thread and conversation are keyed by the client-supplied
    session id. Without a principal component, two users who pick the same
    session id string collide on one thread/conversation — so on a shared-table
    App one user's history/pending interrupt can leak to another. Prefixing the
    key with a hash of the OBO principal isolates them.

    Transparent to clients: they always send the RAW session id, and the server
    re-derives the same namespaced key from their principal — so the same user
    resumes their own thread, while a different user with the same raw id gets a
    different key. Returns *custom_inputs* unchanged when there is no principal
    (local dev / single-user Model Serving) — nothing to isolate by.
    """
    if not custom_inputs or not principal:
        return custom_inputs
    from ._audit import hash_for_audit  # noqa: PLC0415

    ns = hash_for_audit(principal)
    scoped = dict(custom_inputs)
    for key in ("session_id", "thread_id"):
        if scoped.get(key):
            scoped[key] = f"{ns}:{scoped[key]}"
    return scoped

# Operators that genuinely run an agent as a service principal opt in here.
_SP_FALLBACK_ENV = "APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK"

# G2: warn at most once per process when a request falls open to the app SP.
_warned_no_obo_in_app = False


class ApxIdentityError(PermissionError):
    """A served request resolved no caller identity and the fail-closed policy
    rejected it rather than run as the app service principal (G2)."""


def _sp_fallback_allowed() -> bool:
    """True when an operator has explicitly opted into the app-SP fallback.

    Absence of the env var means *not opted in* — i.e. fail closed. That safe
    default is the point of this gate, so we read with no inline default and
    treat unset (``None``) as disabled.
    """
    val = os.environ.get(_SP_FALLBACK_ENV)
    return val is not None and val.strip().lower() in ("1", "true", "yes", "on")


def resolve_no_obo_or_raise() -> None:
    """Identity policy for a served request that resolved **no** OBO user token.

    Fails CLOSED in the Databricks Apps (multi-user) runtime: raises
    :class:`ApxIdentityError` so the request is rejected rather than silently
    running tools as the app service principal (cross-user data access).

    Operators that genuinely run as a service principal opt in with
    ``APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true``, which downgrades the reject to
    a one-time warning. Outside the Apps runtime (local dev, and Model Serving
    which is service-principal by default) this is a no-op so the existing
    behavior is unchanged. See ``docs/design/served-path-guards-and-identity.md`` (G2).
    """
    if not _in_databricks_app():
        return
    if _sp_fallback_allowed():
        warn_once_no_obo_in_app()
        return
    raise ApxIdentityError(
        "Rejected: no OBO user token on this request in the Databricks Apps "
        "runtime. apx-agent fails closed — otherwise tools would run as the app "
        "service principal with cross-user data access. Forward the caller's "
        "X-Forwarded-Access-Token (or custom_inputs.user_token) per request. To "
        "intentionally run as the app service principal, set "
        f"{_SP_FALLBACK_ENV}=true. See "
        "docs/design/served-path-guards-and-identity.md (G2)."
    )


def warn_once_no_obo_in_app() -> None:
    """Log one WARNING when a request resolves no OBO user token while running in
    the Databricks Apps (multi-user) runtime.

    In that case tools run as the **app service principal** (not the caller) and
    memory falls back to a single shared principal — cross-user identity bleed.
    Fires at most once per process. The fail-closed fix is tracked separately;
    see ``docs/design/served-path-guards-and-identity.md`` (G2).
    """
    global _warned_no_obo_in_app
    if _warned_no_obo_in_app or not _in_databricks_app():
        return
    _warned_no_obo_in_app = True
    logger.warning(
        "apx-agent: a request resolved no OBO user token in the Databricks Apps "
        "runtime — tools run as the app service principal (not the caller) and "
        "memory falls back to a shared principal (cross-user identity bleed). "
        "Forward X-Forwarded-Access-Token or custom_inputs.user_token per "
        "request. See docs/design/served-path-guards-and-identity.md (G2)."
    )


def _in_databricks_app() -> bool:
    """True when running inside the Databricks Apps runtime.

    Apps auto-injects reserved env vars including ``DATABRICKS_APP_NAME`` and
    ``DATABRICKS_APP_URL`` (see the Databricks Apps "system environment" docs).
    Their presence is a reliable App-context marker, used to suppress the
    ``X-Forwarded-Host`` workspace-host fallback (that header is the app's own
    hostname in an App, not the workspace API host).
    """
    return bool(
        os.environ.get("DATABRICKS_APP_NAME")
        or os.environ.get("DATABRICKS_APP_URL")
    )


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
    # Precedence: caller-supplied custom_inputs > DATABRICKS_HOST env (Apps
    # runtime injects the workspace API host here) > X-Forwarded-Host (last
    # resort — in Apps this is the App's public hostname, NOT the workspace
    # API URL, so it's only useful for non-Apps proxies that pass the API
    # host through). Verified on fe-stable 2026-05-20: the App proxy's
    # X-Forwarded-Host is the user-facing app subdomain and using it as the
    # workspace_host produces a hostname that does not serve the workspace
    # REST API.
    host = ci.get("workspace_host")
    if not host:
        host = os.environ.get("DATABRICKS_HOST")
    if not host and not _in_databricks_app():
        # X-Forwarded-Host last resort — ONLY outside the Databricks Apps
        # runtime. Inside an App this header is the app's OWN public hostname
        # (…databricksapps.com), NOT the workspace REST API host. Using it as
        # workspace_host makes user-scoped SDK calls loop back to the app
        # (which doesn't serve /api/2.0/*), each attempt read-times-out at 60s
        # and the SDK retries to its 300s ceiling → a 5-minute hang. In-App we
        # rely on DATABRICKS_HOST (auto-injected); if it's anomalously absent
        # we leave workspace_host unset so the SDK fails fast with a clear
        # config error instead of hanging. Kept for non-Apps proxies that do
        # pass the workspace API host through this header.
        host = _header_lookup(hdrs, "X-Forwarded-Host")
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


def make_obo_workspace_client(obo: Mapping[str, Any]) -> Any:
    """Build a ``WorkspaceClient`` that authenticates as the calling user.

    Apps containers ship with the App service principal's ``DATABRICKS_CLIENT_ID`` /
    ``DATABRICKS_CLIENT_SECRET`` in the environment for ambient SP auth. Passing
    ``token=`` and ``host=`` directly to ``WorkspaceClient`` does NOT suppress
    those ambient credentials — the SDK's ``Config.validate`` raises
    ``"more than one authorization method configured: oauth and pat"`` because
    both PAT (the OBO token) and OAuth M2M (the SP env vars) are present.

    This helper constructs an explicit ``Config`` with ``auth_type="pat"`` so
    the SDK uses ONLY the OBO token. The resulting WorkspaceClient's API calls
    run as the calling user (the principal the OBO token was minted for), not
    the App SP.

    Verified on fe-stable 2026-05-20: with this helper, ``current_user.me()``
    returns the calling user's email; without it the bare-``token=`` pattern
    fails with the multi-auth ValueError.

    Args:
        obo: Dict returned by :func:`extract_obo_headers`. Must contain
            ``user_token`` and ``workspace_host``.

    Returns:
        A ``databricks.sdk.WorkspaceClient`` configured for OBO auth.

    Raises:
        ImportError: If ``databricks-sdk`` is not installed.
        ValueError: If ``obo`` is missing ``user_token`` or ``workspace_host``.
    """
    token = obo.get("user_token")
    host = obo.get("workspace_host")
    if not token:
        raise ValueError("make_obo_workspace_client: obo['user_token'] is required")
    if not host:
        raise ValueError("make_obo_workspace_client: obo['workspace_host'] is required")
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.core import Config
    except ImportError as exc:  # pragma: no cover — defensive
        raise ImportError(
            "make_obo_workspace_client requires databricks-sdk; "
            "install apx-agent[runtime]."
        ) from exc
    cfg = Config(host=str(host), token=str(token), auth_type="pat")
    return WorkspaceClient(config=cfg)
