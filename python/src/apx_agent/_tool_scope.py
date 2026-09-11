"""Per-tool scope ceiling — declared identity + UC/secret authority bound.

A tool may declare *its own* execution identity and a permission ceiling —
which UC catalogs/schemas/tables/functions it may touch and which secret
scopes it may read — in its ``[[tool.apx.tools]]`` table or in a
``build_tool(...)`` call. This module holds that declaration (:class:`ToolScope`),
its two enforcement points, and the runtime error:

  * **Compile time** — :func:`validate_tool_scope` refuses a build/deploy when a
    tool's declared :class:`~apx_agent._resources.ResourceSpec` (a UC table /
    function) falls outside its declared ceiling. Raises ``ToolConfigError`` (the
    module's existing config-error idiom) naming the tool + the offending
    identifier.
  * **Runtime** — :class:`ScopeGuard` is a ``before_tool`` guard (sibling of
    ``WatchdogGuard.for_tool()``). When a call's target UC object or secret scope
    is outside the ceiling it raises :class:`ScopeDenied`, stamps the active span
    with an audit event, and logs a WARNING. ``ScopeDenied`` subclasses
    ``PermissionError`` so ``_governance_exception_middleware`` already contains
    it into an error ``ToolMessage`` — the turn stays alive, no HTTP 500.

Back-compat: a tool that declares no scope attaches no ``_apx_scope``; the guard
skips it and the validator is a no-op. Purely additive — undeclared stays today's
unrestricted behavior (this is not deny-by-default).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from ._audit import AuditAttrs, set_audit_attrs
from ._mlflow_tracing import current_active_span
from ._resources import get_resources
from ._tool import ExecutionIdentity, ToolMetadata, get_tool_metadata
from ._tool_config import ToolConfigError

logger = logging.getLogger(__name__)

__all__ = [
    "ToolScope",
    "ScopeDenied",
    "ScopeGuard",
    "parse_tool_scope",
    "attach_scope",
    "get_scope",
    "validate_tool_scope",
    "in_scope",
    "identity_to_execution",
]


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class ScopeDenied(PermissionError):
    """A tool call breached its declared scope ceiling — refuse it.

    Distinct *by type* from :class:`~apx_agent._errors.ToolError`: a
    ``ToolError`` is an operational finding (denied query, missing table) the
    agent should reason about; a ``ScopeDenied`` is an *authorization ceiling
    breach* — the tool declared it may not touch this object. Consumers branch
    on the type. Subclasses ``PermissionError`` so
    ``_compile._governance_exception_middleware._CONTAINED`` contains it into an
    error ``ToolMessage`` instead of a 500. Messages are ``scope_denied:``-prefixed.
    """


# ---------------------------------------------------------------------------
# Scope declaration
# ---------------------------------------------------------------------------


# Identity modes a tool may declare. "sp"/"obo" map to the two
# ExecutionIdentity values; "named-sp:<name>" pins a specific narrow service
# principal (still execution="service"; the name rides ToolMetadata).
_NAMED_SP_PREFIX = "named-sp:"


@dataclass(frozen=True)
class ToolScope:
    """A tool's declared identity + permission ceiling.

    Every field is optional — an all-empty scope constrains nothing on that
    dimension (undeclared = unrestricted, matching back-compat). ``identity`` is
    the raw declared mode (``"sp"`` / ``"obo"`` / ``"named-sp:<name>"``);
    :func:`identity_to_execution` maps it to an ``ExecutionIdentity``.

    UC-object match semantics (see :func:`in_scope`): a ``catalogs`` entry
    matches any object under that catalog; a ``schemas`` entry
    (``catalog.schema``) matches any object under it; ``tables``/``functions``
    entries match their exact fully-qualified name.
    """

    identity: str | None = None
    catalogs: tuple[str, ...] = field(default_factory=tuple)
    schemas: tuple[str, ...] = field(default_factory=tuple)
    tables: tuple[str, ...] = field(default_factory=tuple)
    functions: tuple[str, ...] = field(default_factory=tuple)
    secret_scopes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.identity is not None:
            _validate_identity(self.identity)

    @property
    def has_uc_ceiling(self) -> bool:
        """True when any UC-object dimension is declared (so it constrains)."""
        return bool(self.catalogs or self.schemas or self.tables or self.functions)


def _validate_identity(identity: str) -> None:
    if identity in ("sp", "obo"):
        return
    if identity.startswith(_NAMED_SP_PREFIX):
        if not identity[len(_NAMED_SP_PREFIX):].strip():
            raise ToolConfigError(
                f"identity {identity!r} is missing the service-principal name "
                f"after {_NAMED_SP_PREFIX!r}."
            )
        return
    raise ToolConfigError(
        f"identity {identity!r} must be 'sp', 'obo', or 'named-sp:<name>'."
    )


def identity_to_execution(identity: str) -> ExecutionIdentity:
    """Map a declared identity mode to an ``ExecutionIdentity``.

    ``obo`` → ``"user"``; ``sp`` and ``named-sp:<name>`` → ``"service"``.
    """
    _validate_identity(identity)
    return "user" if identity == "obo" else "service"


def named_sp_name(identity: str | None) -> str | None:
    """Return the service-principal name from a ``named-sp:<name>`` identity, else None."""
    if identity and identity.startswith(_NAMED_SP_PREFIX):
        return identity[len(_NAMED_SP_PREFIX):].strip()
    return None


def _as_str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ToolConfigError(
            f"scope field {key!r} must be a list of non-empty strings; got {value!r}."
        )
    return list(value)


def parse_tool_scope(
    identity: Any = None,
    scope: Any = None,
    secret_scopes: Any = None,
) -> ToolScope | None:
    """Build a :class:`ToolScope` from the declared keys, or ``None`` if none set.

    ``identity`` is a string; ``scope`` is a table with optional
    ``catalogs``/``schemas``/``tables``/``functions`` string lists;
    ``secret_scopes`` is a string list. Malformed input raises ``ToolConfigError``.
    Returns ``None`` when nothing is declared (back-compat — attaches no scope).
    """
    if identity is None and scope is None and secret_scopes is None:
        return None
    if identity is not None and not isinstance(identity, str):
        raise ToolConfigError(f"identity must be a string; got {identity!r}.")
    if scope is not None and not isinstance(scope, dict):
        raise ToolConfigError(f"scope must be a table/dict; got {scope!r}.")
    scope = scope or {}
    unknown = set(scope) - {"catalogs", "schemas", "tables", "functions"}
    if unknown:
        raise ToolConfigError(
            f"scope has unknown key(s) {sorted(unknown)}; "
            f"valid: catalogs, schemas, tables, functions."
        )
    return ToolScope(
        identity=identity,
        catalogs=tuple(_as_str_list(scope.get("catalogs"), "catalogs")),
        schemas=tuple(_as_str_list(scope.get("schemas"), "schemas")),
        tables=tuple(_as_str_list(scope.get("tables"), "tables")),
        functions=tuple(_as_str_list(scope.get("functions"), "functions")),
        secret_scopes=tuple(_as_str_list(secret_scopes, "secret_scopes")),
    )


# ---------------------------------------------------------------------------
# Tool-side annotation (mirrors _resources.attach_resources / get_resources)
# ---------------------------------------------------------------------------


def attach_scope(fn: Any, scope: ToolScope | None) -> Any:
    """Annotate ``fn`` with ``_apx_scope`` (and stamp identity → execution). Returns ``fn``.

    A ``None`` scope is a no-op (back-compat). When the scope declares an
    ``identity``, the corresponding ``ExecutionIdentity`` (and named-SP name) is
    stamped onto the tool's ``ToolMetadata`` so the existing Apps-authorization
    path resolves the credential — reusing ``build_tool``'s ``execution`` seam.
    """
    if scope is None:
        return fn
    fn._apx_scope = scope  # type: ignore[attr-defined]
    if scope.identity is not None:
        metadata = get_tool_metadata(fn) or ToolMetadata()
        # ponytail: a declared identity overrides ToolMetadata.execution
        # unconditionally — the scope declaration is the source of truth. We do
        # NOT detect a conflict against an explicitly-set execution here; if
        # that becomes a real footgun, add a conflict-raise mirroring
        # _apps_authorization's explicit-vs-inferred check.
        fn._apx_tool = replace(  # type: ignore[attr-defined]
            metadata,
            execution=identity_to_execution(scope.identity),
            service_principal_name=named_sp_name(scope.identity),
        )
    return fn


def get_scope(fn: Any) -> ToolScope | None:
    """Return the :class:`ToolScope` attached to ``fn``, or ``None`` if unset."""
    scope = getattr(fn, "_apx_scope", None)
    return scope if isinstance(scope, ToolScope) else None


# ---------------------------------------------------------------------------
# Scope matching
# ---------------------------------------------------------------------------


def _norm_uc(ident: str) -> str:
    """Normalize a UC identifier for comparison.

    UC identifiers are case-insensitive and backtick-quoting is cosmetic, so
    ``main.finance.LEDGER`` and ``` `main`.`finance`.`ledger` ``` both match a
    ``main.finance.ledger`` ceiling.
    """
    return ident.replace("`", "").casefold()


def in_scope(scope: ToolScope, ref: str) -> bool:
    """Is UC identifier ``ref`` (``catalog[.schema[.object]]``) within the ceiling?

    Precedence: a declared ``tables``/``functions`` entry matches its exact FQN;
    a ``catalogs`` entry matches any object under that catalog; a ``schemas``
    entry (``catalog.schema``) matches any object under it. When no UC dimension
    is declared, everything is in scope (undeclared = unrestricted). Matching is
    case-insensitive and backtick-insensitive (UC identifier semantics).
    """
    if not scope.has_uc_ceiling:
        return True
    ref_n = _norm_uc(ref)
    if ref_n in {_norm_uc(t) for t in scope.tables} or ref_n in {_norm_uc(f) for f in scope.functions}:
        return True
    parts = ref_n.split(".")
    if parts[0] in {_norm_uc(c) for c in scope.catalogs}:
        return True
    if len(parts) >= 2 and ".".join(parts[:2]) in {_norm_uc(s) for s in scope.schemas}:
        return True
    return False


def secret_in_scope(scope: ToolScope, secret_scope: str) -> bool:
    """Is ``secret_scope`` within the declared secret-scope allowlist?

    An empty allowlist constrains nothing (undeclared = unrestricted).
    """
    if not scope.secret_scopes:
        return True
    return secret_scope in scope.secret_scopes


# Kinds the UC ceiling governs. Other resource kinds (endpoints, warehouses)
# are not UC securables addressed by catalog/schema/table/function names.
_UC_KINDS = frozenset({"uc_table", "uc_function", "vector_search_index"})

# Call-argument names that carry a fully-qualified UC identifier. ponytail:
# v1 enforces on declared resources + these well-known arg names + secret_scope,
# NOT on parsed free-form SQL bodies (a follow-up — see PRD Risks/out_of_scope).
_UC_ARG_KEYS = frozenset({
    "table", "table_name", "function", "function_name", "full_name",
    "uc_name", "identifier", "securable_full_name",
})
# Only the unambiguous secret-scope arg name. Plain "scope" is a common
# non-secret arg (OAuth/search/config scope) — gating on it caused spurious
# ScopeDenied on legitimate calls.
_SECRET_ARG_KEYS = frozenset({"secret_scope"})


# ---------------------------------------------------------------------------
# Compile-time enforcement
# ---------------------------------------------------------------------------


def validate_tool_scope(fn: Any) -> None:
    """Raise ``ToolConfigError`` if ``fn`` is over-scoped or its scope is malformed.

    No ``_apx_scope`` → no-op (back-compat). Otherwise every declared UC
    ``ResourceSpec`` (table / function) must fall within the ceiling; the first
    that doesn't fails loud, naming the tool, the offending identifier, and the
    ceiling that rejected it.
    """
    scope = get_scope(fn)
    if scope is None:
        return
    if scope.identity is not None:
        _validate_identity(scope.identity)  # re-check when attached directly in Python
    if not scope.has_uc_ceiling:
        return
    name = getattr(fn, "__name__", "<tool>")
    for spec in get_resources(fn):
        if spec.kind in _UC_KINDS and not in_scope(scope, spec.identifier):
            raise ToolConfigError(
                f"tool {name!r} is over-scoped: it declares {spec.kind} "
                f"{spec.identifier!r}, outside its scope ceiling "
                f"(catalogs={list(scope.catalogs)}, schemas={list(scope.schemas)}, "
                f"tables={list(scope.tables)}, functions={list(scope.functions)})."
            )


# ---------------------------------------------------------------------------
# Runtime enforcement — before_tool guard
# ---------------------------------------------------------------------------


class ScopeGuard:
    """``before_tool`` guard that enforces each tool's declared scope ceiling.

    Constructed with the agent's tool callables so it can map a call's tool
    ``name`` back to its :class:`ToolScope`. For a scoped tool it checks the
    call's target UC object(s) — the tool's declared UC resources plus any
    fully-qualified UC identifier passed as an argument — and any referenced
    secret scope against the ceiling. Out of scope → :class:`ScopeDenied`, an
    audit event on the active span, and a WARNING log. A tool with no scope is a
    strict no-op (back-compat).

    Sibling of ``WatchdogGuard.for_tool()``::

        before_tool=compose(WatchdogGuard(wd).for_tool(), ScopeGuard(tools).for_tool())
    """

    def __init__(self, tools: Iterable[Any]) -> None:
        self._scopes: dict[str, ToolScope] = {}
        self._resources: dict[str, list[str]] = {}
        for fn in tools:
            scope = get_scope(fn)
            if scope is None:
                continue
            name = getattr(fn, "__name__", None)
            if not name:
                continue
            self._scopes[name] = scope
            self._resources[name] = [
                s.identifier for s in get_resources(fn) if s.kind in _UC_KINDS
            ]

    @property
    def active(self) -> bool:
        """True when at least one tool declares a scope (else the guard is a no-op)."""
        return bool(self._scopes)

    def _deny(self, tool_name: str, obj: str, kind: str, scope: ToolScope) -> None:
        if kind == "secret":
            reason = f"secret scope {obj!r} outside allowlist {list(scope.secret_scopes)}"
        else:
            reason = (
                f"{obj!r} outside ceiling (catalogs={list(scope.catalogs)}, "
                f"schemas={list(scope.schemas)}, tables={list(scope.tables)}, "
                f"functions={list(scope.functions)})"
            )
        set_audit_attrs(
            current_active_span(),
            scope_action="deny",
            scope_object=obj,
            scope_reason=reason,
        )
        logger.warning("scope_denied: tool %r may not touch %s — %s", tool_name, obj, reason)
        raise ScopeDenied(f"scope_denied: tool {tool_name!r} may not touch {obj!r} ({reason})")

    def for_tool(self):  # noqa: ANN201 — matches WatchdogGuard.for_tool return shape
        """Return a ``before_tool``-compatible callable ``(name, args) -> None``."""

        def _check(tool_name: str, args: dict[str, Any]) -> None:
            scope = self._scopes.get(tool_name)
            if scope is None:
                return  # unscoped tool — no-op (back-compat)
            for ref in self._resources.get(tool_name, []):
                if not in_scope(scope, ref):
                    self._deny(tool_name, ref, "uc", scope)
            if not isinstance(args, dict):
                return
            for key, value in args.items():
                if not isinstance(value, str) or not value:
                    continue
                if key in _UC_ARG_KEYS and "." in value and not in_scope(scope, value):
                    self._deny(tool_name, value, "uc", scope)
                if key in _SECRET_ARG_KEYS and not secret_in_scope(scope, value):
                    self._deny(tool_name, value, "secret", scope)

        return _check


# AuditAttrs guard: scope audit keys must exist (fail loud if _audit.py drifts).
assert hasattr(AuditAttrs, "SCOPE_ACTION"), "add SCOPE_* keys to _audit.AuditAttrs"
