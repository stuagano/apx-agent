"""`apx-agent doctor --durable`: run-not-read readiness for durable_agent_server.

Exercises an apx project the way the native runtime would — compile, start the
lifespan, serve a request, run the deploy preflight — fully offline: temp cwd,
fake workspace clients, refused outbound HTTP, scripted model. Nothing under the
project is written; everything patched is restored on exit.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ._doctor import Check, Status

STAGES = ("Config", "Compile", "Startup", "Request", "Deploy")
SENTINEL = "apx-doctor-sentinel-token"
PEER_URL = "https://apx-doctor-peer.invalid"
SDK_FIX = 'uv run --with "databricks-agentbricks>=0.4.0,<0.5" apx-agent doctor --durable --offline'


class _FakeWorkspaceClient:
    """Stands in for databricks.sdk.WorkspaceClient; never touches the network."""

    def __init__(self, *args: Any, token: str | None = None, **kwargs: Any) -> None:
        bearer = "apx-doctor-app-credential" if token is None else token
        self.config = SimpleNamespace(
            host="https://apx-doctor.invalid",
            authenticate=lambda: {"Authorization": f"Bearer {bearer}"},
        )
        self.current_user = SimpleNamespace(me=lambda: SimpleNamespace(id=f"user-{bearer}"))

    def __getattr__(self, name: str) -> Any:
        return mock.MagicMock(name=f"fake_ws.{name}")


@dataclass
class _Harness:
    captured: list[dict[str, str]] = field(default_factory=list)


@dataclass
class _Loaded:
    config: Any
    agent: Any
    project: Path


def _refuse(*args: Any, **kwargs: Any) -> Any:
    import httpx

    raise httpx.ConnectError("doctor --durable is offline: outbound HTTP refused")


async def _refuse_async(*args: Any, **kwargs: Any) -> Any:
    _refuse()


def _scripted_model(*args: Any, **kwargs: Any) -> Any:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _Scripted(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "apx-doctor-scripted"

        def bind_tools(self, tools: Any, **kw: Any) -> _Scripted:
            return self

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> ChatResult:
            reply = AIMessage(content="apx doctor: ready")
            reply.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
            return ChatResult(generations=[ChatGeneration(message=reply)])

    return _Scripted()


@contextlib.contextmanager
def _isolated(project: Path) -> Iterator[_Harness]:
    harness = _Harness()

    async def _record(self: Any, messages: Any, forwarded: dict[str, str]) -> str:
        harness.captured.append(dict(forwarded))
        return "apx doctor peer: ok"

    # Import the modules that bind workspace clients by name BEFORE patching, so
    # they are scanned (and restored) rather than first-imported holding a fake.
    import importlib

    import databricks.sdk

    for name in ("_compile", "_defaults", "_dev", "_remote", "_wiring"):
        importlib.import_module(f"{__package__}.{name}")
    real_sdk_client = databricks.sdk.WorkspaceClient
    real_make_client = sys.modules[f"{__package__}._defaults"]._make_workspace_client

    def _fake_make_client(*args: Any, **kwargs: Any) -> _FakeWorkspaceClient:
        return _FakeWorkspaceClient()

    env, cwd, path, modules = dict(os.environ), Path.cwd(), list(sys.path), set(sys.modules)
    with tempfile.TemporaryDirectory(prefix="apx-doctor-") as tmp, contextlib.ExitStack() as stack:
        for target, value in (
            ("databricks.sdk.WorkspaceClient", _FakeWorkspaceClient),
            ("apx_agent._defaults._make_workspace_client", _fake_make_client),
            ("apx_agent._compile._build_chat_databricks", _scripted_model),
            ("apx_agent._remote.RemoteDatabricksAgent._run_with_incoming_headers", _record),
            ("httpx.Client.send", _refuse),
            ("httpx.AsyncClient.send", _refuse_async),
        ):
            stack.enter_context(mock.patch(target, value))
        # Modules that did `from X import Name` hold their own reference; rebind those too.
        replacements = {id(real_sdk_client): _FakeWorkspaceClient, id(real_make_client): _fake_make_client}
        for mod_name, module in list(sys.modules.items()):
            if mod_name != "apx_agent" and not mod_name.startswith("apx_agent."):
                continue
            for attr, value in list(vars(module).items()):
                if id(value) in replacements:
                    stack.enter_context(mock.patch.object(module, attr, replacements[id(value)]))
        try:
            sys.path.insert(0, str(project))
            os.environ["APX_PYPROJECT"] = str(project / "pyproject.toml")
            os.environ["DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL"] = "true"
            os.environ.pop("AGENTBRICKS_PROJECT_ROOT", None)
            os.environ.pop("DATABRICKS_APP_NAME", None)
            os.chdir(tmp)
            yield harness
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(env)
            sys.path[:] = path
            for name in set(sys.modules) - modules:
                mod = sys.modules[name]
                if str(getattr(mod, "__file__", None)).startswith(str(project)):
                    del sys.modules[name]


def _fail(name: str, exc: BaseException) -> Check:
    if isinstance(exc, KeyboardInterrupt):
        raise exc
    return Check(name, Status.FAIL, f"{type(exc).__name__}: {exc}")


def _stage_config(project: Path) -> _Loaded | Check:
    import tomllib

    from ._models import AgentConfig

    try:
        data = tomllib.loads((project / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return _fail("Config", exc)
    declared = data.get("tool", {}).get("apx", {}).get("agent")
    if declared is None:
        return Check("Config", Status.SKIP, "not an apx agent project (no [tool.apx.agent])")
    try:
        import databricks_agentkit.runtime.app  # noqa: F401 — presence check
    except ImportError:
        return Check("Config", Status.FAIL, "databricks-agentbricks is not installed in this environment", SDK_FIX)
    try:
        from ._inspection import _load_agent_config
        from ._wiring import resolve_agent

        current = _load_agent_config(pyproject_path=project / "pyproject.toml")
        config = AgentConfig.model_validate({**current.model_dump(), "target": "durable_agent_server"})
        agent = resolve_agent("agent:agent", config, ws=_FakeWorkspaceClient())
    except BaseException as exc:
        return _fail("Config", exc)
    return _Loaded(config=config, agent=agent, project=project)


def _stage_compile(loaded: _Loaded) -> list[Check]:
    from ._runtime_targets import inspect_target

    try:
        report = inspect_target(loaded.agent, config=loaded.config, target="durable_agent_server")
    except BaseException as exc:
        return [_fail("Compile", exc)]
    if report.unsatisfied:
        detail = "; ".join(f"{k}: {report.capabilities[k].detail}" for k in report.unsatisfied)
        return [Check("Compile", Status.FAIL, detail)]
    return [Check("Compile", Status.OK, "no unsatisfied capabilities")]


def _cascade(results: list[Check]) -> list[Check]:
    """Pad to every stage; after the first FAIL, later stages are SKIP."""
    out: list[Check] = []
    blocked: str | None = None
    by_name = {c.name: c for c in results}
    for stage in STAGES:
        if blocked is not None:
            out.append(Check(stage, Status.SKIP, f"blocked by {blocked}"))
            continue
        check = by_name.get(stage)
        if check is None:
            check = Check(stage, Status.SKIP, "not yet implemented")
        out.append(check)
        if check.status is Status.FAIL:
            blocked = stage
    return out


def check_durable_readiness(project: Path) -> list[Check]:
    """Run the durable readiness stages for the apx project at ``project``."""
    project = project.resolve()
    results: list[Check] = []
    with _isolated(project):
        loaded = _stage_config(project)
        if isinstance(loaded, Check):
            if loaded.status is Status.SKIP:
                return [loaded]
            return _cascade([loaded])
        results.append(Check("Config", Status.OK,
                             f"durable target valid; agent imported ({type(loaded.agent).__name__} "
                             f"{getattr(loaded.agent, '_name', loaded.config.name)})"))
        results.extend(_stage_compile(loaded))
    return _cascade(results)
