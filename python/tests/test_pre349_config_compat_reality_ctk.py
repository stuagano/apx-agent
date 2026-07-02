"""Claim-vs-reality: pre-#349 configs (with `instance_name`) must not crash boot.

#349 dropped the `instance_name` field from the three backend config classes
(Session/Memory/Example) but kept `extra="forbid"`, so every agent scaffolded
before #349 — whose `[tool.apx.agent.session]` block still carries
`instance_name = "apx-agent"` — now fails to parse with a raw pydantic
`ValidationError` at boot (the serve path calls `_load_agent_config` with no
try/except). #343 had explicitly kept a compat guard for exactly this reason.

The fix tolerates the obsolete key with a deprecation warning instead of
rejecting it — without resurrecting the field (so real typos still fail
`extra="forbid"`). This reconciles the *claim* ("old projects still boot")
against reality: the configs parse, the obsolete key is gone from the model,
and a genuine typo still raises.

Also covers the stale `bootstrap.__all__` export of `_ensure_lakebase_instance`
(deleted by #349), which breaks `from apx_agent.bootstrap import *`.
"""

from __future__ import annotations

import pytest

from apx_agent import (
    ExampleBackendConfig,
    MemoryBackendConfig,
    SessionBackendConfig,
)

_BACKEND_CLASSES = [
    pytest.param(SessionBackendConfig, id="session"),
    pytest.param(MemoryBackendConfig, id="memory"),
    pytest.param(ExampleBackendConfig, id="example"),
]


@pytest.mark.unit
@pytest.mark.parametrize("cls", _BACKEND_CLASSES)
def test_obsolete_instance_name_is_tolerated_not_rejected(cls, caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        cfg = cls.model_validate({"type": "lakebase", "instance_name": "apx-agent"})
    # Parses (no ValidationError), the obsolete key is dropped from the model,
    # and the deprecation is surfaced.
    assert cfg.type == "lakebase"
    assert not hasattr(cfg, "instance_name")
    assert "instance_name" in caplog.text.lower()


@pytest.mark.unit
@pytest.mark.parametrize("cls", _BACKEND_CLASSES)
def test_real_typos_still_rejected(cls) -> None:
    from pydantic import ValidationError

    # The compat shim must not turn extra="forbid" off for genuine mistakes.
    with pytest.raises(ValidationError):
        cls.model_validate({"type": "lakebase", "instancename": "typo"})


@pytest.mark.unit
def test_pre349_pyproject_loads_without_raising(tmp_path) -> None:
    from apx_agent._inspection import _load_agent_config

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "\n".join(
            [
                "[tool.apx.agent]",
                'name = "legacy-agent"',
                'model = "databricks-claude-sonnet-4-6"',
                "",
                "[tool.apx.agent.session]",
                'type = "lakebase"',
                'instance_name = "apx-agent"',  # the pre-#349 scaffold default
                'host = "${LAKEBASE_HOST}"',
                'database = "databricks_postgres"',
            ]
        )
    )
    cfg = _load_agent_config(pyproject_path=pyproject)
    assert cfg is not None
    assert cfg.name == "legacy-agent"
    assert cfg.session is not None and cfg.session.type == "lakebase"
    assert not hasattr(cfg.session, "instance_name")


@pytest.mark.unit
def test_bootstrap_all_has_no_dangling_export() -> None:
    from apx_agent import bootstrap

    missing = [name for name in bootstrap.__all__ if not hasattr(bootstrap, name)]
    assert not missing, f"bootstrap.__all__ names undefined symbols: {missing}"
