"""Serve the browser and business routes on native DurableAgentServer."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from apx_agent._serve import create_app

app = create_app()

client = Path.cwd() / "client" / "dist"
if client.is_dir():
    app.mount("/", StaticFiles(directory=client, html=True), name="client")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["DATABRICKS_APP_PORT"]))
