"""
Run Jakobsen simulated annealing on each Voynich section against each
candidate language model. Writes a results JSON file and a human-readable
report.

Usage
-----
    python -m scripts.run_analysis
    python -m scripts.run_analysis --iterations 50000 --restarts 6
    python -m scripts.run_analysis --section herbal --language latin

Run from `python/examples/voynich/`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .eva_sections import SECTIONS, tokenize
from .jakobsen_sa import (
    SAConfig,
    jakobsen_sa,
    monoalphabetic_verdict,
    per_token_score,
)
from .ngram_model import all_models, latin_model, hebrew_model, arabic_model


MODEL_FACTORIES = {
    "latin": latin_model,
    "hebrew": hebrew_model,
    "arabic": arabic_model,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jakobsen SA on Voynich sections")
    p.add_argument("--iterations", type=int, default=20_000)
    p.add_argument("--restarts", type=int, default=4)
    p.add_argument("--t-start", type=float, default=10.0)
    p.add_argument("--t-end", type=float, default=0.01)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--section", type=str, default=None,
                   help="restrict to one section (default: all)")
    p.add_argument("--language", type=str, default=None,
                   choices=list(MODEL_FACTORIES),
                   help="restrict to one language (default: all)")
    p.add_argument("--out", type=Path, default=Path("sa_results.json"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = SAConfig(
        iterations=args.iterations,
        t_start=args.t_start,
        t_end=args.t_end,
        restarts=args.restarts,
        seed=args.seed,
    )

    sections = [args.section] if args.section else list(SECTIONS)
    if args.language:
        models = [MODEL_FACTORIES[args.language]()]
    else:
        models = all_models()

    all_results: dict[str, list] = {}

    for section in sections:
        if section not in SECTIONS:
            print(f"unknown section: {section}", file=sys.stderr)
            return 2
        tokens = tokenize(SECTIONS[section])
        print(f"\n=== {section} ({len(tokens)} tokens, "
              f"{len(set(tokens))} distinct glyphs) ===")
        section_results = []
        for model in models:
            t0 = time.time()
            result = jakobsen_sa(tokens, model, cfg)
            result.section = section
            elapsed = time.time() - t0
            pts = per_token_score(result)
            print(
                f"  [{model.name:7s}] score={result.best_score:>10.1f}  "
                f"per-tok={pts:>+6.2f}  ic={result.ic:.3f}  "
                f"rr={result.repeat_rate:.3f}  "
                f"converged={'Y' if result.converged else 'N'}  "
                f"({elapsed:.1f}s)"
            )
            print(f"           decoded: {result.decoded_sample[:80]!r}")
            print(f"           note: {result.notes}")
            section_results.append(result)

        verdict = monoalphabetic_verdict(section_results)
        print(f"  → verdict: {verdict}")
        all_results[section] = [
            {
                "language": r.language,
                "best_score": r.best_score,
                "per_token_score": per_token_score(r),
                "ic": r.ic,
                "repeat_rate": r.repeat_rate,
                "n_tokens": r.n_tokens,
                "n_glyphs": r.n_glyphs,
                "converged": r.converged,
                "restart_scores": r.restart_scores,
                "best_key": r.best_key,
                "decoded_sample": r.decoded_sample,
                "notes": r.notes,
            }
            for r in section_results
        ] + [{"verdict": verdict}]

    args.out.write_text(json.dumps(all_results, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
