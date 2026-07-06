"""quickstart — one-shot setup for the hubspot-complaints-agent Apps deploy.

Creates the MLflow experiment for tracing and writes its ID to .env.
Provisions memory backends (Delta tables or Lakebase instance) if configured.
Safe to re-run; idempotent.
"""
from apx_agent.bootstrap import init_apps_experiment, provision_memory_backends


def main() -> None:
    path, exp_id = init_apps_experiment()
    print(f"MLflow experiment: {path} (id={exp_id})")

    for line in provision_memory_backends():
        print(line)


if __name__ == "__main__":
    main()
