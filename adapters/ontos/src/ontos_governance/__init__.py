"""Governance — pluggable governance UI module for Ontos.

Quick start (Ontos integration):

    from ontos_governance import register_routes
    register_routes(app)   # mounts at /api/governance/*

Quick start (standalone):

    uvicorn ontos_governance.app:app --reload
"""

from ontos_governance.provider import GovernanceProvider
from ontos_governance.router import register_routes

__all__ = ["GovernanceProvider", "register_routes"]
