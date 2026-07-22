"""quickstart — one-shot setup for the memory-demo Apps deploy.

Creates the MLflow experiment for tracing and writes its ID to .env.
Safe to re-run; idempotent.
"""
from pathlib import Path

from apx_agent.bootstrap import init_apps_experiment


def main() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    path, exp_id = init_apps_experiment(agent_name="memory-demo", env_path=str(env_path))
    print(f"MLflow experiment: {path} (id={exp_id})")


if __name__ == "__main__":
    main()
