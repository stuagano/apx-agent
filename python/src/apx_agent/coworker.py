"""Coworker — a pre-grounded DataAgent that remembers (facts + session).

``CoworkerAgent`` is a ``DataAgent`` subclass that adds an optional persona and a
single ``memory`` knob; ``CoworkerTemplate`` wraps it for template-as-config.
Memory is carried as *declared config* (``memory_config`` / ``session_config``)
and wired by the framework's finalize/serve path with the app workspace client —
so construction needs no ``ws`` (same property as DataAgent grounding).
"""

from __future__ import annotations

from ._models import MemoryBackendConfig, SessionBackendConfig

# Bare-knob rungs → backend StoreType. ``lakebase`` is intentionally absent: it
# needs connection details the one-word knob can't express (see normalize).
_KNOB_TO_TYPE: dict[str, str] = {
    "off": "",            # sentinel: disabled
    "inmemory": "inmemory",
    "local": "inmemory",
    "persistent": "delta",
    "delta": "delta",
}


def normalize_memory_knob(
    value: str,
) -> "tuple[MemoryBackendConfig | None, SessionBackendConfig | None]":
    """Map the coworker ``memory`` knob to ``(MemoryBackendConfig,
    SessionBackendConfig)`` for the facts + session subsystems (same tier).

    Returns ``(None, None)`` for ``"off"``. Raises ``ValueError`` for
    ``"lakebase"`` (needs an explicit ``[tool.apx.agent.memory]`` block) and for
    any unknown value.
    """
    v = (value or "").strip().lower()
    if v == "lakebase":
        raise ValueError(
            "memory='lakebase' needs connection details the one-word knob can't "
            "carry — add explicit [tool.apx.agent.memory] and "
            "[tool.apx.agent.session] blocks with type='lakebase' "
            "(host, database, embedding_model, embedding_dim)."
        )
    if v not in _KNOB_TO_TYPE:
        raise ValueError(
            f"memory={value!r} is not a valid tier; use one of: off, inmemory "
            "(alias local), persistent (alias delta), or an explicit "
            "[tool.apx.agent.memory] block for lakebase."
        )
    tier = _KNOB_TO_TYPE[v]
    if not tier:  # "off"
        return (None, None)
    return (MemoryBackendConfig(type=tier), SessionBackendConfig(type=tier))
