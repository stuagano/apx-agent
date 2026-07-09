"""The over-engineering review rubric — the audit complement to the posture.

Ported from Ponytail's `ponytail-review` skill (https://github.com/
DietrichGebert/ponytail, MIT). Where `caps/ponytail.py` biases *authoring*
toward the smallest thing that works (the ceiling), this is the *audit*: point
it at a diff and it lists what to cut. The idea, not the Node.js machinery.

`caps review` prints this; an agent (or a reviewer) applies it to the diff
under review. Scope is complexity only — correctness and security route to a
normal review pass, not this one.
"""

from __future__ import annotations

RUBRIC = """\
PONYTAIL REVIEW — hunt the diff for unnecessary complexity, nothing else. One
line per finding: where it is, what to cut, what replaces it. The diff's best
outcome is getting shorter.

Format:
  L<line>: <tag> <what>. <replacement>.       (or <file>:L<line>: ... for multi-file)

Tags:
  delete:  dead code, unused flexibility, speculative feature. Replaces with nothing.
  stdlib:  hand-rolled thing the standard library ships. Name the function.
  native:  a dependency or code doing what the platform already does. Name the feature.
  yagni:   abstraction with one implementation, config nobody sets, layer with one caller.
  shrink:  same logic, fewer lines. Show the shorter form.

Examples:
  L12-38: stdlib: 27-line email validator. "@" in s, 1 line; real validation is the confirmation mail.
  L4: native: moment.js imported for one format call. Intl.DateTimeFormat, 0 deps.
  repo.py:L88: yagni: AbstractRepository with one implementation. Inline it until a second exists.
  L52-71: delete: retry wrapper around an idempotent local call. Nothing replaces it.
  L30-44: shrink: manual loop builds a dict. dict(zip(keys, values)), 1 line.

Scoring: end with the only metric that matters — `net: -<N> lines possible.`
If there is nothing to cut, say `Lean already. Ship.` and stop.

Boundaries: over-engineering only. Correctness bugs, security holes, and
performance are out of scope — route them to a normal review, not this one. A
single smoke test or one assert-based self-check is the minimum, not bloat;
never flag it for deletion. This lists findings; it does not apply them.
"""


def review_rubric() -> str:
    """The over-engineering review rubric, to apply to the diff under review."""
    return RUBRIC
