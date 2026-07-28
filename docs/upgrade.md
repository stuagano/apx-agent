# Upgrade apx-agent in a scaffolded project

Scaffolded Apps projects pin `apx-agent` from git in `pyproject.toml`:

```toml
dependencies = [
  "apx-agent[langgraph] @ git+https://github.com/stuagano/apx-agent.git@<ref>#subdirectory=python",
]
```

`<ref>` is a commit SHA when the scaffold could resolve one (PEP 610),
otherwise a release tag like `v0.2.3`, otherwise `main`.

## Upgrade steps

1. Pick the ref you want — a tagged release from
   [Releases](https://github.com/stuagano/apx-agent/releases), or a commit SHA
   from `main`.
2. Edit the `@<ref>` in `pyproject.toml`.
3. Re-resolve and install:

   ```bash
   uv lock --upgrade-package apx-agent
   uv sync --group dev
   ```

4. Redeploy:

   ```bash
   uv run apx deploy --target apps
   ```

## Why `uv sync` is mandatory

`apx deploy --target apps` builds a wheel from the **running** `apx-agent`
install in your environment and stages it into `.build/`. Updating
`pyproject.toml` / `uv.lock` alone does not change what is installed in
`.venv`. Skipping `uv sync` silently ships the previous framework.

Prefer `uv run apx …` from the project root so the project pin wins over a
globally installed CLI.

## Pin mismatch (hard fail)

On deploy, apx compares the running VCS SHA (when available) against the
git ref in `pyproject.toml`. A **mismatch aborts the deploy** — typically a
global `apx` shadowing the project venv would otherwise ship the wrong
framework wheel into the App. Fix with `uv sync` + `uv run apx`.

Editable / local-path / PyPI-wheel installs skip the check (no commit SHA
to compare).
