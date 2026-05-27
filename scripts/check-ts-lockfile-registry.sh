#!/usr/bin/env bash
# Fail if typescript/package-lock.json pins packages to an internal Databricks
# npm registry (e.g. npm-proxy.dev.databricks.com). Those hosts are reachable
# only on the Databricks network, so a poisoned lockfile makes GitHub Actions
# `npm ci` hang ~8 min and die ("Exit handler never called!"). Keep resolved
# hosts on registry.npmjs.org — npm's replace-registry-host swaps to the proxy
# automatically for local installs. See PR #85.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/typescript/package-lock.json"

if grep -nE '"resolved":[[:space:]]*"https://[^"]*databricks\.com' "$LOCK"; then
  cat >&2 <<'MSG'

ERROR: typescript/package-lock.json points at an internal Databricks npm registry.
GitHub Actions cannot reach it, so `npm ci` will hang. Fix it with:

  sed -i '' 's|https://npm-proxy.dev.databricks.com/|https://registry.npmjs.org/|g' typescript/package-lock.json
  (cd typescript && rm -rf node_modules && npm ci)   # verify it still installs

MSG
  exit 1
fi

echo "OK: typescript/package-lock.json resolved hosts are clean (public npm only)."
