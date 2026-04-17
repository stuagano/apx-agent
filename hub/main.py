"""Hub entry point — creates the FastAPI app for uvicorn."""

import logging

logging.basicConfig(level=logging.INFO)

from hub.app import create_hub_app

app = create_hub_app()
