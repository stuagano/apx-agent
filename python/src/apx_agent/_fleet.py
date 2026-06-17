"""Fleet selection + bulk-operation helpers.

Pure logic only — no Databricks/MLflow imports at module top level. The CLI
layer fetches model objects and performs tag/alias writes; this module turns
model objects + predicates into resolved agents and renders outcome summaries.

Tag namespaces:
  * ``apx.agent.*`` / ``apx.apps.*`` — system tags (reserved; never written by
    ``fleet tag``).
  * ``apx.label.*`` — user labels (what ``fleet tag`` writes/removes).
"""
from __future__ import annotations

LABEL_PREFIX = "apx.label."
RESERVED_PREFIXES = ("apx.agent.", "apx.apps.")
NAME_TAG = "apx.agent.name"
MODEL_TAG = "apx.agent.model"
APP_NAME_TAG = "apx.apps.app_name"


def to_label_key(key: str) -> str:
    """Map a bare user key into the ``apx.label.`` namespace (idempotent)."""
    return key if key.startswith(LABEL_PREFIX) else LABEL_PREFIX + key


def is_reserved(key: str) -> bool:
    """True if ``key`` is a system tag that ``fleet tag`` must not touch."""
    return any(key.startswith(p) for p in RESERVED_PREFIXES)


def parse_where(exprs: list[str]) -> dict[str, str]:
    """Parse repeated ``--where key=value`` flags into a dict (AND semantics)."""
    out: dict[str, str] = {}
    for expr in exprs:
        if "=" not in expr:
            raise ValueError(f"--where must be key=value, got: {expr!r}")
        key, value = expr.split("=", 1)
        out[key.strip()] = value.strip()
    return out


from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any


@dataclass
class ResolvedAgent:
    """One agent selected by the fleet resolver."""
    uc_name: str
    name: str
    model: str | None
    app_name: str | None
    tags: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


def _tags_dict(model: Any) -> dict[str, str]:
    return {t.key: t.value for t in (getattr(model, "tags", None) or [])}


def _uc_name(model: Any) -> str:
    full = getattr(model, "full_name", None)
    if full:
        return str(full)
    return (
        f"{getattr(model, 'catalog_name', '')}."
        f"{getattr(model, 'schema_name', '')}."
        f"{getattr(model, 'name', '')}"
    )


def _matches_where(tags: dict[str, str], where: dict[str, str]) -> bool:
    for key, value in where.items():
        candidates = {tags.get(key), tags.get(to_label_key(key))}
        if value not in candidates:
            return False
    return True


def resolve_agents(
    models: Any,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    name_glob: str | None = None,
    where: dict[str, str] | None = None,
    uc_names: list[str] | None = None,
) -> list[ResolvedAgent]:
    """Filter registered-model objects into ``ResolvedAgent`` records.

    Only models carrying the ``apx.agent.name`` tag are considered. When
    ``uc_names`` is given, it selects exactly those models and bypasses the
    scope/glob/where filters. Otherwise all of ``catalog``/``schema``/
    ``name_glob``/``where`` are AND-ed.
    """
    where = where or {}
    wanted = set(uc_names or [])
    out: list[ResolvedAgent] = []
    for model in models:
        tags = _tags_dict(model)
        if NAME_TAG not in tags:
            continue
        uc = _uc_name(model)
        if wanted:
            if uc not in wanted:
                continue
        else:
            if catalog and getattr(model, "catalog_name", None) != catalog:
                continue
            if schema and getattr(model, "schema_name", None) != schema:
                continue
            if name_glob and not fnmatch(tags.get(NAME_TAG, ""), name_glob):
                continue
            if where and not _matches_where(tags, where):
                continue
        labels = {
            k[len(LABEL_PREFIX):]: v
            for k, v in tags.items()
            if k.startswith(LABEL_PREFIX)
        }
        out.append(
            ResolvedAgent(
                uc_name=uc,
                name=tags.get(NAME_TAG, ""),
                model=tags.get(MODEL_TAG),
                app_name=tags.get(APP_NAME_TAG),
                tags=tags,
                labels=labels,
            )
        )
    return out


@dataclass
class AgentOutcome:
    """Result of one per-agent action in a bulk command."""
    uc_name: str
    status: str  # "ok" | "skipped" | "failed"
    detail: str = ""


def render_summary(outcomes: list[AgentOutcome], *, apply: bool) -> tuple[str, int]:
    """Render a per-agent result table + summary line.

    Returns ``(text, exit_code)``. ``exit_code`` is 1 if any outcome failed,
    else 0. When ``apply`` is False the header marks the run as a dry-run.
    """
    lines: list[str] = []
    header = "Fleet plan (dry-run — nothing changed; pass --apply to execute):" if not apply \
        else "Fleet result:"
    lines.append(header)
    for o in outcomes:
        lines.append(f"  [{o.status:<7}] {o.uc_name}" + (f"  {o.detail}" if o.detail else ""))
    n_ok = sum(1 for o in outcomes if o.status == "ok")
    n_skip = sum(1 for o in outcomes if o.status == "skipped")
    n_fail = sum(1 for o in outcomes if o.status == "failed")
    lines.append(f"Summary: {n_ok} ok, {n_skip} skipped, {n_fail} failed")
    return "\n".join(lines), (1 if n_fail else 0)
