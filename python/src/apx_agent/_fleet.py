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
