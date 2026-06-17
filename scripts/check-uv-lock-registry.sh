#!/usr/bin/env bash
# Fail (or, with --fix, rewrite) if any tracked uv.lock pins packages to
# Databricks' internal PyPI proxy (pypi-proxy.dev.databricks.com). That index
# is unreachable for external users and for deployed Apps/serving endpoints, so
# committed + shipped locks must resolve from public PyPI. uv re-records the
# proxy whenever `uv sync` runs with UV_INDEX_URL pointed at it — hence the
# pre-commit auto-fix + this CI guard. The package download URLs are already
# public (files.pythonhosted.org); only the recorded index changes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

# Match on the host (not the full URL) so the detector and the fixer cover the
# same surface: a scheme (http vs https), trailing-slash, or sub-path variant of
# the proxy host must both trip CI red AND be rewritten by --fix. The package
# download URLs are on files.pythonhosted.org, so host replacement never touches
# them.
PROXY_HOST="pypi-proxy.dev.databricks.com"
PUBLIC_HOST="pypi.org"

bad=""
for f in $(git ls-files '*uv.lock'); do
  if grep -q "$PROXY_HOST" "$f" 2>/dev/null; then
    if [ "$FIX" = "1" ]; then
      perl -i -pe "s{\Q${PROXY_HOST}\E}{${PUBLIC_HOST}}g" "$f"
      echo "fixed: $f"
    else
      bad="$bad $f"
    fi
  fi
done

if [ "$FIX" = "0" ] && [ -n "$bad" ]; then
  {
    echo ""
    echo "ERROR: uv.lock files pin packages to the internal Databricks PyPI proxy"
    echo "(pypi-proxy.dev.databricks.com) — unreachable for external users and"
    echo "deployed apps. Affected:"
    for f in $bad; do echo "  - $f"; done
    echo ""
    echo "Fix:  scripts/check-uv-lock-registry.sh --fix"
  } >&2
  exit 1
fi

echo "OK: tracked uv.lock files resolve from public PyPI."
