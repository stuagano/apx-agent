#!/usr/bin/env bash
# Deploy agent-hub to Databricks Apps.
# Usage: ./deploy.sh --app <app-name> --workspace-path <path> [--profile <profile>]
#
# Example:
#   ./deploy.sh \
#     --app my-agent-hub \
#     --workspace-path /Workspace/Users/me@example.com/agent-hub-build
set -euo pipefail

APP_NAME=""
WORKSPACE_PATH=""
PROFILE="DEFAULT"

while [[ $# -gt 0 ]]; do
  case $1 in
    --app)            APP_NAME="$2";        shift 2 ;;
    --workspace-path) WORKSPACE_PATH="$2";  shift 2 ;;
    --profile)        PROFILE="$2";         shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$APP_NAME" || -z "$WORKSPACE_PATH" ]]; then
  echo "Usage: ./deploy.sh --app <app-name> --workspace-path <path> [--profile <profile>]"
  exit 1
fi

echo "==> Building frontend"
npm run build

echo "==> Building Python wheel"
rm -rf .build/
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
echo "    $(cat .build/requirements.txt)"

echo "==> Uploading to workspace ($WORKSPACE_PATH)"
databricks workspace import-dir .build "$WORKSPACE_PATH" \
  --profile "$PROFILE" --overwrite

echo "==> Deploying $APP_NAME"
databricks apps deploy "$APP_NAME" \
  --source-code-path "$WORKSPACE_PATH" \
  --profile "$PROFILE"

echo "==> Verifying"
databricks apps get "$APP_NAME" --profile "$PROFILE" -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
state = d.get('app_status', {}).get('state', 'unknown')
url = d.get('url', '')
ts = d.get('active_deployment', {}).get('update_time', '')
print(f'State: {state}')
print(f'Time:  {ts}')
print(f'URL:   {url}')
if state != 'RUNNING':
    sys.exit(1)
"
