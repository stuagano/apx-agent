#!/bin/bash
# SessionStart hook for caps: injects the "lazy senior dev" posture as standing
# context. Like the Stop-gate wrapper, it cheaply short-circuits (no Python)
# when the project has no capabilities.yaml, and fails OPEN (exit 0) on any
# missing dependency so a broken hook never blocks a session from starting.
set -u
input=$(cat)

# Resolve the project cwd from the payload; fall back to the process cwd.
cwd=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
[ -z "$cwd" ] && cwd="$PWD"

# Walk up looking for capabilities.yaml.
found=""
dir="$cwd"
while :; do
  if [ -f "$dir/capabilities.yaml" ]; then found="$dir"; break; fi
  parent=$(dirname "$dir")
  [ "$parent" = "$dir" ] && break
  dir="$parent"
done
[ -z "$found" ] && exit 0   # no manifest -> nothing to inject

KIT="${CAPS_KIT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="${CAPS_GATE_PYTHON:-$KIT/.venv/bin/python}"
if [ ! -x "$PYTHON" ] && ! command -v "$PYTHON" >/dev/null 2>&1; then
  exit 0   # venv/python missing -> fail open
fi

PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m caps ponytail
exit 0
