#!/usr/bin/env python3
"""Pre-commit auto-fix: regenerate each changed .apx/okf bundle's sibling
schema.json cache so the committed cache never drifts from its source.

Invoked by pre-commit with the changed .apx/okf/**/*.md paths as argv. Exits 1
(and re-stages) when it rewrote a cache, 0 when everything was already in sync —
the pre-commit "fixer" convention.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "python" / "src"))
    from apx_agent._okf import okf_manifest

    changed = False
    seen: set[Path] = set()
    for arg in argv:
        p = Path(arg).resolve()
        okf_root = next((a for a in [p, *p.parents] if a.name == "okf" and a.parent.name == ".apx"), None)
        if okf_root is None or okf_root in seen:
            continue
        seen.add(okf_root)
        manifest = okf_manifest(okf_root)
        if manifest is None:
            continue
        cache = okf_root.parent / "schema.json"
        new = json.dumps(manifest, indent=2)
        if not cache.is_file() or cache.read_text() != new:
            cache.write_text(new)
            print(f"regenerated {cache}")
            changed = True
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
