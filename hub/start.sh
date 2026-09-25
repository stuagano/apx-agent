#!/bin/bash
# Databricks Apps compute has no access to the internal PyPI proxy, and
# apx-agent is a local path dependency (not published to any index we can reach
# from the app). So we bundle both wheels into the deploy source and install
# them here — apx-agent first, then the hub. Their remaining transitive deps
# (langgraph, databricks-sdk, fastapi, …) are all public and resolve from the
# platform's default index. Mirrors the working apx Apps recipe.
set -euo pipefail

pip install ./apx_agent-*.whl --quiet
pip install ./agent_hub-*.whl --quiet

exec uvicorn agent_hub.backend.app:app \
    --host 0.0.0.0 \
    --port "${DATABRICKS_APP_PORT:-8080}" \
    --workers 2
