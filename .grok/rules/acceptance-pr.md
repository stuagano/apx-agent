# Acceptance complete means open the pull request

In this repo, when every acceptance criterion on a bead is met, open the GitHub pull request from `wt/bead/issue/<id>` to `main`. Do not ask first.

A criterion is met only when the committed evidence shows it. A skipped live check, a recorded blocker, a missing principal, or any other unmet item means the bead stays in progress. Do not open a pull request for it.

The pull request body names the bead id, lists the acceptance items, and cites the checks that were actually read. If a pull request for that branch is already open, leave it. Do not open a second one.

Do not `bh work approve` or `bh work merge` unless the operator asks. Do not force-push. Do not push `main`.

`~/Documents/apx-agent` stays on `main`. Commit and push the bead branch from its `bh` worktree before opening the pull request.
