#!/bin/bash
# Build deployable directories for each Voynich agent.
# Each directory is self-contained and can be deployed with:
#   databricks apps deploy <app-name> --source-code-path /Workspace/... --profile fe-stable

set -e
cd "$(dirname "$0")/.."

DEPLOY_DIR="deploy"
AGENTS="decipherer historian critic judge orchestrator grounder"
HOST="https://fevm-serverless-stable-qh44kx.cloud.databricks.com"
APP_DOMAIN="7474652869938903.aws.databricksapps.com"
WAREHOUSE_ID="76cf70399b8d0ef0"
POP_TABLE="serverless_stable_qh44kx_catalog.voynich.population"
WF_PREFIX="serverless_stable_qh44kx_catalog.voynich.workflow"

# M2M OAuth — each app's service principal credentials for FMAPI and agent-to-agent calls.
# Read from env vars so secrets don't end up in git.
#   export SP_SECRET_orchestrator=... SP_SECRET_decipherer=... etc.
sp_client_id() {
  case "$1" in
    orchestrator) echo "ac3824be-60f3-41b8-8a8d-0393aa4f34ca" ;;
    decipherer)   echo "711b3648-e130-4093-81a8-07abf8785597" ;;
    historian)    echo "28ccb756-2b85-42da-b25b-117f85e8be1c" ;;
    critic)       echo "af6b9c2d-2b9c-458d-91ca-0894d1bc2253" ;;
    judge)        echo "f6dc4e47-c18b-4999-b2d7-d9cc59a6b080" ;;
    grounder)     echo "PLACEHOLDER_GROUNDER_CLIENT_ID" ;;
  esac
}
for A in $AGENTS; do
  VAR="SP_SECRET_${A}"
  eval VAL=\$$VAR
  if [ -z "$VAL" ]; then
    echo "ERROR: $VAR not set. Each agent needs its SP secret." >&2
    exit 1
  fi
done

# Build the framework
echo "Building appkit-agent..."
npm run build

rm -rf "$DEPLOY_DIR"

for AGENT in $AGENTS; do
  DIR="$DEPLOY_DIR/voynich-$AGENT"
  mkdir -p "$DIR/appkit-agent"

  # Copy the built framework (bundle only, no nested package.json —
  # all deps are declared in the deploy package.json)
  cp dist/index.mjs "$DIR/appkit-agent/index.mjs"
  cp dist/index.d.mts "$DIR/appkit-agent/index.d.mts"

  # Copy agent source
  cp "examples/voynich/$AGENT/app.ts" "$DIR/app.ts"
  cp "examples/voynich/voynich-config.ts" "$DIR/voynich-config.ts"

  # Create package.json — deps must match what the bundle actually imports
  cat > "$DIR/package.json" <<EOF
{
  "name": "voynich-$AGENT",
  "private": true,
  "type": "module",
  "scripts": { "start": "tsx app.ts" },
  "dependencies": {
    "express": "^4.21.0",
    "zod": "^4.0.0",
    "zod-to-json-schema": "^3.25.0",
    "tsx": "^4.20.0",
    "typescript": "~5.9.0"
  }
}
EOF

  # Create app.yaml
  cat > "$DIR/app.yaml" <<EOF
command:
  - npx
  - tsx
  - app.ts

env:
  - name: PORT
    value: "8000"
  - name: DATABRICKS_HOST
    value: "$HOST"
  - name: DATABRICKS_WAREHOUSE_ID
    value: "$WAREHOUSE_ID"
  - name: POPULATION_TABLE
    value: "$POP_TABLE"
  - name: DATABRICKS_CLIENT_ID
    value: "$(sp_client_id $AGENT)"
  - name: DATABRICKS_CLIENT_SECRET
    value: "$(eval echo \$SP_SECRET_${AGENT})"
EOF

  # Orchestrator needs extra env vars
  if [ "$AGENT" = "orchestrator" ]; then
    cat >> "$DIR/app.yaml" <<EOF
  - name: MUTATION_AGENT_URL
    value: "https://voynich-decipherer-$APP_DOMAIN"
  - name: FITNESS_AGENT_URLS
    value: "https://voynich-historian-$APP_DOMAIN,https://voynich-critic-$APP_DOMAIN,https://voynich-grounder-$APP_DOMAIN"
  - name: JUDGE_AGENT_URL
    value: "https://voynich-judge-$APP_DOMAIN"
  - name: WORKFLOW_TABLE_PREFIX
    value: "$WF_PREFIX"
  - name: POPULATION_SIZE
    value: "50"
  - name: MUTATION_BATCH
    value: "20"
  - name: MAX_GENERATIONS
    value: "500"
EOF
  fi

  # Grounder needs VISION_TABLE
  if [ "$AGENT" = "grounder" ]; then
    cat >> "$DIR/app.yaml" <<EOF
  - name: VISION_TABLE
    value: "serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis"
EOF
  fi

  # Fix import paths: ../../../src/index.js → ./appkit-agent/index.mjs
  sed -i '' "s|'../../../src/index.js'|'./appkit-agent/index.mjs'|g" "$DIR/app.ts"
  # Fix voynich-config import
  sed -i '' "s|'../voynich-config.js'|'./voynich-config.ts'|g" "$DIR/app.ts"

  echo "  Built: $DIR"
done

echo "Done. Deploy dirs in $DEPLOY_DIR/"
