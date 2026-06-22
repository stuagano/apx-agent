"""Sentinel marking a tool parameter as the in-graph keyed-state view.

Lives in its own leaf module so both _defaults (the Dependencies.State alias)
and _inspection (detection) can import it without a cycle.
"""


class _StateDep:
    """Marker object; identity-compared in _is_state_dependency."""


_STATE_DEP = _StateDep()
