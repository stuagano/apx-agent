#!/usr/bin/env python3
"""Hive operations for the apx beads database.

The bd version is scripts/beads/BD_VERSION. GitHub issue imports use the same
labels as the existing hive rows. Bead ids look like apx-om5, apx-ije.2, or
apx-merge-slot. The repo name apx-agent is not a bead id.

A GitHub issue and its bead share the issue URL as external_ref. Opening the
issue creates the bead. Closing the issue closes that bead, and reopening the
issue reopens it. The daily sync does the same for issues that already moved.

A merged pull request closes every bead it names, plus the bead for each
Fixes/Closes #N in the title or body. --force is used only when every open
blocker is an intake-hold gate; any other open blocker leaves the bead open.
The linked GitHub issue is closed too, because a squash merge often drops
the keyword from the commit message and an open issue would make the daily
sync reopen the bead.

Pushing refs/dolt/data uses the dolt remote. When GITHUB_TOKEN and
GITHUB_REPOSITORY are set, origin is pointed at that token before bootstrap.
The token is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LABELS = (
    "intake:untriaged",
    "origin:github",
    "provider:github",
    "repo:apx-agent",
    "org:stuagano",
)
EXEMPT_AUTHORS = frozenset({"dependabot[bot]", "github-actions[bot]"})
EXEMPT_LABEL = "beads:exempt"
NOT_A_BEAD = frozenset({"apx-agent"})
BEAD_ID = re.compile(
    r"(?<![A-Za-z0-9])apx-(?:merge-slot|[a-z0-9]{3,8}(?:\.[0-9]+)?)(?![A-Za-z0-9])"
)
# GitHub's closing keywords. Longer forms come first so "closes" is not read
# as "close". The number has to follow on the same line; "fixed in #12" does not.
_CLOSING_ISSUE = re.compile(
    r"(?i)(?:^|[\s(])(?:closes|closed|close|fixes|fixed|fix|resolves|resolved|resolve)"
    r":?[ \t]+(?:#(\d+)|https://github\.com/[^/\s]+/[^/\s]+/issues/(\d+))(?![A-Za-z0-9])"
)
_TOKEN = re.compile(r"x-access-token:[^@\s]+")
PUSH_ATTEMPTS = 5
# Docs PR #1236 merged at 2026-11-23T16:49:41Z and names apx-e0i without
# finishing that work. The first 12-hour backfill still covers that merge.
# Ignore anything earlier than 17:00Z. Once the window starts later, this
# floor no longer moves the cutoff.
MERGE_SCAN_FLOOR = datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PrBeadCheck:
    ok: bool
    reason: str
    bead_ids: list[str]


@dataclass(frozen=True)
class Ensured:
    bead_id: str
    created: bool


@dataclass(frozen=True)
class IssueSnapshot:
    number: int
    title: str
    body: str
    url: str
    state: str


@dataclass(frozen=True)
class IssueSync:
    bead_id: str
    outcome: str


@dataclass(frozen=True)
class PrSnapshot:
    number: int
    title: str
    body: str
    branch: str


def redact(text: str) -> str:
    return _TOKEN.sub("x-access-token:REDACTED", text)


def referenced_bead_ids(title: str, body: str, branch: str) -> list[str]:
    """Bead ids mentioned in a PR title, body, or branch, in that order."""
    found: list[str] = []
    seen: set[str] = set()
    for text in (title, body, branch):
        for match in BEAD_ID.finditer(text):
            bead_id = match.group(0)
            if bead_id in NOT_A_BEAD or bead_id in seen:
                continue
            seen.add(bead_id)
            found.append(bead_id)
    return found


def evaluate_pr(
    title: str,
    body: str,
    branch: str,
    labels: list[str],
    author: str,
) -> PrBeadCheck:
    if author in EXEMPT_AUTHORS:
        return PrBeadCheck(True, f"exempt author {author}", [])
    if EXEMPT_LABEL in labels:
        return PrBeadCheck(True, f"exempt label {EXEMPT_LABEL}", [])
    bead_ids = referenced_bead_ids(title, body, branch)
    if not bead_ids:
        return PrBeadCheck(
            False,
            "bead reference missing: put a bead id (apx-…) in the title, body, "
            "or branch (wt/bead/issue/<id>), or add the beads:exempt label",
            [],
        )
    return PrBeadCheck(True, "referenced " + ", ".join(bead_ids), bead_ids)


def _run(args: list[str], *, cwd: Path, check: bool) -> CmdResult:
    proc = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    result = CmdResult(proc.returncode, proc.stdout, proc.stderr)
    if check and result.returncode != 0:
        detail = redact(result.stderr.strip())
        raise SystemExit(f"{' '.join(args[:3])} failed: {detail}")
    return result


def _bd() -> str:
    return os.environ["BD"] if "BD" in os.environ else "bd"


def authenticate_origin(cwd: Path) -> None:
    """Point origin at GITHUB_TOKEN when both credential env vars are present."""
    if "GITHUB_TOKEN" not in os.environ:
        return
    if "GITHUB_REPOSITORY" not in os.environ:
        raise SystemExit("GITHUB_REPOSITORY is required when GITHUB_TOKEN is set")
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    if token == "" or repo == "":
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY must be non-empty")
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _run(["git", "remote", "set-url", "origin", url], cwd=cwd, check=True)


def ensure_database(cwd: Path) -> None:
    """Clone refs/dolt/data when this checkout has no hive yet."""
    if (cwd / ".beads").is_dir():
        return
    authenticate_origin(cwd)
    _run([_bd(), "bootstrap", "--yes"], cwd=cwd, check=True)


def find_bead_row(cwd: Path, url: str) -> dict[str, Any] | None:
    """Return the bead whose external_ref is this issue URL, including closed."""
    result = _run(
        [
            _bd(),
            "list",
            "--external-ref",
            url,
            "--all",
            "--json",
            "--limit",
            "0",
            "--flat",
            "--include-infra",
            "--include-ephemeral",
            "--include-templates",
            "--include-gates",
        ],
        cwd=cwd,
        check=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise SystemExit("bd list --json did not return a list")
    if len(rows) == 0:
        return None
    if len(rows) > 1:
        ids = [str(row["id"]) for row in rows]
        raise SystemExit(f"multiple beads for {url}: {ids}")
    row = rows[0]
    if not isinstance(row, dict):
        raise SystemExit("bd list row is not an object")
    return row


def find_bead(cwd: Path, url: str) -> str | None:
    row = find_bead_row(cwd, url)
    if row is None:
        return None
    return str(row["id"])


def _blank_body(value: Any) -> str:
    """GitHub sends JSON null when an issue or PR body was left empty."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SystemExit("body is not a string")
    return value


def create_bead(cwd: Path, title: str, body: str, url: str) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    cmd = [
        _bd(),
        "--sandbox",
        "create",
        "--title",
        title,
        "--body-file",
        body_path,
        "--type",
        "bug",
        "--priority",
        "2",
        "--status",
        "open",
        "--external-ref",
        url,
        "--labels",
        ",".join(LABELS),
        "--json",
    ]
    if body == "":
        cmd.append("--allow-empty-description")
    try:
        result = _run(cmd, cwd=cwd, check=True)
    finally:
        os.unlink(body_path)
    created = json.loads(result.stdout)
    bead_id = str(created["id"])
    if bead_id == "":
        raise SystemExit("bd create returned an empty id")
    return bead_id


def ensure_issue(cwd: Path, title: str, body: str, url: str, *, dry_run: bool) -> Ensured:
    existing = find_bead(cwd, url)
    if existing is not None:
        return Ensured(existing, False)
    if dry_run:
        return Ensured("", True)
    return Ensured(create_bead(cwd, title, body, url), True)


def show_bead(cwd: Path, bead_id: str) -> dict[str, Any]:
    result = _run([_bd(), "show", bead_id, "--json"], cwd=cwd, check=False)
    if result.returncode != 0:
        raise SystemExit(f"bead {bead_id} was not found")
    payload = json.loads(result.stdout)
    if isinstance(payload, list):
        if len(payload) != 1:
            raise SystemExit(f"bd show {bead_id} returned {len(payload)} rows")
        row = payload[0]
    else:
        row = payload
    if not isinstance(row, dict):
        raise SystemExit(f"bd show {bead_id} did not return an object")
    return row


def blocking_ids(row: dict[str, Any]) -> list[str]:
    """Open beads that block this one. Closed and non-blocking edges are omitted."""
    if "dependencies" not in row:
        return []
    deps = row["dependencies"]
    if not isinstance(deps, list):
        return []
    found: list[str] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        if "dependency_type" not in dep or dep["dependency_type"] != "blocks":
            continue
        if "status" not in dep or dep["status"] == "closed":
            continue
        if "id" not in dep or not isinstance(dep["id"], str) or dep["id"] == "":
            continue
        found.append(dep["id"])
    return found


def closing_issue_numbers(text: str) -> list[int]:
    """Issue numbers a title or body closes, in order, without duplicates."""
    found: list[int] = []
    seen: set[int] = set()
    for match in _CLOSING_ISSUE.finditer(text):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if raw is None:
            continue
        number = int(raw)
        if number in seen:
            continue
        seen.add(number)
        found.append(number)
    return found


def is_intake_hold_gate(row: dict[str, Any]) -> bool:
    if "issue_type" not in row or row["issue_type"] != "gate":
        return False
    if "description" not in row or not isinstance(row["description"], str):
        return False
    return "intake-hold:" in row["description"]


def only_intake_hold_blockers(rows: list[Any]) -> bool:
    """True when every open ``blocks`` edge is an intake-hold gate.

    An empty list, a closed edge, or any other open blocker is false.
    """
    blocking: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "dependency_type" not in row or row["dependency_type"] != "blocks":
            continue
        if "status" not in row or row["status"] == "closed":
            continue
        blocking.append(row)
    if not blocking:
        return False
    for row in blocking:
        if not is_intake_hold_gate(row):
            return False
    return True


def merge_close_force(cwd: Path, bead_id: str) -> bool:
    """Whether a merged pull request may close this bead with ``--force``."""
    row = show_bead(cwd, bead_id)
    if "dependencies" not in row or not isinstance(row["dependencies"], list):
        return False
    return only_intake_hold_blockers(row["dependencies"])


def close_bead(
    cwd: Path,
    bead_id: str,
    reason: str,
    *,
    dry_run: bool,
    force: bool = False,
) -> str:
    """Close a bead. ``force`` overrides an assignee, pin, or open gate.

    Issue sync passes force. A merged pull request passes force only when
    every open blocker is an intake-hold gate, and logs those blockers first.
    """
    row = show_bead(cwd, bead_id)
    status = row["status"]
    if status == "closed":
        return "already-closed"
    blockers = blocking_ids(row)
    if force and blockers:
        print(f"{bead_id} is blocked by [{', '.join(blockers)}]; closing with --force")
    if dry_run:
        return "would-close"
    cmd = [_bd(), "--sandbox", "close", bead_id, "--reason", reason]
    if force:
        cmd.append("--force")
    _run(cmd, cwd=cwd, check=True)
    return "closed"


def reopen_bead(cwd: Path, bead_id: str, reason: str, *, dry_run: bool) -> str:
    row = show_bead(cwd, bead_id)
    if row["status"] != "closed":
        return "already-open"
    if dry_run:
        return "would-reopen"
    _run([_bd(), "--sandbox", "reopen", bead_id, "--reason", reason], cwd=cwd, check=True)
    return "reopened"


def normalize_github_state(value: Any) -> str:
    """Accept the webhook spelling (open) and the gh CLI spelling (OPEN)."""
    if not isinstance(value, str):
        raise SystemExit("issue state is not a string")
    state = value.lower()
    if state != "open" and state != "closed":
        raise SystemExit(f"unsupported issue state {value}")
    return state


def plan_issue_sync(github_state: str, bead_status: str | None) -> str:
    """Decide how a bead should follow its GitHub issue.

    ``bead_status`` is None when no bead has this issue URL. A closed issue
    with no bead is skipped. An open issue reopens a closed bead and leaves
    any other status, including in_progress, unchanged.
    """
    state = normalize_github_state(github_state)
    if bead_status is None:
        if state == "open":
            return "create"
        return "skip"
    if state == "closed":
        if bead_status == "closed":
            return "unchanged"
        return "close"
    if bead_status == "closed":
        return "reopen"
    return "unchanged"


def _issue_url_value(value: Any) -> str:
    if not isinstance(value, str) or "/issues/" not in value:
        raise SystemExit("issue url is required")
    return value


def snapshot_from_mapping(issue: dict[str, Any], *, url_key: str) -> IssueSnapshot:
    number = issue["number"]
    title = issue["title"]
    if not isinstance(number, int) or not isinstance(title, str) or title == "":
        raise SystemExit("issue number and title are required")
    if "state" not in issue:
        raise SystemExit("issue state is required")
    body = _blank_body(issue["body"] if "body" in issue else None)
    return IssueSnapshot(
        number,
        title,
        body,
        _issue_url_value(issue[url_key]),
        normalize_github_state(issue["state"]),
    )


def issue_from_event(event: dict[str, Any]) -> IssueSnapshot:
    issue = event["issue"]
    if not isinstance(issue, dict):
        raise SystemExit("issue is not an object")
    if "pull_request" in issue:
        raise SystemExit("pull requests are not imported as issues")
    return snapshot_from_mapping(issue, url_key="html_url")


def issues_from_payload(payload: Any) -> list[IssueSnapshot]:
    if not isinstance(payload, list):
        raise SystemExit("issues file is not a list")
    snapshots: list[IssueSnapshot] = []
    for issue in payload:
        if not isinstance(issue, dict):
            raise SystemExit("issue row is not an object")
        snapshots.append(snapshot_from_mapping(issue, url_key="url"))
    return snapshots


def apply_issue_sync(cwd: Path, issue: IssueSnapshot, *, dry_run: bool) -> IssueSync:
    """Create, close, or reopen the bead that matches this issue URL."""
    row = find_bead_row(cwd, issue.url)
    status: str | None = None
    bead_id = ""
    if row is not None:
        bead_id = str(row["id"])
        status_value = row["status"]
        if not isinstance(status_value, str):
            raise SystemExit(f"bead {bead_id} status is not a string")
        status = status_value
    action = plan_issue_sync(issue.state, status)
    if action == "skip":
        return IssueSync("", "skipped")
    if action == "unchanged":
        return IssueSync(bead_id, "unchanged")
    if action == "create":
        if dry_run:
            return IssueSync("", "would-create")
        return IssueSync(create_bead(cwd, issue.title, issue.body, issue.url), "created")
    if action == "close":
        outcome = close_bead(
            cwd,
            bead_id,
            f"GitHub issue #{issue.number} is closed",
            dry_run=dry_run,
            force=True,
        )
        return IssueSync(bead_id, outcome)
    if action == "reopen":
        outcome = reopen_bead(
            cwd,
            bead_id,
            f"GitHub issue #{issue.number} is open",
            dry_run=dry_run,
        )
        return IssueSync(bead_id, outcome)
    raise SystemExit(f"unknown issue sync action {action}")


_PUSH_OUTCOMES = frozenset({"created", "closed", "reopened"})


def _push_if_changed(cwd: Path, changed: bool, *, dry_run: bool, no_push: bool) -> None:
    if changed and not dry_run and not no_push:
        push_with_retry(_subprocess_execute(cwd), time.sleep)


def _print_sync(number: int, result: IssueSync) -> None:
    json.dump(
        {
            "number": number,
            "bead_id": result.bead_id,
            "created": result.outcome == "created",
            "outcome": result.outcome,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def push_with_retry(
    execute: Callable[[list[str]], CmdResult],
    pause: Callable[[int], None],
) -> None:
    """Push refs/dolt/data. On failure, pull and retry. Never force-push."""
    last = ""
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        pushed = execute([_bd(), "dolt", "push"])
        if pushed.returncode == 0:
            return
        last = redact(pushed.stderr.strip())
        pulled = execute([_bd(), "dolt", "pull"])
        if pulled.returncode != 0:
            last = redact(pulled.stderr.strip())
        if attempt < PUSH_ATTEMPTS:
            pause(2**attempt)
    raise SystemExit(f"bd dolt push failed after {PUSH_ATTEMPTS} attempts: {last}")


def _subprocess_execute(cwd: Path) -> Callable[[list[str]], CmdResult]:
    def execute(args: list[str]) -> CmdResult:
        return _run(args, cwd=cwd, check=False)

    return execute


def comment_bead(number: int, bead_id: str) -> None:
    script = Path(__file__).with_name("comment_bead.sh")
    _run([str(script), str(number), bead_id], cwd=Path.cwd(), check=True)


def _issue_url(number: int, explicit: str) -> str:
    if explicit != "":
        return explicit
    if "GITHUB_REPOSITORY" not in os.environ:
        raise SystemExit("pass --url or set GITHUB_REPOSITORY")
    repo = os.environ["GITHUB_REPOSITORY"]
    return f"https://github.com/{repo}/issues/{number}"


def _load_event(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit("GitHub event is not an object")
    return payload


def _pull_request(event: dict[str, Any]) -> dict[str, Any]:
    pr = event["pull_request"]
    if not isinstance(pr, dict):
        raise SystemExit("pull_request is not an object")
    return pr


def check_from_event(event: dict[str, Any]) -> PrBeadCheck:
    pr = _pull_request(event)
    title = pr["title"]
    if not isinstance(title, str):
        raise SystemExit("pull request title is not a string")
    head = pr["head"]
    if not isinstance(head, dict):
        raise SystemExit("pull request head is not an object")
    branch = head["ref"]
    if not isinstance(branch, str):
        raise SystemExit("pull request branch is not a string")
    user = pr["user"]
    if not isinstance(user, dict):
        raise SystemExit("pull request user is not an object")
    author = user["login"]
    if not isinstance(author, str):
        raise SystemExit("pull request author is not a string")
    labels: list[str] = []
    raw_labels = pr["labels"]
    if not isinstance(raw_labels, list):
        raise SystemExit("pull request labels are not a list")
    for label in raw_labels:
        if not isinstance(label, dict):
            raise SystemExit("pull request label is not an object")
        name = label["name"]
        if not isinstance(name, str):
            raise SystemExit("pull request label name is not a string")
        labels.append(name)
    return evaluate_pr(title, _blank_body(pr["body"] if "body" in pr else None), branch, labels, author)


def _print_ensured(number: int, ensured: Ensured) -> None:
    json.dump(
        {"number": number, "bead_id": ensured.bead_id, "created": ensured.created},
        sys.stdout,
    )
    sys.stdout.write("\n")


def cmd_check_pr(args: argparse.Namespace) -> int:
    if args.event != "":
        result = check_from_event(_load_event(args.event))
    else:
        body = Path(args.body_file).read_text(encoding="utf-8")
        labels = [label for label in args.labels.split(",") if label != ""]
        result = evaluate_pr(args.title, body, args.branch, labels, args.author)
    print(result.reason)
    return 0 if result.ok else 1


def cmd_ensure_issue(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.event != "":
        event = _load_event(args.event)
        issue = event["issue"]
        if not isinstance(issue, dict):
            raise SystemExit("issue is not an object")
        if "pull_request" in issue:
            raise SystemExit("pull requests are not imported as issues")
        number = issue["number"]
        title = issue["title"]
        url = issue["html_url"]
        if not isinstance(number, int) or not isinstance(title, str) or not isinstance(url, str):
            raise SystemExit("issue number, title, and html_url are required")
        body = _blank_body(issue["body"] if "body" in issue else None)
    else:
        number = args.number
        title = args.title
        body = Path(args.body_file).read_text(encoding="utf-8")
        url = _issue_url(number, args.url)
    ensure_database(cwd)
    ensured = ensure_issue(cwd, title, body, url, dry_run=args.dry_run)
    if ensured.created and not args.dry_run and not args.no_push:
        push_with_retry(_subprocess_execute(cwd), time.sleep)
    if ensured.created and args.dry_run:
        print(f"would create a bead for {url}")
        return 0
    _print_ensured(number, ensured)
    if args.comment and not args.dry_run:
        comment_bead(number, ensured.bead_id)
    return 0


def _repository() -> str:
    if "GITHUB_REPOSITORY" not in os.environ:
        return ""
    return os.environ["GITHUB_REPOSITORY"]


def snapshot_from_event_pr(pr: dict[str, Any]) -> PrSnapshot:
    title = pr["title"]
    head = pr["head"]
    if not isinstance(title, str) or not isinstance(head, dict):
        raise SystemExit("pull request title and head are required")
    branch = head["ref"]
    if not isinstance(branch, str):
        raise SystemExit("pull request branch is not a string")
    number = pr["number"]
    if not isinstance(number, int):
        raise SystemExit("pull request number is not an int")
    body = _blank_body(pr["body"] if "body" in pr else None)
    return PrSnapshot(number, title, body, branch)


def linked_bead_ids(
    cwd: Path,
    title: str,
    body: str,
    branch: str,
    repository: str,
    numbers: list[int],
) -> list[str]:
    """Bead ids named on the PR, then the bead for each closing keyword."""
    ids = referenced_bead_ids(title, body, branch)
    if not numbers:
        return ids
    if repository == "":
        print("GITHUB_REPOSITORY is unset; skipping Fixes/Closes bead lookup")
        return ids
    for number in numbers:
        url = f"https://github.com/{repository}/issues/{number}"
        bead_id = find_bead(cwd, url)
        if bead_id is None:
            print(f"issue #{number} has no bead")
            continue
        if bead_id not in ids:
            ids.append(bead_id)
    return ids


def close_linked_issue(repository: str, number: int, note: str) -> str:
    """Comment on and close a GitHub issue. Skip pull requests and closed issues."""
    viewed = _run(
        [
            "gh",
            "api",
            f"repos/{repository}/issues/{number}",
            "--jq",
            "{state:.state,is_pr:(.pull_request != null)}",
        ],
        cwd=Path.cwd(),
        check=True,
    )
    info = json.loads(viewed.stdout)
    if not isinstance(info, dict):
        raise SystemExit(f"issue #{number} lookup did not return an object")
    if info["is_pr"] is True:
        return "skipped-pull-request"
    state = info["state"]
    if not isinstance(state, str):
        raise SystemExit(f"issue #{number} state is not a string")
    if state.lower() != "open":
        return "already-closed"
    _run(
        ["gh", "issue", "comment", str(number), "--repo", repository, "--body", note],
        cwd=Path.cwd(),
        check=True,
    )
    _run(
        [
            "gh",
            "issue",
            "close",
            str(number),
            "--repo",
            repository,
            "--reason",
            "completed",
        ],
        cwd=Path.cwd(),
        check=True,
    )
    return "closed"


def issue_close_note(pr: PrSnapshot) -> str:
    if pr.number == 0:
        return f"Closed by a merged pull request: {pr.title}"
    return f"Closed by merged PR #{pr.number}: {pr.title}"


def close_pull_request(
    cwd: Path,
    pr: PrSnapshot,
    repository: str,
    reason: str,
    *,
    dry_run: bool,
) -> bool:
    """Close this PR's beads, then close linked issues.

    ``--force`` is passed only when every open blocker is an intake-hold
    gate. Return True when the hive changed.
    """
    numbers = closing_issue_numbers(f"{pr.title}\n{pr.body}")
    bead_ids = linked_bead_ids(cwd, pr.title, pr.body, pr.branch, repository, numbers)
    if not bead_ids and not numbers:
        print(f"PR #{pr.number}: no bead id; nothing to close")
        return False
    changed = False
    for bead_id in bead_ids:
        outcome = close_bead(
            cwd,
            bead_id,
            reason,
            dry_run=dry_run,
            force=merge_close_force(cwd, bead_id),
        )
        print(f"{bead_id} {outcome}")
        if outcome == "closed":
            changed = True
    note = issue_close_note(pr)
    for number in numbers:
        if repository == "":
            print(f"issue #{number} not closed; GITHUB_REPOSITORY is unset")
            continue
        if dry_run:
            print(f"would close issue #{number}")
            continue
        print(f"issue #{number} {close_linked_issue(repository, number, note)}")
    return changed


def _finish_closes(cwd: Path, changed: bool, *, dry_run: bool, no_push: bool) -> None:
    if changed and not dry_run and not no_push:
        push_with_retry(_subprocess_execute(cwd), time.sleep)


def cmd_close_pr(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.event != "":
        event = _load_event(args.event)
        pr = _pull_request(event)
        merged = pr["merged"]
        if merged is not True:
            print("pull request is not merged; nothing to close")
            return 0
        snapshot = snapshot_from_event_pr(pr)
        reason = f"Merged PR #{snapshot.number}"
    else:
        snapshot = PrSnapshot(
            0,
            args.title,
            Path(args.body_file).read_text(encoding="utf-8"),
            args.branch,
        )
        reason = args.reason
    numbers = closing_issue_numbers(f"{snapshot.title}\n{snapshot.body}")
    if not referenced_bead_ids(snapshot.title, snapshot.body, snapshot.branch) and not numbers:
        print("no bead id on the pull request; nothing to close")
        return 0
    ensure_database(cwd)
    changed = close_pull_request(cwd, snapshot, _repository(), reason, dry_run=args.dry_run)
    _finish_closes(cwd, changed, dry_run=args.dry_run, no_push=args.no_push)
    return 0


def parse_github_time(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise SystemExit(f"mergedAt {value} has no timezone")
    return parsed


def select_recent_prs(payload: Any, *, within_hours: int, now: datetime) -> list[PrSnapshot]:
    """Merged PRs from a ``gh pr list`` payload, newest window only.

    ``within_hours`` of 0 keeps every row. A positive window drops a pull
    request whose ``mergedAt`` is older than that, which is how the schedule
    avoids replaying merges from earlier in the week. The cutoff never moves
    earlier than ``MERGE_SCAN_FLOOR``.
    """
    if not isinstance(payload, list):
        raise SystemExit("pull requests file is not a list")
    if within_hours < 0:
        raise SystemExit("within-hours must be >= 0")
    cutoff = None if within_hours == 0 else now - timedelta(hours=within_hours)
    if cutoff is not None and cutoff < MERGE_SCAN_FLOOR:
        cutoff = MERGE_SCAN_FLOOR
    selected: list[PrSnapshot] = []
    for row in payload:
        if not isinstance(row, dict):
            raise SystemExit("pull request row is not an object")
        number = row["number"]
        title = row["title"]
        if not isinstance(number, int) or not isinstance(title, str) or title == "":
            raise SystemExit("pull request number and title are required")
        if "headRefName" not in row or not isinstance(row["headRefName"], str):
            raise SystemExit("pull request headRefName is required")
        merged_at: datetime | None = None
        if "mergedAt" in row and row["mergedAt"] is not None:
            if not isinstance(row["mergedAt"], str):
                raise SystemExit("mergedAt is not a string")
            merged_at = parse_github_time(row["mergedAt"])
        if cutoff is not None and (merged_at is None or merged_at < cutoff):
            continue
        body = _blank_body(row["body"] if "body" in row else None)
        selected.append(PrSnapshot(number, title, body, row["headRefName"]))
    return selected


def cmd_close_merged(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    with open(args.prs_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    prs = select_recent_prs(
        payload,
        within_hours=args.within_hours,
        now=datetime.now(timezone.utc),
    )
    if not prs:
        print("no merged pull requests in the window")
        return 0
    ensure_database(cwd)
    repository = _repository()
    changed = False
    for pr in prs:
        if close_pull_request(
            cwd,
            pr,
            repository,
            f"Merged PR #{pr.number}",
            dry_run=args.dry_run,
        ):
            changed = True
    _finish_closes(cwd, changed, dry_run=args.dry_run, no_push=args.no_push)
    return 0


def _sync_one(cwd: Path, issue: IssueSnapshot, args: argparse.Namespace) -> bool:
    """Apply one issue. Return True when the hive changed."""
    result = apply_issue_sync(cwd, issue, dry_run=args.dry_run)
    if result.outcome == "skipped" or result.outcome == "unchanged":
        return False
    if result.outcome == "would-create":
        print(f"would create a bead for {issue.url}")
        return False
    _print_sync(issue.number, result)
    if result.outcome == "created" and args.comment and not args.dry_run:
        comment_bead(issue.number, result.bead_id)
    return result.outcome in _PUSH_OUTCOMES


def cmd_sync_issue(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.event != "":
        issue = issue_from_event(_load_event(args.event))
    else:
        body = Path(args.body_file).read_text(encoding="utf-8")
        issue = snapshot_from_mapping(
            {
                "number": args.number,
                "title": args.title,
                "body": body,
                "url": _issue_url(args.number, args.url),
                "state": args.state,
            },
            url_key="url",
        )
    ensure_database(cwd)
    changed = _sync_one(cwd, issue, args)
    _push_if_changed(cwd, changed, dry_run=args.dry_run, no_push=args.no_push)
    return 0


def cmd_sync_open(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    with open(args.issues_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    issues = issues_from_payload(payload)
    if len(issues) >= args.limit:
        raise SystemExit(
            f"issue list has {len(issues)} rows, at the limit {args.limit}; refusing to guess that the list is complete"
        )
    ensure_database(cwd)
    changed = False
    for issue in issues:
        if _sync_one(cwd, issue, args):
            changed = True
    _push_if_changed(cwd, changed, dry_run=args.dry_run, no_push=args.no_push)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="apx beads hive operations")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-pr")
    check.add_argument("--event", default="")
    check.add_argument("--title", default="")
    check.add_argument("--body-file", default="")
    check.add_argument("--branch", default="")
    check.add_argument("--author", default="")
    check.add_argument("--labels", default="")
    check.set_defaults(func=cmd_check_pr)

    ensure = sub.add_parser("ensure-issue")
    ensure.add_argument("--event", default="")
    ensure.add_argument("--number", type=int)
    ensure.add_argument("--title", default="")
    ensure.add_argument("--body-file", default="")
    ensure.add_argument("--url", default="")
    ensure.add_argument("--dry-run", action="store_true")
    ensure.add_argument("--no-push", action="store_true")
    ensure.add_argument("--comment", action="store_true")
    ensure.set_defaults(func=cmd_ensure_issue)

    close = sub.add_parser("close-pr")
    close.add_argument("--event", default="")
    close.add_argument("--title", default="")
    close.add_argument("--body-file", default="")
    close.add_argument("--branch", default="")
    close.add_argument("--author", default="")
    close.add_argument("--labels", default="")
    close.add_argument("--reason", default="Merged pull request")
    close.add_argument("--dry-run", action="store_true")
    close.add_argument("--no-push", action="store_true")
    close.set_defaults(func=cmd_close_pr)

    merged = sub.add_parser("close-merged")
    merged.add_argument("--prs-file", required=True)
    merged.add_argument("--within-hours", type=int, default=12)
    merged.add_argument("--dry-run", action="store_true")
    merged.add_argument("--no-push", action="store_true")
    merged.set_defaults(func=cmd_close_merged)

    sync_issue = sub.add_parser("sync-issue")
    sync_issue.add_argument("--event", default="")
    sync_issue.add_argument("--number", type=int)
    sync_issue.add_argument("--title", default="")
    sync_issue.add_argument("--body-file", default="")
    sync_issue.add_argument("--url", default="")
    sync_issue.add_argument("--state", default="")
    sync_issue.add_argument("--dry-run", action="store_true")
    sync_issue.add_argument("--no-push", action="store_true")
    sync_issue.add_argument("--comment", action="store_true")
    sync_issue.set_defaults(func=cmd_sync_issue)

    sync = sub.add_parser("sync-open")
    sync.add_argument("--issues-file", required=True)
    sync.add_argument("--limit", type=int, default=1000)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--no-push", action="store_true")
    sync.add_argument("--comment", action="store_true")
    sync.set_defaults(func=cmd_sync_open)
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "check-pr" and args.event == "":
        missing = [
            name
            for name, value in (
                ("--title", args.title),
                ("--body-file", args.body_file),
                ("--branch", args.branch),
                ("--author", args.author),
            )
            if value == ""
        ]
        if missing:
            raise SystemExit("check-pr needs --event or " + ", ".join(missing))
    if args.command == "ensure-issue" and args.event == "":
        if args.number is None or args.title == "" or args.body_file == "":
            raise SystemExit("ensure-issue needs --event or --number, --title, and --body-file")
    if args.command == "sync-issue" and args.event == "":
        if args.number is None or args.title == "" or args.body_file == "" or args.state == "":
            raise SystemExit(
                "sync-issue needs --event or --number, --title, --body-file, and --state"
            )
    if args.command == "close-pr" and args.event == "":
        missing = [
            name
            for name, value in (
                ("--title", args.title),
                ("--body-file", args.body_file),
                ("--branch", args.branch),
                ("--author", args.author),
            )
            if value == ""
        ]
        if missing:
            raise SystemExit("close-pr needs --event or " + ", ".join(missing))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
