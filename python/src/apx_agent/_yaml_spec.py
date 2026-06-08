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

    if "name" not in data:
        raise SpecValidationError("Spec is missing required field: 'name'")

    # 'tools' in the YAML is a list of tool dicts — not part of AgentConfig
    # (those go to [[tool.apx.tools]]). Strip it so pydantic doesn't choke.
    data.pop("tools", None)

    try:
        return AgentConfig.model_validate(data)
    except ValidationError as e:
        raise SpecValidationError(f"Invalid spec: {e}") from e
