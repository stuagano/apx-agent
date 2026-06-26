"""apx-agent shell — the interactive REPL. The readline loop is thin; the
testable logic lives in pure functions (completion, dispatch, built-ins)."""
from __future__ import annotations

import re
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


# ── bare `apx-agent` → shell (interactive) / help (non-TTY) ───────────────────


def test_bare_invocation_non_tty_shows_help_not_shell(monkeypatch):
    # CliRunner stdio is non-TTY → keep the safe help-on-no-args behaviour.
    from click.testing import CliRunner

    import apx_agent.cli as climod

    result = CliRunner().invoke(climod.main, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output  # help, not the shell banner
    assert "Commands:" in result.output
    assert "interactive shell ·" not in result.output  # shell banner absent


def test_bare_invocation_interactive_launches_shell(monkeypatch):
    from unittest.mock import patch

    from click.testing import CliRunner

    import apx_agent.cli as climod

    with patch.object(climod, "_stdio_is_interactive", return_value=True), \
            patch("apx_agent._shell.run_shell") as rs:
        result = CliRunner().invoke(climod.main, [])
    assert result.exit_code == 0
    assert rs.called


def test_subcommand_still_runs_with_invoke_without_command(monkeypatch):
    # A real subcommand must dispatch normally (the group callback returns early).
    from unittest.mock import patch

    from click.testing import CliRunner

    import apx_agent.cli as climod

    with patch.object(climod, "_stdio_is_interactive", return_value=True), \
            patch("apx_agent._shell.run_shell") as rs:
        result = CliRunner().invoke(climod.main, ["version"])
    assert result.exit_code == 0
    assert not rs.called          # shell NOT launched when a command is given
    assert result.output.strip()  # version printed


# ── color ────────────────────────────────────────────────────────────────────


def test_color_off_when_not_a_tty(monkeypatch):
    from apx_agent import _shell

    monkeypatch.setattr(_shell.sys.stdout, "isatty", lambda: False)
    assert _shell._color_on() is False


def test_color_respects_no_color_env(monkeypatch):
    from apx_agent import _shell

    monkeypatch.setattr(_shell.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    assert _shell._color_on() is False


def test_colored_prompt_is_readline_safe(monkeypatch):
    from apx_agent import _shell

    monkeypatch.setattr(_shell, "_color_on", lambda: True)
    p = _shell._prompt()
    assert "\x1b[" in p                      # actually colored
    assert "\001" in p and "\002" in p
    # EVERY ANSI escape (segmented prompt repeats them) is bracketed for readline
    for m in re.finditer(r"\x1b\[[0-9;]*m", p):
        assert p[m.start() - 1] == "\001" and p[m.end()] == "\002"


def test_segmented_prompt_colors_profile_separately(monkeypatch):
    from apx_agent import _shell

    monkeypatch.setattr(_shell, "_color_on", lambda: True)
    monkeypatch.setattr("apx_agent.cli._status_prompt_string",
                        lambda _cwd: "apx:demo(apps) ▸ fe-stable")
    p = _shell._prompt()
    # context, the ▸ separator, the profile, and ❯ are all present...
    assert "apx:demo(apps)" in p and "fe-stable" in p and "❯" in p
    # ...and distinctly colored (more than one fg code in play)
    assert len(set(re.findall(r"\x1b\[(\d+)m", p))) >= 3
    for m in re.finditer(r"\x1b\[[0-9;]*m", p):  # still readline-safe
        assert p[m.start() - 1] == "\001" and p[m.end()] == "\002"


def test_plain_prompt_has_no_escapes_when_color_off(monkeypatch):
    from apx_agent import _shell

    monkeypatch.setattr(_shell, "_color_on", lambda: False)
    p = _shell._prompt()
    assert "\x1b" not in p and "\001" not in p
    assert "❯" in p


def test_banner_lines_have_wordmark_and_hint():
    from apx_agent import _shell

    lines = _shell._banner_lines()
    text = "".join(lines)
    assert "⬡ apx-agent" in text   # on-brand wordmark
    assert "v" in text             # version stamp
    assert "help" in text and "Ctrl-D" in text  # the hint
    # version is trimmed of the long +g<sha> build suffix
    assert "+g" not in text
