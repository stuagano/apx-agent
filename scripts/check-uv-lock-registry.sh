#!/usr/bin/env bash
# Fail (or, with --fix, rewrite) if any tracked uv.lock pins packages to a
# Databricks internal PyPI proxy (pypi-proxy.<env>.databricks.com — dev, cloud,
# …). That index is unreachable for external users and for deployed
# Apps/serving endpoints, so committed + shipped locks must resolve from public
# PyPI. uv re-records the proxy whenever `uv sync` runs with UV_INDEX_URL
# pointed at it — hence the pre-commit auto-fix + this CI guard. Some proxy
# variants (cloud) also serve the wheel files themselves, so both the index
# (/simple) and the package download URLs (/packages/) are rewritten. Kept in
# sync with _sanitize_uv_lock in cli.py (the deploy-time equivalent).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

# Any pypi-proxy env variant: a single subdomain label before .databricks.com.
PROXY_RE='pypi-proxy\.[A-Za-z0-9-]+\.databricks\.com'

bad=""
for f in $(git ls-files '*uv.lock'); do
  if grep -qE "$PROXY_RE" "$f" 2>/dev/null; then
    if [ "$FIX" = "1" ]; then
      # Two rules, matching _sanitize_uv_lock: index → pypi.org, package files →
      # files.pythonhosted.org (the proxy mirrors PyPI's /packages/ layout).
      perl -i -pe "s{https://${PROXY_RE}/simple}{https://pypi.org/simple}g; s{https://${PROXY_RE}/packages/}{https://files.pythonhosted.org/packages/}g" "$f"
      echo "fixed: $f"
    else
      bad="$bad $f"
    fi
  fi
done

if [ "$FIX" = "0" ] && [ -n "$bad" ]; then
  {
    echo ""
    echo "ERROR: uv.lock files pin packages to an internal Databricks PyPI proxy"
    echo "(pypi-proxy.<env>.databricks.com) — unreachable for external users and"
    echo "deployed apps. Affected:"
    for f in $bad; do echo "  - $f"; done
    echo ""
    echo "Fix:  scripts/check-uv-lock-registry.sh --fix"
  } >&2
  exit 1
fi

echo "OK: tracked uv.lock files resolve from public PyPI."
