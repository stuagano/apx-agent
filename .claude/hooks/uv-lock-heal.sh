#!/usr/bin/env bash
# PostToolUse hook: after a `uv` command runs, re-sanitize python/uv.lock so a
# `uv sync`/`uv lock` that re-recorded the internal pypi-proxy index never
# lingers as a dirty lockfile. Belt-and-suspenders to session-start.sh's
# --frozen sync: the source fix stops the churn, this heals it if anything
# still slips through (a manual `uv add`, `uv lock`, etc.).
#
# Reads the PostToolUse JSON on stdin. Cheap guard first: only act when the tool
# was Bash AND the command mentions `uv`. Fails OPEN (exit 0) always — a heal
# hook must never block a turn.
set -u

input=$(cat)

# Only Bash tool calls.
printf '%s' "$input" | grep -q '"tool_name"[[:space:]]*:[[:space:]]*"Bash"' || exit 0
# Only when the command actually invoked uv (word-boundary-ish: `uv ` or `uv\n`).
printf '%s' "$input" | grep -Eq '\buv\b' || exit 0

repo_root="${CLAUDE_PROJECT_DIR:-$PWD}"
fixer="$repo_root/scripts/check-uv-lock-registry.sh"
[ -x "$fixer" ] || exit 0

# Only bother if the lock is actually dirty with the proxy host.
lock="$repo_root/python/uv.lock"
[ -f "$lock" ] || exit 0
grep -q "pypi-proxy.dev.databricks.com" "$lock" 2>/dev/null || exit 0

"$fixer" --fix >/dev/null 2>&1 || true
exit 0
