#!/usr/bin/env python3
"""Hive operations for the apx beads database.

The bd version is scripts/beads/BD_VERSION. GitHub issue imports use the same
labels as the existing hive rows. Bead ids look like apx-om5, apx-ije.2, or
apx-merge-slot. The repo name apx-agent is not a bead id.

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
_TOKEN = re.compile(r"x-access-token:[^@\s]+")
PUSH_ATTEMPTS = 5


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


def find_bead(cwd: Path, url: str) -> str | None:
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
    return str(rows[0]["id"])


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


def close_bead(cwd: Path, bead_id: str, reason: str, *, dry_run: bool) -> str:
    row = show_bead(cwd, bead_id)
    status = row["status"]
    if status == "closed":
        return "already-closed"
    if dry_run:
        return "would-close"
    _run([_bd(), "--sandbox", "close", bead_id, "--reason", reason], cwd=cwd, check=True)
    return "closed"


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


def cmd_close_pr(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.event != "":
        event = _load_event(args.event)
        pr = _pull_request(event)
        merged = pr["merged"]
        if merged is not True:
            print("pull request is not merged; nothing to close")
            return 0
        title = pr["title"]
        head = pr["head"]
        if not isinstance(title, str) or not isinstance(head, dict):
            raise SystemExit("pull request title and head are required")
        branch = head["ref"]
        if not isinstance(branch, str):
            raise SystemExit("pull request branch is not a string")
        body = _blank_body(pr["body"] if "body" in pr else None)
        number = pr["number"]
        if not isinstance(number, int):
            raise SystemExit("pull request number is not an int")
        reason = f"Merged PR #{number}"
    else:
        title = args.title
        body = Path(args.body_file).read_text(encoding="utf-8")
        branch = args.branch
        reason = args.reason
    bead_ids = referenced_bead_ids(title, body, branch)
    if not bead_ids:
        print("no bead id on the pull request; nothing to close")
        return 0
    ensure_database(cwd)
    changed = False
    for bead_id in bead_ids:
        outcome = close_bead(cwd, bead_id, reason, dry_run=args.dry_run)
        print(f"{bead_id} {outcome}")
        if outcome == "closed":
            changed = True
    if changed and not args.no_push:
        push_with_retry(_subprocess_execute(cwd), time.sleep)
    return 0


def cmd_sync_open(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    with open(args.issues_file, encoding="utf-8") as handle:
        issues = json.load(handle)
    if not isinstance(issues, list):
        raise SystemExit("issues file is not a list")
    if len(issues) >= args.limit:
        raise SystemExit(
            f"open issue list has {len(issues)} rows, at the limit {args.limit}; refusing to guess that the list is complete"
        )
    ensure_database(cwd)
    created_any = False
    for issue in issues:
        if not isinstance(issue, dict):
            raise SystemExit("issue row is not an object")
        number = issue["number"]
        title = issue["title"]
        url = issue["url"]
        if not isinstance(number, int) or not isinstance(title, str) or not isinstance(url, str):
            raise SystemExit("each issue needs int number, string title, and string url")
        body = _blank_body(issue["body"] if "body" in issue else None)
        ensured = ensure_issue(cwd, title, body, url, dry_run=args.dry_run)
        if args.dry_run and ensured.created:
            print(f"would create a bead for {url}")
            continue
        _print_ensured(number, ensured)
        if ensured.created and not args.dry_run:
            created_any = True
            if args.comment:
                comment_bead(number, ensured.bead_id)
    if created_any and not args.no_push:
        push_with_retry(_subprocess_execute(cwd), time.sleep)
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
