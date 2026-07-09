"""
caps — the capability-verification layer for claude-test-kit.

Declares the capabilities a project promises (capabilities.yaml), proves them
against reality, and records proof in a committed ledger. Built on ctk.

Phase 1 (this package) is the manual runner: `python -m caps verify`.
"""

from .fingerprint import fingerprint
from .freshness import FreshnessError, parse_duration, waiver_active
from .ledger import LedgerEntry, load_ledger, save_ledger
from .manifest import Capability, ManifestError, load_manifest
from .runner import run_capability

__all__ = [
    "Capability",
    "load_manifest",
    "ManifestError",
    "LedgerEntry",
    "load_ledger",
    "save_ledger",
    "fingerprint",
    "parse_duration",
    "waiver_active",
    "FreshnessError",
    "run_capability",
]

__version__ = "0.1.0"
