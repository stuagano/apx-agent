import os

import mlflow

from .core import create_app
from .router import router
from . import agent_router as agent_router  # noqa: F401 — registers Agent instance before create_app

# Point MLflow traces at the configured experiment (set via MLFLOW_EXPERIMENT_NAME env var).
_experiment = os.environ.get("MLFLOW_EXPERIMENT_NAME")
if _experiment:
    mlflow.set_experiment(_experiment)

app = create_app(routers=[router])
