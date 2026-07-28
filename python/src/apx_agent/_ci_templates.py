"""CI templates for ``apx scaffold --target apps``.

Borrowed from agent-foundry's three-stage branch flow (issue-shaped):

* PR → ``main``: unit tests only (no workspace credentials).
* PR → ``release``: unit tests + deploy to ``staging`` bundle target.
* Push to ``release``: gated deploy to ``prod`` (GitHub Environment /
  GitLab manual job).

``dev`` stays laptop-only; CI never touches it.

Auth contract (OAuth service-principal M2M), per target:

* ``DATABRICKS_HOST_<TARGET>``
* ``DATABRICKS_CLIENT_ID_<TARGET>``
* ``DATABRICKS_CLIENT_SECRET_<TARGET>``

Optional ``FRAMEWORK_REPO_TOKEN`` — only needed when the project pins
``apx-agent`` from a *private* git URL (public ``stuagano/apx-agent``
needs no token).

Substitution uses ``__APP_NAME__`` + ``str.replace`` — not ``.format()`` —
so GitHub's ``${{ }}`` expressions survive intact.
"""

from __future__ import annotations

from typing import Literal

CiProvider = Literal["github", "gitlab"]

# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------

_GH_PR_TO_MAIN = """\
name: pr-to-main

# Unit tests on every PR targeting ``main``.
# No workspace credentials needed.

on:
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install 3.11

      - name: Optional auth for private apx-agent pin
        env:
          FRAMEWORK_REPO_TOKEN: ${{ secrets.FRAMEWORK_REPO_TOKEN }}
        run: |
          if [ -n "$FRAMEWORK_REPO_TOKEN" ]; then
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "git@github.com:"
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "ssh://git@github.com/"
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "https://github.com/"
          fi

      - name: Install project
        run: uv sync --group dev

      - name: Unit tests
        run: uv run pytest -q
"""

_GH_PR_TO_RELEASE = """\
name: pr-to-release

# Validate + deploy to the ``staging`` bundle target on every PR into ``release``.
# Requires staging OAuth secrets (see docs/deploy-cicd.md).

on:
  pull_request:
    branches: [release]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  validate-and-deploy-staging:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    env:
      DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST_STAGING }}
      DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID_STAGING }}
      DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET_STAGING }}
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install 3.11

      - name: Optional auth for private apx-agent pin
        env:
          FRAMEWORK_REPO_TOKEN: ${{ secrets.FRAMEWORK_REPO_TOKEN }}
        run: |
          if [ -n "$FRAMEWORK_REPO_TOKEN" ]; then
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "git@github.com:"
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "ssh://git@github.com/"
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "https://github.com/"
          fi

      - name: Install project
        run: uv sync --group dev

      - name: Unit tests
        run: uv run pytest -q

      - name: Deploy to staging
        run: uv run apx deploy --target apps --bundle-target staging --no-run
"""

_GH_RELEASE_DEPLOY_PROD = """\
name: release-deploy-prod

# Deploy to prod on merge to ``release``, gated by the ``prod`` GitHub
# Environment (Settings → Environments → required reviewers).

on:
  push:
    branches: [release]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  deploy-prod:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    environment:
      name: prod
    env:
      DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST_PROD }}
      DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID_PROD }}
      DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET_PROD }}
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install 3.11

      - name: Optional auth for private apx-agent pin
        env:
          FRAMEWORK_REPO_TOKEN: ${{ secrets.FRAMEWORK_REPO_TOKEN }}
        run: |
          if [ -n "$FRAMEWORK_REPO_TOKEN" ]; then
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "git@github.com:"
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "ssh://git@github.com/"
            git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "https://github.com/"
          fi

      - name: Install project
        run: uv sync --group dev

      - name: Deploy to prod
        run: uv run apx deploy --target apps --bundle-target prod
"""

# ---------------------------------------------------------------------------
# GitLab CI
# ---------------------------------------------------------------------------

_GITLAB_CI = """\
# GitLab CI for __APP_NAME__, generated by ``apx scaffold --ci gitlab``.
#
# Three-stage flow:
#
#   * MR → main:     unit tests only
#   * MR → release:  unit tests + deploy: staging
#   * Push release:  deploy: prod (manual gate)
#
# CI variables (Settings → CI/CD → Variables, masked):
#
#   * DATABRICKS_HOST_STAGING / DATABRICKS_CLIENT_ID_STAGING / DATABRICKS_CLIENT_SECRET_STAGING
#   * DATABRICKS_HOST_PROD    / DATABRICKS_CLIENT_ID_PROD    / DATABRICKS_CLIENT_SECRET_PROD
#   * FRAMEWORK_REPO_TOKEN (optional — private apx-agent pin only)

default:
  image: ghcr.io/astral-sh/uv:python3.11-bookworm-slim
  before_script:
    - |
      if [ -n "${FRAMEWORK_REPO_TOKEN:-}" ]; then
        git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "git@github.com:"
        git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "ssh://git@github.com/"
        git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "https://github.com/"
      fi
    - uv sync --group dev

stages:
  - test
  - deploy

unit-tests:
  stage: test
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "main"'
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "release"'
  script:
    - uv run pytest -q

deploy-staging:
  stage: deploy
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "release"'
  variables:
    DATABRICKS_HOST: $DATABRICKS_HOST_STAGING
    DATABRICKS_CLIENT_ID: $DATABRICKS_CLIENT_ID_STAGING
    DATABRICKS_CLIENT_SECRET: $DATABRICKS_CLIENT_SECRET_STAGING
  script:
    - uv run apx deploy --target apps --bundle-target staging --no-run

deploy-prod:
  stage: deploy
  rules:
    - if: '$CI_COMMIT_BRANCH == "release"'
      when: manual
  variables:
    DATABRICKS_HOST: $DATABRICKS_HOST_PROD
    DATABRICKS_CLIENT_ID: $DATABRICKS_CLIENT_ID_PROD
    DATABRICKS_CLIENT_SECRET: $DATABRICKS_CLIENT_SECRET_PROD
  script:
    - uv run apx deploy --target apps --bundle-target prod
"""


def render_github_workflows(app_name: str) -> dict[str, str]:
    """Return ``{filename: contents}`` for the three GitHub Actions workflows.

    Keys are filenames relative to ``.github/workflows/``.
    """
    _ = app_name  # reserved for future app-specific steps
    return {
        "pr-to-main.yml": _GH_PR_TO_MAIN,
        "pr-to-release.yml": _GH_PR_TO_RELEASE,
        "release-deploy-prod.yml": _GH_RELEASE_DEPLOY_PROD,
    }


def render_gitlab_ci(app_name: str) -> str:
    """Return the contents of ``.gitlab-ci.yml``."""
    return _GITLAB_CI.replace("__APP_NAME__", app_name)


def render_ci_files(app_name: str, provider: CiProvider) -> dict[str, str]:
    """Return ``{relative_path: contents}`` for the chosen CI provider."""
    if provider == "github":
        return {
            f".github/workflows/{name}": body
            for name, body in render_github_workflows(app_name).items()
        }
    if provider == "gitlab":
        return {".gitlab-ci.yml": render_gitlab_ci(app_name)}
    raise ValueError(f"unknown CI provider: {provider!r}")


__all__ = [
    "CiProvider",
    "render_ci_files",
    "render_github_workflows",
    "render_gitlab_ci",
]
