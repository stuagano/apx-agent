"""
Strict subprocess runner.

The whole point: a process exiting 0 is NOT proof it worked. This runner forces
you to assert on the exit code *and* the output, and it surfaces stderr that
looks like an error even when the exit code is 0 (the classic "exit 0 but wrong
output" trap).
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Patterns in stderr that usually mean something went wrong even if exit==0.
_ERROR_MARKERS = re.compile(
    r"\b(Traceback|Error|Exception|FATAL|CRITICAL|Segmentation fault|"
    r"command not found|No such file|Permission denied)\b",
    re.IGNORECASE,
)


class RunFailure(AssertionError):
    """Raised when a strict assertion on a RunResult fails."""


@dataclass
class RunResult:
    cmd: Sequence[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    # ----- exit code -----
    def ok(self) -> RunResult:
        """Assert the process exited 0. Fails loudly with full output."""
        if self.returncode != 0:
            raise RunFailure(self._report(f"expected exit 0, got {self.returncode}"))
        return self

    def code(self, expected: int) -> RunResult:
        if self.returncode != expected:
            raise RunFailure(self._report(f"expected exit {expected}, got {self.returncode}"))
        return self

    def failed(self) -> RunResult:
        """Assert the process did NOT exit 0 (for testing your error paths)."""
        if self.returncode == 0:
            raise RunFailure(self._report("expected non-zero exit, got 0"))
        return self

    # ----- stderr / error sniffing -----
    def no_stderr_errors(self) -> RunResult:
        """
        Fail if stderr contains error-looking text, even when exit==0.
        Catches scripts that print a traceback and then exit 0 anyway.
        """
        m = _ERROR_MARKERS.search(self.stderr or "")
        if m:
            raise RunFailure(self._report(f"stderr looks like an error (matched {m.group(0)!r})"))
        return self

    def silent_stderr(self) -> RunResult:
        """Assert stderr is completely empty."""
        if (self.stderr or "").strip():
            raise RunFailure(self._report("expected empty stderr"))
        return self

    # ----- stdout content -----
    def out_has(self, substring: str) -> RunResult:
        if substring not in self.stdout:
            raise RunFailure(self._report(f"stdout missing substring {substring!r}"))
        return self

    def out_matches(self, pattern: str) -> RunResult:
        if not re.search(pattern, self.stdout):
            raise RunFailure(self._report(f"stdout did not match /{pattern}/"))
        return self

    def out_nonempty(self) -> RunResult:
        if not self.stdout.strip():
            raise RunFailure(self._report("stdout was empty (success-looking but empty)"))
        return self

    def json(self) -> object:
        """Parse stdout as JSON, failing loudly if it isn't valid JSON."""
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as e:
            raise RunFailure(self._report(f"stdout was not valid JSON: {e}")) from e

    # ----- reporting -----
    def _report(self, headline: str) -> str:
        cmd = " ".join(shlex.quote(str(c)) for c in self.cmd)
        out = self.stdout if len(self.stdout) < 4000 else self.stdout[:4000] + "\n...[truncated]"
        err = self.stderr if len(self.stderr) < 4000 else self.stderr[:4000] + "\n...[truncated]"
        return (
            f"\n=== RUN FAILED: {headline} ===\n"
            f"$ {cmd}\n"
            f"exit={self.returncode}  duration={self.duration:.2f}s  timed_out={self.timed_out}\n"
            f"--- stdout ---\n{out or '(empty)'}\n"
            f"--- stderr ---\n{err or '(empty)'}\n"
            f"==============================="
        )


def run(
    cmd: str | Sequence[str],
    *,
    input: str | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> RunResult:
    """
    Run a command and capture everything. Does NOT assert anything by itself —
    call .ok() / .out_has() / etc. on the result so the assertion is explicit.

    Pass a list (preferred) or a string (split with shlex).
    """
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    cmd = [str(c) for c in cmd]

    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            input=input,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = 124
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (
            e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        ) + f"\n[ctk] timed out after {timeout}s"
    duration = time.monotonic() - start

    return RunResult(
        cmd=cmd,
        returncode=rc,
        stdout=out or "",
        stderr=err or "",
        duration=duration,
        timed_out=timed_out,
    )
