#!/usr/bin/env bash
set -euo pipefail

# Deploy to Databricks Apps
#
# Usage:
#   ./deploy.sh              — build + deploy
#   ./deploy.sh build-only   — build only

PROFILE="${DATABRICKS_PROFILE:-fe-stable}"
APP_NAME="pipeline-agent"
BUNDLE_PATH="/Workspace/Users/$(databricks current-user me --profile "$PROFILE" -o json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('userName','unknown'))")/.bundle/$APP_NAME/dev/files"

echo "=== Building esbuild bundle ==="
npm run build:bundle
echo "Bundle: $(du -sh dist/app.cjs | cut -f1)"

if [[ "${1:-}" == "build-only" ]]; then
  echo "Build complete (skipping deploy)"
  exit 0
fi

echo ""
echo "=== Uploading ==="
databricks bundle deploy --target dev --profile "$PROFILE"

echo ""
echo "=== Deploying app ==="
databricks apps deploy "$APP_NAME" \
  --source-code-path "$BUNDLE_PATH" \
  --profile "$PROFILE" \
  -o json | python3 -c "
import sys,json
d = json.load(sys.stdin)
s = d.get('status',{})
print(f'Deploy: {s.get(\"state\",\"?\")} — {s.get(\"message\",\"\")}')
"

echo ""
echo "=== Verifying ==="
sleep 3
databricks apps get "$APP_NAME" --profile "$PROFILE" -o json | python3 -c "
import sys,json
d = json.load(sys.stdin)
print(f'Compute: {d.get(\"compute_status\",{}).get(\"state\",\"?\")}')
print(f'App: {d.get(\"app_status\",{}).get(\"state\",\"?\")}')
print(f'URL: {d.get(\"url\",\"?\")}')
"
