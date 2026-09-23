"""Governance data providers.

The default provider (GovernanceProvider) reads from Delta tables written
by the Governance scanner. Custom providers can implement the
GovernanceProvider protocol for alternative backends.
"""

from ontos_governance.providers.governance import GovernanceProvider

__all__ = ["GovernanceProvider"]
