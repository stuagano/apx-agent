"""Load and validate a coworker YAML spec into an AgentConfig."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._models import AgentConfig


class SpecValidationError(ValueError):
    """Raised when a YAML spec fails validation."""


def _resolve_env_vars(value: Any) -> Any:
    """Recursively replace $VAR / ${VAR} in string leaves."""
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            key = m.group(1) or m.group(2)
            return os.environ.get(key, m.group(0))
        return re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)", _sub, value)
    if isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    return value


_VAR_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Scaffold flags that resolve common placeholder variables.
_VAR_HINTS: dict[str, str] = {
    "CATALOG": "pass --catalog <catalog>  or  export CATALOG=<value>",
    "SCHEMA":  "pass --schema <schema>    or  export SCHEMA=<value>",
}


def _collect_unresolved(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Return (field_path, var_name) for every unresolved $VAR in *value*."""
    hits: list[tuple[str, str]] = []
    if isinstance(value, str):
        for m in _VAR_RE.finditer(value):
            hits.append((path, m.group(1) or m.group(2)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            hits.extend(_collect_unresolved(item, f"{path}[{i}]"))
    elif isinstance(value, dict):
        for k, v in value.items():
            hits.extend(_collect_unresolved(v, f"{path}.{k}" if path else k))
    return hits


def _check_unresolved_vars(data: dict[str, Any]) -> None:
    """Raise SpecValidationError if any $VAR placeholders were not substituted."""
    hits = _collect_unresolved(data)
    if not hits:
        return
    lines = []
    seen: set[tuple[str, str]] = set()
    for field, var in hits:
        key = (field, var)
        if key in seen:
            continue
        seen.add(key)
        hint = _VAR_HINTS.get(var, f"export {var}=<value>")
        lines.append(f"  {field:<35} ${var:<20} ({hint})")
    detail = "\n".join(lines)
    raise SpecValidationError(
        f"YAML contains unresolved placeholder(s) — set the variable before deploying:\n\n"
        f"{detail}\n\n"
        f"Tip: re-scaffold with the missing flags, e.g.:\n"
        f"  apx scaffold --catalog <catalog> --schema <schema>"
    )


def load_spec(path: Path) -> "AgentConfig":
    """Load a YAML spec file and return a validated AgentConfig.

    Resolves $VAR / ${VAR} env var references in string values before
    validation. Raises FileNotFoundError if the file does not exist,
    SpecValidationError if required fields are missing or types are wrong.
    """
    import yaml
    from pydantic import ValidationError
    from ._models import AgentConfig

    if not path.exists():
        raise FileNotFoundError(f"Spec file not found: {path}")

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise SpecValidationError(f"Expected a YAML mapping at the top level, got {type(raw).__name__}")

    data = _resolve_env_vars(raw)
    _check_unresolved_vars(data)

    if "name" not in data:
        raise SpecValidationError("Spec is missing required field: 'name'")

    # 'tools' in the YAML is a list of tool dicts — not part of AgentConfig
    # (those go to [[tool.apx.tools]]). Strip it so pydantic doesn't choke.
    data.pop("tools", None)

    try:
        return AgentConfig.model_validate(data)
    except ValidationError as e:
        raise SpecValidationError(f"Invalid spec: {e}") from e
