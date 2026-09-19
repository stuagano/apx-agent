#!/usr/bin/env bash
# Select pytest paths for CI.
#
# pull_request → runtime core + tests mapped from the diff.
# Anything else (push, workflow_dispatch, schedule) → full suite.
# Fail-safe → full suite (unknown src, unreadable diff, wide-blast files).
#
# Stdout: space-separated paths relative to python/ (so `uv run pytest $paths`
# works with working-directory: python).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_DIR="$ROOT/python/tests"

full_suite() {
  echo "tests/"
  exit 0
}

# Non-PR events keep the full 3.11 + 3.13 suite. Missing event name is fail-safe.
if [ "${EVENT_NAME:-}" != "pull_request" ]; then
  full_suite
fi

changed=""
if [ -n "${PR_PYTEST_CHANGED_FILES:-}" ]; then
  changed="$PR_PYTEST_CHANGED_FILES"
elif [ -n "${BASE:-}" ] && [ -n "${HEAD:-}" ]; then
  if ! git -C "$ROOT" cat-file -e "${BASE}^{commit}" 2>/dev/null; then
    git -C "$ROOT" fetch --no-tags --depth=1 origin "$BASE" >/dev/null 2>&1 || full_suite
  fi
  if ! changed=$(git -C "$ROOT" diff --name-only "$BASE" "$HEAD"); then
    full_suite
  fi
else
  full_suite
fi

is_wide_blast() {
  case "$1" in
    python/tests/conftest.py|\
    python/pyproject.toml|\
    python/uv.lock|\
    python/src/apx_agent/__init__.py|\
    python/src/apx_agent/_defaults.py|\
    python/src/apx_agent/_models.py)
      return 0
      ;;
  esac
  return 1
}

exist_glob() {
  local pattern="$1"
  local f
  shopt -s nullglob
  for f in "$TESTS_DIR"/$pattern; do
    if [ -f "$f" ]; then
      printf 'tests/%s\n' "$(basename "$f")"
    fi
  done
  shopt -u nullglob
}

core_paths() {
  exist_glob 'test_compile*.py'
  exist_glob 'test_parallel_context_isolation.py'
  exist_glob 'test_*a2a*.py'
  exist_glob 'test_mlflow_tracing.py'
  exist_glob 'test_multi_hop*.py'
  exist_glob 'test_*reality_ctk.py'
  exist_glob 'test_wiring.py'
  exist_glob 'test_remote*.py'
  exist_glob 'test_agents*.py'
  exist_glob 'test_pr_pytest_paths.py'
  local f
  shopt -s nullglob
  for f in "$TESTS_DIR"/gates/test_*.py; do
    printf 'tests/gates/%s\n' "$(basename "$f")"
  done
  shopt -u nullglob
}

map_src() {
  local rel="$1"
  local base
  base="$(basename "$rel")"
  case "$base" in
    *.py) ;;
    *) return 1 ;;
  esac
  local stem="${base%.py}"
  local mod="${stem#_}"
  local found=0
  local f
  shopt -s nullglob
  for f in "$TESTS_DIR"/test_"${mod}".py "$TESTS_DIR"/test_"${mod}"_*.py; do
    if [ -f "$f" ]; then
      printf 'tests/%s\n' "$(basename "$f")"
      found=1
    fi
  done
  shopt -u nullglob
  [ "$found" -eq 1 ]
}

paths_file="$(mktemp)"
core_paths >"$paths_file"

unknown=0
while IFS= read -r f || [ -n "$f" ]; do
  [ -z "$f" ] && continue
  if is_wide_blast "$f"; then
    rm -f "$paths_file"
    full_suite
  fi
  case "$f" in
    python/tests/*)
      case "$f" in
        *.py)
          printf 'tests/%s\n' "${f#python/tests/}" >>"$paths_file"
          ;;
      esac
      ;;
    python/src/apx_agent/*)
      if mapped=$(map_src "$f"); then
        printf '%s\n' "$mapped" >>"$paths_file"
      else
        unknown=1
      fi
      ;;
    typescript/*)
      exist_glob 'test_appkit*.py' >>"$paths_file"
      ;;
    .github/*|scripts/*)
      if [ -f "$TESTS_DIR/test_pr_pytest_paths.py" ]; then
        printf 'tests/test_pr_pytest_paths.py\n' >>"$paths_file"
      fi
      ;;
    docs/*)
      ;;
    *.md)
      ;;
    *)
      unknown=1
      ;;
  esac
done <<<"$changed"

if [ "$unknown" -eq 1 ]; then
  rm -f "$paths_file"
  full_suite
fi

# Unique, stable, space-separated. Skip paths that vanished between listing
# and now (deleted in the PR) so pytest does not get a missing-file error.
{
  sort -u "$paths_file" | while IFS= read -r p || [ -n "$p" ]; do
    [ -z "$p" ] && continue
    if [ -e "$ROOT/python/$p" ]; then
      printf '%s\n' "$p"
    fi
  done
} | tr '\n' ' '
echo
rm -f "$paths_file"
