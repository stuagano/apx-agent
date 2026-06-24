#!/usr/bin/env bash
# SessionStart hook for apx-agent development.
#
# 1. Make sure the Python dev environment can run the verify gate (so the Ctk
#    read-after-write step actually works during the session).
# 2. Surface the two development disciplines and the verify command. Hook stdout
#    is added to the session context.
#
# Best-effort: never fail the session. Each step is independently guarded.

set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Best-effort env sync so `make check` / `uv run pytest` are runnable.
if command -v uv >/dev/null 2>&1 && [ -d "$repo_root/python" ]; then
  ( cd "$repo_root/python" && uv sync --quiet ) >/dev/null 2>&1 || true
fi

cat <<'EOF'
apx-agent development — two disciplines apply to every change:

  • Ponytail — don't over-engineer. Reuse an existing primitive before adding
    code; remove anything the tests don't exercise.
  • Ctk — claim-vs-reality. A change is not done because a command exited 0.
    Read the result back: run `make check` (includes the *_reality_ctk.py
    tests) and assert artifacts are real with ctk.verify(Artifact(...)).

Loop each change: choose the smallest change → act → verify (`make check` +
`pre-commit run --all-files`) → keep only if green → repeat or stop.

See CLAUDE.md and docs/loops/README.md.
EOF

exit 0
