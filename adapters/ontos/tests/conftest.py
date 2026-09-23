"""Test-local stubs for heavy optional deps of the ontos adapter.

The adapter's import chain (router → providers → databricks.sql, plus
ontos_sync → pyspark / databricks.sdk) requires packages that are not
runtime deps of apx-agent. Where the real package is not importable,
register a minimal stub so the adapter modules import cleanly in tests.

When the real parent package IS installed (e.g. the Databricks SDK),
the stub submodule is also set as an attribute on it so
``from databricks import sql`` resolves without the SQL connector.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _install_submodule_stub(parent: str, child: str, **attrs: object) -> None:
    fullname = f"{parent}.{child}"
    if fullname in sys.modules:
        return
    try:
        __import__(fullname)
        return  # real submodule importable — no stub needed
    except ImportError:
        pass
    stub = ModuleType(fullname)
    for key, value in attrs.items():
        setattr(stub, key, value)
    sys.modules[fullname] = stub
    parent_mod = sys.modules.get(parent)
    if parent_mod is not None:
        setattr(parent_mod, child, stub)


_install_submodule_stub("databricks", "sql", connect=MagicMock)
_install_submodule_stub("databricks", "sdk", WorkspaceClient=MagicMock)
_install_submodule_stub("pyspark", "sql", SparkSession=MagicMock)
