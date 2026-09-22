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
    ``GovernanceGuard.for_tool()``). It best-effort inspects well-known UC arg
    names and ``secret_scope``; declared ResourceSpecs
    are *not* re-checked here (compile-time already refused any that were
    over-scope). Unqualified identifiers (``table_name="ledger"``) are a v1
    non-goal — resolving them needs a session default catalog we do not have.
    Out of scope → :class:`ScopeDenied`, an audit event, and a WARNING.
    ``ScopeDenied`` subclasses ``PermissionError`` so
    ``_governance_exception_middleware`` already contains it into an error
    ``ToolMessage`` — the turn stays alive, no HTTP 500.

Back-compat: a tool that declares no scope attaches no ``_apx_scope``; the guard
skips it and the validator is a no-op. Purely additive — undeclared stays today's
unrestricted behavior (this is not deny-by-default).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from collections.abc import Callable, Iterable
from typing import Any

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
    "attach_scope_guard",
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


def _split_uc(ident: str) -> list[str]:
    """Split a UC identifier into casefolded segments, respecting backtick quoting.

    UC identifiers are case-insensitive and backtick-quoting is cosmetic, but a
    dot *inside* a backtick-quoted segment is part of the name, not a separator:
    ``main.finance.LEDGER`` → ``[main, finance, ledger]`` and
    ``` `my.catalog`.sch.tbl ``` → ``[my.catalog, sch, tbl]``.
    ponytail: does not handle escaped backticks (``` `` ```) inside a quoted
    segment — an exotic name we don't support; upgrade the tokenizer if it ever
    matters.
    """
    segments: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in ident:
        if ch == "`":
            quoted = not quoted
        elif ch == "." and not quoted:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    segments.append("".join(buf))
    return [s.casefold() for s in segments]


def in_scope(scope: ToolScope, ref: str) -> bool:
    """Is UC identifier ``ref`` (``catalog[.schema[.object]]``) within the ceiling?

    Precedence: a declared ``tables``/``functions`` entry matches its exact FQN;
    a ``catalogs`` entry matches any object under that catalog; a ``schemas``
    entry (``catalog.schema``) matches any object under it. When no UC dimension
    is declared, everything is in scope (undeclared = unrestricted). Matching is
    case-insensitive and backtick-insensitive (UC identifier semantics), and
    compares segment-wise so a quoted dot is never treated as a separator.

    Unqualified one-segment names (``ledger``) are *not* resolved against a
    default catalog — that is a v1 non-goal. Callers that only have a bare
    name cannot be denied here without inventing session context we don't have.
    """
    if not scope.has_uc_ceiling:
        return True
    ref_seg = _split_uc(ref)
    if any(ref_seg == _split_uc(t) for t in scope.tables) or any(ref_seg == _split_uc(f) for f in scope.functions):
        return True
    if any(ref_seg[:1] == _split_uc(c) for c in scope.catalogs):
        return True
    if len(ref_seg) >= 2 and any(ref_seg[:2] == _split_uc(s) for s in scope.schemas):
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
# runtime enforcement is this well-known-arg scan + secret_scope, NOT a second
# pass over declared ResourceSpecs (those are compile-time only) and NOT parsed
# free-form SQL bodies (a follow-up — see PRD Risks/out_of_scope). Only dotted
# values are checked — unqualified identifiers are a documented v1 non-goal.
# Only UC-specific arg names — generic ones (identifier / full_name / uc_name)
# collide with common non-UC args (a dotted value like "example.com" or "v1.2.3"
# would trip a spurious ScopeDenied), same reason plain "scope" is excluded below.
_UC_ARG_KEYS = frozenset({
    "table", "table_name", "function", "function_name", "securable_full_name",
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

    ``tools`` is either an iterable of callables or a zero-arg getter that
    returns one. A getter (or the live ``leaf._tool_fns`` list) means late
    ``_register_tool`` / ``bind_workspace`` updates are visible without
    re-composing the hook.

    For a scoped tool the guard inspects well-known UC arg names and any
    ``secret_scope`` argument. Declared ``ResourceSpec`` s are enforced at
    compile time by :func:`validate_tool_scope` and are intentionally *not*
    re-checked here — that loop could never deny a tool that passed build.
    Unqualified identifiers (``table_name="ledger"``) are a v1 non-goal:
    arg-scan only fires on dotted values in ``_UC_ARG_KEYS``.

    Out of scope → :class:`ScopeDenied`, an audit event on the active span,
    and a WARNING log. A tool with no scope is a strict no-op (back-compat).

    Sibling of ``GovernanceGuard.for_tool()``::

        before_tool=compose(GovernanceGuard(wd).for_tool(), ScopeGuard(tools).for_tool())
    """

    def __init__(self, tools: Iterable[Any] | Callable[[], Iterable[Any]]) -> None:
        self._tools = tools

    def _iter_tools(self) -> Iterable[Any]:
        tools = self._tools
        if callable(tools):
            tools = tools()
        return tools or ()

    def _scope_for(self, tool_name: str) -> ToolScope | None:
        for fn in self._iter_tools():
            if getattr(fn, "__name__", None) == tool_name:
                return get_scope(fn)
        return None

    @property
    def active(self) -> bool:
        """True when at least one tool declares a scope (else the guard is a no-op)."""
        return any(get_scope(fn) is not None for fn in self._iter_tools())

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

    def for_tool(self):  # noqa: ANN201 — matches GovernanceGuard.for_tool return shape
        """Return a ``before_tool``-compatible callable ``(name, args) -> None``."""

        def _check(tool_name: str, args: dict[str, Any]) -> None:
            scope = self._scope_for(tool_name)
            if scope is None:
                return  # unscoped tool — no-op (back-compat)
            if not isinstance(args, dict):
                return
            for key, value in args.items():
                if not isinstance(value, str) or not value:
                    continue
                # v1 non-goal: unqualified identifiers (no ".") never reach
                # in_scope — resolving them needs a default catalog we don't have.
                if key in _UC_ARG_KEYS and "." in value and not in_scope(scope, value):
                    self._deny(tool_name, value, "uc", scope)
                if key in _SECRET_ARG_KEYS and not secret_in_scope(scope, value):
                    self._deny(tool_name, value, "secret", scope)

        return _check


def attach_scope_guard(leaf: Any) -> bool:
    """Attach a live :class:`ScopeGuard` onto ``leaf._before_tool`` if needed.

    Idempotent via ``_apx_scope_guard``: a second call is a no-op even when
    the leaf later gains more scoped tools — the live tool map already sees
    them. Returns True when a hook was newly attached. A leaf with no scoped
    tools is left byte-for-byte unchanged.
    """
    if getattr(leaf, "_apx_scope_guard", None) is not None:
        return False
    if not hasattr(leaf, "_before_tool"):
        return False

    def _live_tools() -> Iterable[Any]:
        return getattr(leaf, "_tool_fns", []) or []

    guard = ScopeGuard(_live_tools)
    if not guard.active:
        return False

    hook = guard.for_tool()
    existing = getattr(leaf, "_before_tool", None)
    if existing is not None:
        from ._guards import compose  # noqa: PLC0415

        setattr(leaf, "_before_tool", compose(existing, hook))
    else:
        setattr(leaf, "_before_tool", hook)
    setattr(leaf, "_apx_scope_guard", guard)
    return True


# AuditAttrs guard: scope audit keys must exist (fail loud if _audit.py drifts).
assert hasattr(AuditAttrs, "SCOPE_ACTION"), "add SCOPE_* keys to _audit.AuditAttrs"
