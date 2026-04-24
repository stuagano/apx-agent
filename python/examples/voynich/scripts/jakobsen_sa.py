"""
Jakobsen-style simulated annealing solver for monoalphabetic substitution
ciphers, adapted to the EVA glyph alphabet of the Voynich manuscript.

Algorithm
---------
Jakobsen (1995) is the standard fast solver for monoalphabetic substitution.
The classical version is a deterministic hill-climb that swaps adjacent rows
in a key matrix. We use the more general SA variant: at each step propose a
random swap of two key entries, accept improvements always, accept worse keys
with probability `exp(delta / T)`, and cool the temperature on a schedule.
This escapes local maxima the pure hill-climb gets stuck in, especially when
the cipher alphabet (~23 EVA glyphs) doesn't match the plaintext alphabet
(22-28 letters depending on language).

Convergence: a real monoalphabetic substitution converges in 10-30k swaps,
typically under one second of CPU time. If multiple restarts all converge to
the same key, you've found the substitution. If restarts diverge to wildly
different keys with similar scores, the cipher isn't monoalphabetic.

Score
-----
Provided by `ngram_model.LangModel.score()` — sum of bigram log-probabilities
over the decoded character stream. Higher is better.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field

from .ngram_model import LangModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def index_of_coincidence(tokens: list[str]) -> float:
    """Friedman's IC. Plaintext languages typically score 0.06-0.075;
    random text scores ~1/N where N is alphabet size. The Voynich manuscript
    famously scores ~0.08, which is in-range for natural language but the
    repeat structure (e.g. `qokeedy qokeedy ykeedy ...`) is not."""
    if len(tokens) < 2:
        return 0.0
    counts = Counter(tokens)
    n = len(tokens)
    numerator = sum(c * (c - 1) for c in counts.values())
    denominator = n * (n - 1)
    return numerator / denominator if denominator else 0.0


def repeat_rate(tokens: list[str]) -> float:
    """Fraction of adjacent token pairs that are identical. Natural-language
    text scores ~0.001 (rare repeats like 'had had'). Voynich scores 5-10x
    higher in some sections — another signal that monoalphabetic substitution
    of natural-language plaintext is the wrong model."""
    if len(tokens) < 2:
        return 0.0
    repeats = sum(1 for i in range(len(tokens) - 1) if tokens[i] == tokens[i + 1])
    return repeats / (len(tokens) - 1)


# ---------------------------------------------------------------------------
# Key representation
# ---------------------------------------------------------------------------


@dataclass
class Key:
    """A bijective mapping cipher_glyph -> plaintext_letter.

    The cipher alphabet is the set of distinct EVA glyphs in the section.
    The plaintext alphabet is the top-N most-frequent letters of the target
    language, where N == len(cipher alphabet).
    """

    glyphs: tuple[str, ...]  # cipher symbols, ordered
    letters: list[str]       # plaintext letter at position i decodes glyphs[i]

    def mapping(self) -> dict[str, str]:
        return dict(zip(self.glyphs, self.letters))

    def decode(self, tokens: list[str]) -> str:
        m = self.mapping()
        # Unknown glyphs (shouldn't happen if alphabet was built from this
        # token stream) become '?' — penalised by the bigram floor.
        return "".join(m.get(t, "?") for t in tokens)

    def copy(self) -> "Key":
        return Key(self.glyphs, list(self.letters))


def initial_key(glyphs: tuple[str, ...], model: LangModel) -> Key:
    """Frequency-aligned starting key: the most-common cipher glyph maps to
    the most-common plaintext letter, etc. This is Jakobsen's standard seed."""
    letters = list(model.alphabet[: len(glyphs)])
    # If language alphabet is smaller than cipher alphabet, pad with the
    # rarest letters cycling.
    while len(letters) < len(glyphs):
        letters.append(model.alphabet[-1])
    return Key(glyphs, letters)


# ---------------------------------------------------------------------------
# Simulated annealing
# ---------------------------------------------------------------------------


@dataclass
class SAConfig:
    iterations: int = 20_000
    t_start: float = 10.0
    t_end: float = 0.01
    restarts: int = 4
    seed: int | None = None


@dataclass
class SAResult:
    language: str
    section: str | None
    best_key: dict[str, str]
    best_score: float
    decoded_sample: str
    n_glyphs: int
    n_tokens: int
    ic: float
    repeat_rate: float
    restart_scores: list[float] = field(default_factory=list)
    converged: bool = False  # all restarts agree on the same top key
    notes: str = ""


def _temperature(step: int, total: int, t_start: float, t_end: float) -> float:
    # Geometric (exponential) cooling
    if total <= 1:
        return t_end
    ratio = t_end / t_start
    return t_start * (ratio ** (step / (total - 1)))


def jakobsen_sa(
    tokens: list[str],
    model: LangModel,
    config: SAConfig | None = None,
) -> SAResult:
    """Run Jakobsen SA against a tokenized EVA glyph stream."""
    cfg = config or SAConfig()
    rng = random.Random(cfg.seed)

    glyphs = tuple(sorted({t for t in tokens}, key=lambda g: -tokens.count(g)))
    n = len(glyphs)
    if n < 2:
        return SAResult(
            language=model.name, section=None,
            best_key={}, best_score=0.0, decoded_sample="",
            n_glyphs=n, n_tokens=len(tokens),
            ic=0.0, repeat_rate=0.0,
            notes="alphabet too small to attack",
        )

    ic = index_of_coincidence(tokens)
    rr = repeat_rate(tokens)

    best_overall: Key | None = None
    best_overall_score = -math.inf
    restart_scores: list[float] = []
    top_keys: list[tuple[str, ...]] = []

    for restart in range(cfg.restarts):
        # Seed each restart slightly differently
        restart_rng = random.Random((cfg.seed or 0) + restart * 7919)
        key = initial_key(glyphs, model)
        # Perturb the seed key for restarts > 0 so they explore different basins
        if restart > 0:
            for _ in range(restart * 2):
                i, j = restart_rng.sample(range(n), 2)
                key.letters[i], key.letters[j] = key.letters[j], key.letters[i]

        current_score = model.score(key.decode(tokens))
        best_local = key.copy()
        best_local_score = current_score

        for step in range(cfg.iterations):
            T = _temperature(step, cfg.iterations, cfg.t_start, cfg.t_end)
            i, j = restart_rng.sample(range(n), 2)
            key.letters[i], key.letters[j] = key.letters[j], key.letters[i]
            new_score = model.score(key.decode(tokens))
            delta = new_score - current_score
            if delta > 0 or restart_rng.random() < math.exp(delta / max(T, 1e-9)):
                current_score = new_score
                if new_score > best_local_score:
                    best_local_score = new_score
                    best_local = key.copy()
            else:
                # reject: undo swap
                key.letters[i], key.letters[j] = key.letters[j], key.letters[i]

        restart_scores.append(best_local_score)
        top_keys.append(tuple(best_local.letters))
        if best_local_score > best_overall_score:
            best_overall_score = best_local_score
            best_overall = best_local

    assert best_overall is not None

    # Convergence test: do at least half the restarts agree on the same key?
    key_counts = Counter(top_keys)
    most_common, count = key_counts.most_common(1)[0]
    converged = count >= max(2, cfg.restarts // 2 + 1)

    decoded = best_overall.decode(tokens)
    sample = decoded[:200]

    notes_parts: list[str] = []
    if ic < 0.045:
        notes_parts.append(f"low IC ({ic:.3f}) — flatter than natural language")
    elif ic > 0.085:
        notes_parts.append(f"high IC ({ic:.3f}) — more peaked than natural language")
    if rr > 0.01:
        notes_parts.append(f"high adjacent-repeat rate ({rr:.3f}) — atypical for plaintext")
    if not converged:
        notes_parts.append("restarts disagreed on best key — no stable optimum")

    return SAResult(
        language=model.name,
        section=None,
        best_key=best_overall.mapping(),
        best_score=best_overall_score,
        decoded_sample=sample,
        n_glyphs=n,
        n_tokens=len(tokens),
        ic=ic,
        repeat_rate=rr,
        restart_scores=restart_scores,
        converged=converged,
        notes="; ".join(notes_parts) if notes_parts else "ok",
    )


# ---------------------------------------------------------------------------
# Verdict heuristic
# ---------------------------------------------------------------------------


def per_token_score(result: SAResult) -> float:
    """Normalize score by token count so different sections are comparable."""
    if result.n_tokens < 2:
        return 0.0
    return result.best_score / (result.n_tokens - 1)


def monoalphabetic_verdict(results: list[SAResult]) -> str:
    """Given the best SA result across {language} candidates for one section,
    decide whether monoalphabetic substitution is plausible.

    Heuristic: a real monoalphabetic substitution should produce
    (a) per-token score better than ~-3.5 (typical real-text bigram log-prob),
    (b) restart convergence on a single key,
    (c) IC and repeat-rate roughly in plaintext range.
    """
    if not results:
        return "no data"
    best = max(results, key=per_token_score)
    pts = per_token_score(best)

    if pts > -3.5 and best.converged and best.repeat_rate < 0.005:
        return f"PLAUSIBLE — {best.language} key converged at per-token score {pts:.2f}"
    if pts > -4.5 and best.converged:
        return f"WEAK — {best.language} converged but score {pts:.2f} below natural-text range"
    if not best.converged:
        return (
            f"REJECTED — restarts diverged across all languages "
            f"(best per-token {pts:.2f}, {best.language}); "
            f"cipher is unlikely to be monoalphabetic"
        )
    return f"REJECTED — best per-token score {pts:.2f} is far below natural-text range"
