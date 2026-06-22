"""A dict-like view over the in-graph keyed state that records writes.

The view is handed to tools as ``Dependencies.State``. Reads pass through to
the injected state dict; writes are recorded so the tool wrapper can emit them
as a LangGraph ``Command`` state delta after the tool returns. See
docs/design/keyed-state-tool-access.md (G3 increment 2).
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any


class StateProxy(MutableMapping[str, Any]):
    """Read-through, write-tracking view of the keyed ``state`` dict.

    ``delta`` is the set of keys this proxy wrote, with their latest values —
    that is what becomes the ``Command`` state update. In-place mutation of a
    value read out of the proxy (e.g. ``proxy["xs"].append(1)``) is NOT tracked;
    reassign the key to record a change.
    """

    def __init__(self, source: dict[str, Any] | None) -> None:
        self._source: dict[str, Any] = source if source is not None else {}
        self._writes: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key in self._writes:
            return self._writes[key]
        return self._source[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._writes[key] = value

    def __delitem__(self, key: str) -> None:
        # Deletion is recorded as a write of None (the reducer can't remove a
        # key; this is the closest in-graph semantic). Documented limitation.
        if key not in self._source and key not in self._writes:
            raise KeyError(key)
        self._writes[key] = None

    def __iter__(self) -> Iterator[str]:
        return iter({**self._source, **self._writes})

    def __len__(self) -> int:
        return len({**self._source, **self._writes})

    @property
    def dirty(self) -> bool:
        return bool(self._writes)

    @property
    def delta(self) -> dict[str, Any]:
        return dict(self._writes)
