#!/bin/bash
# Preflight check for Databricks Builder App deployment.
# Verifies local tools and workspace requirements before you run deploy.sh.
#
# Usage:
#   ./scripts/preflight.sh                              # auto-detects your profile
#   ./scripts/preflight.sh --profile my-workspace      # or specify one explicitly

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

PROFILE=""
APP_NAME="databricks-builder"
PASS=0
WARN=0
FAIL=0
SKIP_LAKEBASE=false
DEPLOY_FLAGS=""

usage() {
  echo "Usage: $0 [--profile my-workspace] [--app-name my-builder-app]"
  echo ""
  echo "Checks that your environment and workspace are ready for deploy.sh."
  echo "If --profile is omitted, picks from your authenticated profiles automatically."
  echo ""
  echo "Examples:"
  echo "  $0                                   # auto-detect profile"
  echo "  $0 --profile my-workspace            # use a specific profile"
  echo "  $0 --profile my-workspace --app-name my-builder-app"
  echo ""
  echo "Options:"
  echo "  --profile PROFILE    Databricks CLI profile (optional — auto-detected if omitted)"
  echo "  --app-name NAME      App name for the suggested deploy command (default: databricks-builder)"
  echo "  -h, --help           Show this help"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --profile) PROFILE="$2"; shift 2 ;;
    --app-name) APP_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo -e "${RED}Unknown option: $1${NC}"; usage; exit 1 ;;
  esac
done

if [ -z "$PROFILE" ]; then
  # Try to auto-detect from valid configured profiles
  if ! command -v databricks &>/dev/null; then
    echo -e "${RED}Error: --profile is required and Databricks CLI is not installed${NC}"
    echo ""
    echo -e "  Install: curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh"
    exit 1
  fi

  # Parse profiles, collect only Valid=YES ones (skip header line)
  VALID_PROFILES=()
  while IFS= read -r line; do
    name=$(echo "$line" | awk '{print $1}')
    valid=$(echo "$line" | awk '{print $NF}')
    if [ "$valid" = "YES" ] && [ -n "$name" ]; then
      VALID_PROFILES+=("$name")
    fi
  done < <(databricks auth profiles 2>/dev/null | tail -n +2)

  if [ "${#VALID_PROFILES[@]}" -eq 0 ]; then
    echo -e "${RED}No authenticated Databricks profiles found.${NC}"
    echo ""
    echo -e "  Run this to authenticate, then re-run preflight:"
    echo -e "  ${BLUE}databricks auth login --host https://YOUR-WORKSPACE.cloud.databricks.com --profile my-workspace${NC}"
    exit 1
  elif [ "${#VALID_PROFILES[@]}" -eq 1 ]; then
    PROFILE="${VALID_PROFILES[0]}"
    echo -e "  ${BLUE}→${NC}  Using profile: ${BOLD}${PROFILE}${NC} (only authenticated profile found)"
    echo ""
  else
    echo -e "${BOLD}Select a Databricks profile:${NC}"
    echo ""
    for i in "${!VALID_PROFILES[@]}"; do
      echo -e "  $((i+1))) ${VALID_PROFILES[$i]}"
    done
    echo ""
    while true; do
      read -rp "  Enter number [1-${#VALID_PROFILES[@]}]: " choice
      if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#VALID_PROFILES[@]}" ]; then
        PROFILE="${VALID_PROFILES[$((choice-1))]}"
        echo ""
        break
      fi
      echo -e "  ${RED}Invalid choice. Enter a number between 1 and ${#VALID_PROFILES[@]}.${NC}"
    done
  fi
fi

MIN_CLI_VERSION="0.287.0"
MIN_NODE_VERSION="18"

# ─── helpers ──────────────────────────────────────────────────────────────────

pass()  { echo -e "  ${GREEN}✓${NC}  $1"; PASS=$((PASS+1)); }
warn()  { echo -e "  ${YELLOW}⚠${NC}  $1"; WARN=$((WARN+1)); }
fail()  { echo -e "  ${RED}✗${NC}  $1"; FAIL=$((FAIL+1)); }
info()  { echo -e "  ${BLUE}→${NC}  $1"; }

version_gte() {
  # Returns 0 (true) if $1 >= $2
  printf '%s\n%s\n' "$2" "$1" | sort -V -C
}

# ─── banner ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║      Databricks Builder App — Preflight Check              ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Profile:  ${BOLD}${PROFILE}${NC}"
echo -e "  App name: ${BOLD}${APP_NAME}${NC}"
echo ""

# ─── 1. Local tools ───────────────────────────────────────────────────────────

echo -e "${BOLD}[1/3] Local tools${NC}"

# Databricks CLI
if ! command -v databricks &>/dev/null; then
  fail "Databricks CLI not found"
  info "Install: curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh"
else
  cli_version=$(databricks --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if version_gte "$cli_version" "$MIN_CLI_VERSION"; then
    pass "Databricks CLI v${cli_version}"
  else
    fail "Databricks CLI v${cli_version} (need v${MIN_CLI_VERSION}+)"
    info "Update: curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh"
  fi
fi

# uv
if ! command -v uv &>/dev/null; then
  fail "uv not found"
  info "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
else
  pass "uv $(uv --version 2>/dev/null | head -1)"
fi

# Node.js
if ! command -v node &>/dev/null; then
  fail "Node.js not found (need v${MIN_NODE_VERSION}+)"
  info "Install: https://nodejs.org/en/download  or  brew install node"
else
  node_version=$(node --version 2>/dev/null | grep -oE '[0-9]+' | head -1)
  if version_gte "$node_version" "$MIN_NODE_VERSION"; then
    pass "Node.js $(node --version)"
  else
    fail "Node.js $(node --version) (need v${MIN_NODE_VERSION}+)"
    info "Update: brew upgrade node  or  https://nodejs.org/en/download"
  fi
fi

# npm
if ! command -v npm &>/dev/null; then
  fail "npm not found"
  info "Comes with Node.js — reinstall Node"
else
  pass "npm $(npm --version 2>/dev/null)"
fi

echo ""

# ─── 2. Databricks auth ───────────────────────────────────────────────────────

echo -e "${BOLD}[2/3] Databricks authentication${NC}"

# Profile exists in config
if ! databricks auth env --profile "$PROFILE" &>/dev/null; then
  fail "Profile '${PROFILE}' not found or not authenticated"
  info "Run: databricks configure --profile ${PROFILE}"
  info "  or: databricks auth login --profile ${PROFILE}"
  echo ""
  echo -e "${RED}Cannot check workspace without valid auth. Fix authentication and re-run.${NC}"
  echo ""
  exit 1
fi

WORKSPACE_HOST=$(databricks auth env --profile "$PROFILE" 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip().rstrip(',')
    if 'DATABRICKS_HOST' in line:
        import re
        m = re.search(r'\"([^\"]+)\"', line.split(':', 1)[-1])
        if m: print(m.group(1)); break
" 2>/dev/null)

CURRENT_USER=$(databricks current-user me --profile "$PROFILE" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('userName', d.get('displayName', '')))
except: pass
" 2>/dev/null)

if [ -z "$CURRENT_USER" ]; then
  fail "Could not authenticate to workspace"
  info "Run: databricks auth login --profile ${PROFILE}"
  echo ""
  exit 1
fi

pass "Authenticated as ${CURRENT_USER}"
info "Workspace: ${WORKSPACE_HOST}"

echo ""

# ─── 3. Workspace requirements ────────────────────────────────────────────────

echo -e "${BOLD}[3/3] Workspace requirements${NC}"

CLI_ARGS="--profile ${PROFILE}"

# Apps enabled
APPS_EXIT=0
APPS_STDERR=$(databricks apps list $CLI_ARGS --output json 2>&1 >/dev/null) || APPS_EXIT=$?
if [ "$APPS_EXIT" -ne 0 ]; then
  if echo "$APPS_STDERR" | grep -qi "PERMISSION_DENIED\|not authorized\|CAN_USE\|permission"; then
    fail "You don't have permission to use Apps on this workspace"
    info "Ask your admin for 'Can Use' on Databricks Apps"
  elif echo "$APPS_STDERR" | grep -qi "not.*enabled\|not.*supported\|RESOURCE_DOES_NOT_EXIST\|FEATURE_DISABLED"; then
    fail "Databricks Apps not enabled on this workspace"
    info "Contact your workspace admin to enable Apps"
  else
    fail "Apps check failed: ${APPS_STDERR}"
  fi
else
  pass "Databricks Apps enabled"
fi

# Claude via FMAPI
ENDPOINTS=$(databricks serving-endpoints list $CLI_ARGS 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    endpoints = data if isinstance(data, list) else data.get('endpoints', [])
    claude = [e.get('name','') for e in endpoints if 'claude' in e.get('name','').lower()]
    print('\n'.join(claude[:3]))
except: pass
" 2>/dev/null)

if [ -n "$ENDPOINTS" ]; then
  BEST=$(echo "$ENDPOINTS" | grep -i "opus\|sonnet" | head -1 || echo "$ENDPOINTS" | head -1)
  pass "Claude available via FMAPI (${BEST})"
else
  fail "No Claude endpoints found on this workspace"
  info "The app routes Claude calls through Databricks FMAPI."
  info "Your workspace needs a 'databricks-claude-*' serving endpoint."
  info "Contact your admin or check: ${WORKSPACE_HOST}/ml/endpoints"
fi

# Workspace write permissions (for app upload)
WS_TEST=$(databricks workspace list "/Workspace/Users/${CURRENT_USER}" $CLI_ARGS --output json 2>&1)
if echo "$WS_TEST" | grep -qi "PERMISSION_DENIED\|403"; then
  fail "Cannot write to /Workspace/Users/${CURRENT_USER}"
  info "App code is uploaded there during deploy — you need write access to your user folder"
else
  pass "Workspace write access confirmed"
fi

# Lakebase Autoscale (optional)
LAKEBASE_CHECK=$(databricks bundle validate $CLI_ARGS 2>&1) || true
if echo "$LAKEBASE_CHECK" | grep -qi "postgres_projects.*not supported\|resource type.*postgres_projects"; then
  warn "Lakebase Autoscale not available — deploy will use SQLite instead"
  SKIP_LAKEBASE=true
else
  LAKEBASE_API=$(databricks api get /api/2.0/lakebase/autoscale/postgres/projects $CLI_ARGS 2>&1) || true
  if echo "$LAKEBASE_API" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    # Success: top-level has 'projects' key (even if empty list)
    if 'projects' in d:
        sys.exit(0)
    sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null; then
    pass "Lakebase Autoscale available"
  else
    warn "Lakebase Autoscale not available — deploy will use SQLite instead"
    SKIP_LAKEBASE=true
  fi
fi

echo ""

# ─── Summary ──────────────────────────────────────────────────────────────────

echo -e "${BOLD}─────────────────────────────────────────────────────────────${NC}"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}${BOLD}✗  Not ready — ${FAIL} issue(s) to fix${NC}"
  echo ""
  echo -e "  Fix the items marked ${RED}✗${NC} above, then re-run this script."
  echo ""
  exit 1
fi

# Build deploy command
if [ "$SKIP_LAKEBASE" = true ]; then
  DEPLOY_FLAGS="--skip-lakebase"
  LAKEBASE_NOTE="  (Using SQLite — no Lakebase provisioning needed)"
fi

echo -e "${GREEN}${BOLD}✓  Ready to deploy!${NC}"
echo ""

if [ "$WARN" -gt 0 ]; then
  echo -e "  ${YELLOW}${WARN} warning(s) — see above. Deployment will still succeed.${NC}"
  echo ""
fi

if [ -n "$LAKEBASE_NOTE" ]; then
  echo -e "${YELLOW}${LAKEBASE_NOTE}${NC}"
  echo ""
fi

echo -e "${BOLD}  Run this to deploy:${NC}"
echo ""
if [ -n "$DEPLOY_FLAGS" ]; then
  echo -e "  ${GREEN}./scripts/deploy.sh ${APP_NAME} --profile ${PROFILE} ${DEPLOY_FLAGS}${NC}"
else
  echo -e "  ${GREEN}./scripts/deploy.sh ${APP_NAME} --profile ${PROFILE}${NC}"
fi
echo ""
echo -e "  The app will be live in ~5 minutes at a URL like:"
echo -e "  https://${APP_NAME}-$(echo "$WORKSPACE_HOST" | grep -oE '[0-9]+' | tr -d '\n' | head -c 16 2>/dev/null).aws.databricksapps.com"
echo ""
