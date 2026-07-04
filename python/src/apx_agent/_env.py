"""``$VAR`` / ``${VAR}`` environment-reference expansion.

One helper shared by every config surface that accepts env references
(``sub_agents`` URLs, registry/self URLs, tool config values, resource
declarations, dev-UI probes). Previously each caller carried its own copy of
the same three lines — issue #436's rewire consolidated them here.
"""

from __future__ import annotations

import os

_UNSET_ENV = ""  # unset $VAR references resolve to empty so callers can warn-and-skip


def resolve_env_var(value: str) -> str:
    """Resolve a ``$VAR`` or ``${VAR}`` reference to its environment value.

    A value that doesn't start with ``$`` passes through unchanged. A ``$VAR``
    reference whose variable is unset resolves to ``""`` — callers decide
    whether that means warn, skip, or fail.
    """
    if not value.startswith("$"):
        return value
    var_name = value.lstrip("$").strip("{}")
    return os.environ.get(var_name, _UNSET_ENV)
