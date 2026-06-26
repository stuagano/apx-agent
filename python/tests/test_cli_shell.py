"""apx-agent shell — the interactive REPL. The readline loop is thin; the
testable logic lives in pure functions (completion, dispatch, built-ins)."""
from __future__ import annotations

from pathlib import Path

import click
import pytest

from apx_agent._shell import (
    _command_tree,
    _completion_candidates,
    _dispatch,
    _handle_builtin,
)

# A small synthetic tree mirroring describe-cli's shape, for deterministic
# completion tests independent of the real command surface.
_TREE = {
    "commands": {
        "agents": {"commands": {
            "list": {"params": [
                {"flags": ["--local"]}, {"flags": ["--format"]}, {"flags": ["--json"]},
            ]},
            "deploy": {"params": []},
        }},
        "doctor": {"params": [{"flags": ["--json"]}]},
    },
}


# ── completion ───────────────────────────────────────────────────────────────


def test_top_level_completes_groups_and_builtins():
    cands = _completion_candidates(_TREE, [], "")
    assert "agents" in cands and "doctor" in cands
    assert {"cd", "exit", "help", "clear", "quit"} <= set(cands)


def test_prefix_filters_candidates():
    assert _completion_candidates(_TREE, [], "ag") == ["agents"]
    # 'e' matches the exit/help built-ins (and nothing else in this tree)
    assert "exit" in _completion_candidates(_TREE, [], "e")


def test_completes_subcommands_after_group():
    assert set(_completion_candidates(_TREE, ["agents"], "")) >= {"list", "deploy"}


def test_completes_flags_when_text_is_a_dash():
    cands = _completion_candidates(_TREE, ["agents", "list"], "--")
    assert set(cands) == {"--local", "--format", "--json"}


def test_flags_do_not_move_the_command_node():
    # a flag mid-line shouldn't break subcommand resolution for completion
    cands = _completion_candidates(_TREE, ["agents", "--quiet", "list"], "--")
    assert "--local" in cands


def test_real_tree_completes_known_commands():
    t = _command_tree()
    assert _completion_candidates(t, [], "age") == ["agents"]
    assert "--json" in _completion_candidates(t, ["agents", "list"], "--")


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_dispatch_runs_a_real_command(capsys):
    _dispatch("version")
    assert capsys.readouterr().out.strip()  # version printed something


def test_dispatch_survives_unknown_command(capsys):
    _dispatch("definitely-not-a-command")  # must not raise
    # ClickException.show() writes to stderr; the REPL stays alive either way.
    assert "No such command" in capsys.readouterr().err


def test_dispatch_handles_unbalanced_quotes(capsys):
    _dispatch('agents list "oops')  # shlex parse error — must not raise
    assert "parse error" in capsys.readouterr().out


def test_dispatch_empty_line_is_noop():
    _dispatch("   ")  # no exception, no output expected


# ── built-ins ────────────────────────────────────────────────────────────────


def test_cd_changes_directory_and_is_handled(tmp_path, monkeypatch):
    monkeypatch.chdir(Path.cwd())
    assert _handle_builtin(f"cd {tmp_path}") is True
    assert Path.cwd() == tmp_path.resolve()


def test_cd_bad_path_is_graceful(capsys):
    assert _handle_builtin("cd /no/such/dir/xyz") is True
    assert "cd:" in capsys.readouterr().out


def test_help_builtin(capsys):
    assert _handle_builtin("help") is True
    assert "apx-agent shell" in capsys.readouterr().out


def test_exit_raises_eoferror_to_break_loop():
    with pytest.raises(EOFError):
        _handle_builtin("exit")
    with pytest.raises(EOFError):
        _handle_builtin("quit")


def test_non_builtin_returns_false():
    assert _handle_builtin("status") is False


# ── non-TTY behaviour ────────────────────────────────────────────────────────


def test_shell_warns_and_still_runs_when_not_a_tty(monkeypatch, capsys):
    """Piped/redirected: print a clear note that interactive features are off,
    but still read + dispatch lines (scriptable)."""
    from apx_agent import _shell

    # Feed two lines then EOF; stdin/stdout are non-TTY under capsys by default.
    lines = iter(["version", "exit"])

    def fake_input(*_args) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    _shell.run_shell()

    out = capsys.readouterr().out
    assert "not a terminal" in out          # the clear note
    assert "interactive shell" in out       # banner still shown
    # `version` was actually dispatched (its output appears)
    assert any(ch.isdigit() for ch in out)


def test_shell_skips_readline_setup_when_not_a_tty(monkeypatch):
    # When non-interactive, completer setup must be skipped (readline has no
    # effect anyway). Assert set_completer is never called.
    from apx_agent import _shell

    called = {"completer": False}
    import readline

    monkeypatch.setattr(readline, "set_completer",
                        lambda *_a, **_k: called.__setitem__("completer", True))
    monkeypatch.setattr("builtins.input", lambda *_a: (_ for _ in ()).throw(EOFError))
    _shell.run_shell()
    assert called["completer"] is False
