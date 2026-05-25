"""quickstart — one-shot setup for the hello-world Apps deploy.

Creates the MLflow experiment for tracing and writes its ID to .env.
Safe to re-run; idempotent.
"""
from apx_agent.bootstrap import init_apps_experiment


def main() -> None:
    path, exp_id = init_apps_experiment()
    print(f"MLflow experiment: {path} (id={exp_id})")


if __name__ == "__main__":
    main()
