"""apx-agent shell — an interactive REPL so you're *in* apx, not firing one-off
shell commands.

You get an ambient context prompt (project ▸ profile), run subcommands without
the ``apx-agent`` prefix, tab-complete the whole command tree (powered by the
same introspection ``describe-cli`` uses), and keep history — all on the stdlib
(``readline``), no new dependencies.

Built-ins beyond the click commands: ``cd`` (navigate projects, prompt
follows), ``help``, ``clear``, ``exit``/``quit`` (or Ctrl-D).
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

import click

_BUILTINS = ("cd", "help", "clear", "exit", "quit")


def _command_tree() -> dict[str, Any]:
    """The CLI command tree (groups → subcommands → params) for completion —
    the same structure ``describe-cli`` emits."""
    from .cli import _describe_command, main

    return _describe_command(main, click.Context(main, info_name="apx"))


def _completion_candidates(tree: dict[str, Any], words: list[str], text: str) -> list[str]:
    """Completion candidates for ``text`` given the already-typed ``words``.

    Walks the tree by the command words typed so far, then offers: flags when
    ``text`` starts with ``-``; otherwise the next subcommand names (plus the
    shell built-ins at the top level). Pure + offline — unit-tested.
    """
    node = tree
    consumed = 0
    for w in words:
        if w.startswith("-"):
            continue  # a flag doesn't move us deeper in the command tree
        cmds = node.get("commands")
        if cmds and w in cmds:
            node = cmds[w]
            consumed += 1
        else:
            break

    if text.startswith("-"):
        cands = [f for p in node.get("params", []) for f in p["flags"] if f.startswith("--")]
    else:
        cmds = node.get("commands") or {}
        cands = list(cmds.keys())
        if consumed == 0:
            cands += list(_BUILTINS)
    return sorted(c for c in cands if c.startswith(text))


def _dispatch(line: str) -> None:
    """Run one REPL line through the click CLI in-process. Click errors are
    shown without killing the session; the REPL survives any exception."""
    from .cli import main

    try:
        args = shlex.split(line)
    except ValueError as exc:
        click.echo(f"parse error: {exc}")
        return
    if not args:
        return
    try:
        main.main(args=args, standalone_mode=False, prog_name="apx-agent")
    except click.exceptions.ClickException as exc:
        exc.show()
    except (click.exceptions.Abort, KeyboardInterrupt):
        click.echo("^C")
    except SystemExit:
        pass  # a command called sys.exit — don't leave the shell
    except Exception as exc:  # noqa: BLE001 — a bad command must not kill the REPL
        click.echo(f"error: {exc}")


def _prompt() -> str:
    from .cli import _status_prompt_string

    return f"{_status_prompt_string(Path.cwd())} ❯ "


def _handle_builtin(line: str) -> bool:
    """Handle a shell built-in. Returns True if the line was a built-in."""
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    if cmd in ("exit", "quit"):
        raise EOFError
    if cmd == "clear":
        click.clear()
        return True
    if cmd == "cd":
        target = parts[1].strip() if len(parts) > 1 else str(Path.home())
        try:
            os.chdir(os.path.expanduser(target))
        except OSError as exc:
            click.echo(f"cd: {exc}")
        return True
    if cmd == "help":
        click.echo(
            "apx-agent shell — run any apx command without the prefix.\n"
            "  e.g.  status · agents list --local · doctor · describe-cli\n"
            "Built-ins: cd <dir>, clear, help, exit/quit (or Ctrl-D).\n"
            "Tab-completes commands and flags."
        )
        return True
    return False


def run_shell() -> None:
    """Launch the interactive apx-agent REPL."""
    import sys

    # Interactive line-editing (tab-completion, history, the editable prompt)
    # only works on a terminal: CPython's input() routes through readline only
    # when stdin AND stdout are TTYs. Piped/redirected, it still reads & runs
    # lines (scriptable), just without those niceties — say so plainly.
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    try:
        import readline
    except ImportError:  # pragma: no cover — readline absent (rare)
        readline = None

    tree = _command_tree()

    if interactive and readline is not None:
        def _completer(text: str, state: int) -> "str | None":
            buf = readline.get_line_buffer()
            words = buf[: readline.get_begidx()].split()
            cands = _completion_candidates(tree, words, text)
            return cands[state] if state < len(cands) else None

        readline.set_completer(_completer)
        readline.set_completer_delims(" \t")
        readline.parse_and_bind("tab: complete")

    click.echo("apx-agent interactive shell · type 'help', Ctrl-D to exit")
    if not interactive:
        click.echo(
            "(not a terminal — tab-completion, history, and line-editing are "
            "disabled; lines are still read and run. For scripting, prefer "
            "`apx-agent <cmd> --json` / `describe-cli`.)"
        )
    while True:
        try:
            line = input(_prompt()).strip()
        except EOFError:
            click.echo()
            break
        except KeyboardInterrupt:
            click.echo("^C  (Ctrl-D or 'exit' to leave)")
            continue
        if not line:
            continue
        try:
            if _handle_builtin(line):
                continue
        except EOFError:
            break
        _dispatch(line)
