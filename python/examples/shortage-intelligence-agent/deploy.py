"""Canonical deploy script for shortage-intelligence.

Demonstrates the standard apx-agent deploy flow:

  1. log_agent(agent, model=..., registered_model_name=...,
               experiment=...) — logs the compiled ChatAgent + auto-
       derived MLflow resources, registers the model version in UC.
  2. databricks.agents.deploy(registered_model_name, model_version=...)
       — promotes the registered version to a Model Serving endpoint.
  3. set_uc_tags_for_agent(...) — writes apx.agent.* UC tags so the
       agent shows up in `apx list`, `apx topology`, and watchdog's
       crawler.
  4. publish_tools_to_uc(agent) — registers any @tool(uc=...)
       decorated tools (currently: classify_shortage_severity) into
       UC so Genie / Managed MCP can reach them too.

Run::

    cd python/examples/shortage-intelligence-agent
    uv sync
    python deploy.py

Override defaults via environment::

    REGISTERED_MODEL_NAME=main.agents.shortage_intel \
    SERVING_MODEL_ENDPOINT=databricks-claude-opus-4-7 \
    APX_EXPERIMENT=/Users/me@company.com/agents/shortage_intel \
        python deploy.py

Use `--no-deploy` (or set ``APX_NO_DEPLOY=1``) to only log + register
without promoting to a serving endpoint — useful for first-time
testing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger("shortage-intelligence.deploy")


def _get_env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Environment variable {name} is required.")
    return value


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-deploy",
        action="store_true",
        default=os.environ.get("APX_NO_DEPLOY") == "1",
        help="Log + register only — skip databricks.agents.deploy.",
    )
    parser.add_argument(
        "--no-publish-tools",
        action="store_true",
        help="Skip publish_tools_to_uc.",
    )
    args = parser.parse_args()

    registered_name = _get_env(
        "REGISTERED_MODEL_NAME",
        default="main.agents.shortage_intelligence",
    )
    model_endpoint = _get_env(
        "SERVING_MODEL_ENDPOINT",
        default="databricks-claude-sonnet-4-6",
    )
    experiment = _get_env("APX_EXPERIMENT", default=None)
    assert registered_name and model_endpoint  # narrowed by defaults

    # Import lazily so this script works even when apx_agent isn't on
    # PYTHONPATH yet (the user error gets a clearer message).
    try:
        import mlflow
        from apx_agent import (
            log_agent,
            publish_tools_to_uc,
            set_uc_tags_for_agent,
        )
    except ImportError as e:
        raise SystemExit(
            f"Could not import apx_agent: {e}. Run `uv sync` in this directory first."
        ) from e

    from shortage_intelligence_agent.backend.agent_router import agent

    if not args.no_publish_tools:
        logger.info("Publishing @tool(uc=...) decorated tools to UC...")
        try:
            results = publish_tools_to_uc(agent)
            for r in results:
                grants = ", ".join(r.grants_applied) or "none"
                logger.info("  published %s (grants: %s)", r.uc_name, grants)
            if not results:
                logger.info("  (no UC-syncable tools on this agent)")
        except Exception:
            logger.exception("publish_tools_to_uc failed — continuing with deploy.")

    logger.info("Logging agent to MLflow + registering in UC...")
    with mlflow.start_run():
        info = log_agent(
            agent,
            model=model_endpoint,
            registered_model_name=registered_name,
            experiment=experiment,
        )
    logger.info(
        "Registered %s version %s",
        registered_name, getattr(info, "registered_model_version", "?"),
    )

    if not args.no_deploy:
        try:
            from databricks import agents
        except ImportError as e:
            raise SystemExit(
                "databricks-agents is required for deployment. "
                "Install with: pip install databricks-agents"
            ) from e
        logger.info("Deploying to Model Serving...")
        agents.deploy(
            registered_name,
            model_version=info.registered_model_version,
        )
        logger.info("Deployed %s as a Model Serving endpoint.", registered_name)
    else:
        logger.info("Skipping databricks.agents.deploy (--no-deploy).")

    logger.info("Writing apx.agent.* UC tags...")
    set_uc_tags_for_agent(
        agent,
        registered_model_name=registered_name,
        model=model_endpoint,
        name="shortage_intelligence",
    )
    logger.info("Done.")


if __name__ == "__main__":
    main()
