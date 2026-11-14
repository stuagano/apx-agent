"""The durable host turns on LangChain autolog at startup, like the other runtimes (#1185).

Without it a deployed durable agent records one ``graph.invoke`` span per turn
and no LLM or tool spans.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import mlflow
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from mlflow.utils.autologging_utils import autologging_is_disabled

from tests.test_runtime_targets import execution  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_autolog() -> Iterator[None]:
    mlflow.langchain.autolog(disable=True)
    yield
    mlflow.langchain.autolog(disable=True)


def _start(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: F811
    from apx_agent._durable_agent import compile_to_durable_agent_server

    (tmp_path / "pyproject.toml").write_text('[tool.apx.deploy]\nname = "proof"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"file://{tmp_path}/mlruns")
    app = compile_to_durable_agent_server(execution.agent, model="test", service_ws=execution.ws,
                                          checkpointer=InMemorySaver())
    assert autologging_is_disabled("langchain")  # compiling alone changes nothing
    with TestClient(app):
        pass


@pytest.mark.unit
def test_durable_start_enables_langchain_autolog(execution: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                 tmp_path: Path) -> None:
    monkeypatch.delenv("APX_AGENT_MLFLOW_AUTOLOG", raising=False)
    _start(execution, monkeypatch, tmp_path)
    assert not autologging_is_disabled("langchain")


@pytest.mark.unit
def test_durable_start_honors_autolog_opt_out(execution: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                              tmp_path: Path) -> None:
    monkeypatch.setenv("APX_AGENT_MLFLOW_AUTOLOG", "0")
    _start(execution, monkeypatch, tmp_path)
    assert autologging_is_disabled("langchain")
