"""Standalone governance API — for development and testing.

Run with:

    cd adapters/ontos
    uvicorn ontos_governance.app:app --reload

Requires environment variables for GovernanceProvider (see README.md).
For local development without a Databricks connection, the API will
start but endpoints will return 500s until a provider is available.
"""

from fastapi import FastAPI

from ontos_governance.router import register_routes

app = FastAPI(
    title="Governance API",
    description="Pluggable governance UI module — standalone mode",
    version="0.1.0",
)

register_routes(app)
