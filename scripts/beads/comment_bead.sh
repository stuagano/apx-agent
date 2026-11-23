#!/usr/bin/env bash
# Comment the bead id on a GitHub issue. No-op when that marker is already there.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: comment_bead.sh ISSUE_NUMBER BEAD_ID" >&2
  exit 2
fi

number="$1"
bead="$2"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
marker="Bead: ${bead}"

bodies="$(gh api --paginate "repos/${repo}/issues/${number}/comments" --jq '.[].body')"
if printf '%s\n' "$bodies" | grep -F -x -q "$marker"; then
  exit 0
fi

gh issue comment "$number" --repo "$repo" --body "$marker"
