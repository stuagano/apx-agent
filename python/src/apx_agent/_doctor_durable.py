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
from collections.abc import Callable, Iterator
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


def _disable_tracing(stack: contextlib.ExitStack) -> None:
    """Turn MLflow tracing off for the run (no trace export), restoring it on exit.

    Restore uses ``reset`` (lazy re-init on next use), not ``enable``: ``enable``
    would eagerly build the default sqlite tracking store in the caller's cwd.
    """
    try:
        from mlflow.tracing import disable, reset
        from mlflow.tracing.provider import is_tracing_enabled
    except ImportError:
        return
    if is_tracing_enabled():
        disable()
        stack.callback(reset)


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
            ("httpx.HTTPTransport.handle_request", _refuse),
            ("httpx.AsyncHTTPTransport.handle_async_request", _refuse_async),
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
        _disable_tracing(stack)
        try:
            sys.path.insert(0, str(project))
            os.environ["APX_PYPROJECT"] = str(project / "pyproject.toml")
            os.environ["DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL"] = "true"
            os.environ.pop("AGENTBRICKS_PROJECT_ROOT", None)
            os.environ["APX_AGENT_MLFLOW_AUTOLOG"] = "0"
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


_SURFACE_CALLS = {"include_router", "add_middleware", "mount", "mount_mcp_endpoints"}
_ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "websocket", "api_route"}


def _entrypoint_file(project: Path) -> Path | None:
    import yaml

    bundle = project / "databricks.yml"
    if bundle.exists():
        doc = yaml.safe_load(bundle.read_text())
        apps = doc.get("resources", {}).get("apps", {}) if isinstance(doc, dict) else {}
        for app in apps.values() if isinstance(apps, dict) else []:
            config = app.get("config", {}) if isinstance(app, dict) else {}
            command = config.get("command", []) if isinstance(config, dict) else []
            for part in [command] if isinstance(command, str) else command:
                module = str(part).split(":")[0]
                candidate = project / (module.replace(".", "/") + ".py")
                if module and candidate.exists():
                    return candidate
    for rel in ("app.py", "agent_server/start_server.py"):
        if (project / rel).exists():
            return project / rel
    return None


def _entrypoint_surface(project: Path) -> list[str]:
    """Hand-added FastAPI surface in the current entrypoint (static scan)."""
    import ast

    path = _entrypoint_file(project)
    if path is None:
        return []
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name in _SURFACE_CALLS:
                arg = ast.unparse(node.args[0]) if node.args else ""
                found.append(f"{name}({arg})")
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Attribute) and target.attr in _ROUTE_VERBS:
                    found.append(f"@{ast.unparse(target)} {node.name}")
    return found


@dataclass
class _Started:
    app: Any
    client: Any
    mcp: bool


def _stage_startup(loaded: _Loaded, surface: Callable[[], list[str]], stack: contextlib.ExitStack) -> _Started | Check:
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver

    from ._runtime_targets import compile_agent

    try:
        # A hand-written entrypoint that cannot be parsed is a Startup failure.
        mcp = any(s.startswith("mount_mcp_endpoints") for s in surface())
        # The managed Session Store is provisioned at deploy; substitute memory.
        config = loaded.config.model_copy(update={"session": None})
        checkpointer = InMemorySaver() if type(loaded.agent).__name__ == "LlmAgent" else None
        app = compile_agent(loaded.agent, config=config, target="durable_agent_server",
                            model="apx-doctor-model", service_ws=_FakeWorkspaceClient(),
                            **({"checkpointer": checkpointer} if checkpointer is not None else {}))
        if mcp:
            from ._wiring import mount_mcp_endpoints

            mount_mcp_endpoints(app, loaded.agent)
        client = stack.enter_context(TestClient(app))
        ready = client.get("/readyz")
        if ready.status_code != 200 or ready.json().get("checks", {}).get("runtime_store") != "ok":
            return Check("Startup", Status.FAIL, f"/readyz {ready.status_code}: {ready.text[:200]}")
        if mcp and app.state._apx_mount_state is None:
            return Check("Startup", Status.FAIL, "/mcp mounted but its lifecycle never started (stays 503)")
    except BaseException as exc:
        return _fail("Startup", exc)
    return _Started(app=app, client=client, mcp=mcp)


def _delegates(agent: Any) -> list[Any]:
    from ._resources import _iter_tool_fns

    return [fn for fn in _iter_tool_fns(agent) if hasattr(fn, "__apx_sub_agent_url__")]


def _call_delegate(fn: Any, headers: Any) -> None:
    import asyncio
    import inspect

    params = [p for p in inspect.signature(fn).parameters if p != "headers"]
    kwargs = {p: "apx doctor probe" for p in params}
    asyncio.run(fn(headers=headers, **kwargs))


def _stage_request(loaded: _Loaded, started: _Started, harness: _Harness) -> Check:
    import uuid

    from databricks_agentkit.runtime.auth import RequestAuthContext

    from ._durable_agent import durable_request_headers

    try:
        os.environ.pop("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", None)
        os.environ["DATABRICKS_APP_NAME"] = "apx-doctor"
        os.environ["DATABRICKS_HOST"] = "https://apx-doctor.invalid"
        body = {"id": str(uuid.uuid4()), "session_id": "apx-doctor",
                "input": [{"role": "user", "content": "apx doctor readiness probe"}]}
        headers = {"X-Forwarded-User": "apx-doctor", "X-Forwarded-Access-Token": SENTINEL}
        from . import _durable_agent

        seen: list[tuple[bool, bool]] = []

        def _spy(auth: Any, *, principal: str, forward: bool) -> Any:
            seen.append((auth._local, forward))
            return durable_request_headers(auth, principal=principal, forward=forward)

        with mock.patch.object(_durable_agent, "durable_request_headers", _spy):
            response = started.client.post("/api/invocations", json=body, headers=headers)
        if response.status_code != 200:
            return Check("Request", Status.FAIL, f"/api/invocations {response.status_code}: {response.text[:200]}")
        saved = started.client.get(f"/api/invocations/{body['id']}", headers=headers)
        if saved.status_code != 200:
            return Check("Request", Status.FAIL, f"GET /api/invocations/<id> {saved.status_code}: {saved.text[:200]}")
        if SENTINEL in response.text or SENTINEL in saved.text:
            return Check("Request", Status.FAIL, "request-user token persisted in the invocation")
        delegates = _delegates(loaded.agent)
        # Without delegates durable has no request-user auth to resolve, so no call is expected.
        if (delegates or seen) and (not seen or seen[-1] != (False, bool(delegates))):
            return Check("Request", Status.FAIL, "durable did not install the request-user resolver "
                         f"(observed (local, forward) = {seen[-1] if seen else 'no call'}, "
                         f"expected (False, {bool(delegates)}))")
        for fn in delegates:
            deployed = RequestAuthContext(token=SENTINEL, principal="apx-doctor", local=False)
            harness.captured.clear()
            _call_delegate(fn, durable_request_headers(deployed, principal="apx-doctor", forward=True))
            want = {"X-Forwarded-Access-Token": SENTINEL, "Authorization": f"Bearer {SENTINEL}"}
            if harness.captured != [want]:
                return Check("Request", Status.FAIL, f"delegate {fn.__name__} forwarded {sorted(harness.captured[0]) if harness.captured else 'nothing'}")
            local = RequestAuthContext(token=None, principal="local-developer", local=True)
            harness.captured.clear()
            _call_delegate(fn, durable_request_headers(local, principal="local-developer", forward=not local._local))
            if harness.captured != [{}]:
                return Check("Request", Status.FAIL, f"delegate {fn.__name__} forwarded credentials from a local context")
    except BaseException as exc:
        return _fail("Request", exc)
    finally:
        os.environ.pop("DATABRICKS_APP_NAME", None)
    return Check("Request", Status.OK,
                 f"invocation ok; {len(delegates)} delegate(s) forwards caller token; local forwards none; not persisted")


def _cascade(results: list[Check]) -> list[Check]:
    """Pad to every stage; after the first FAIL, later stages are SKIP."""
    out: list[Check] = []
    blocked: str | None = None
    by_name = {c.name: c for c in results}
    for stage in STAGES:
        if blocked is not None:
            out.append(Check(stage, Status.SKIP, f"blocked by {blocked}"))
            continue
        check = by_name[stage]  # every unblocked stage has produced a result
        out.append(check)
        if check.status is Status.FAIL:
            blocked = stage
    return out


def _stage_deploy(loaded: _Loaded) -> Check:
    import tomllib

    import click
    import yaml

    from ._agentbricks_deploy import validate_cli_deployment

    bundle = loaded.project / "databricks.yml"
    if not bundle.exists():
        return Check("Deploy", Status.SKIP, "no databricks.yml (native deploy generates its own bundle)")
    try:
        doc = yaml.safe_load(bundle.read_text())
        resources = doc.get("resources") if isinstance(doc, dict) else None
        apps = resources.get("apps") if isinstance(resources, dict) else None
        if not isinstance(apps, dict) or not apps:
            return Check("Deploy", Status.FAIL, "databricks.yml has no resources.apps entry",
                         "Declare the app under resources.apps (native deploy needs exactly one).")
        bundle_key = next(iter(apps))
        deploy = tomllib.loads((loaded.project / "pyproject.toml").read_text()).get("tool", {}).get("apx", {}).get("deploy", {})
        app_name = deploy["app_name"] if "app_name" in deploy else apps[bundle_key]["name"]
        # The real deploy needs --profile and runs the app; the preflight checks the bundle, not those flags.
        validate_cli_deployment(doc, bundle_key=bundle_key, app_name=app_name, profile="apx-doctor", no_run=False)
    except click.ClickException as exc:
        return Check("Deploy", Status.FAIL, exc.message, "Fix the bundle for a native durable deploy and re-run (first rejection only).")
    except BaseException as exc:
        return _fail("Deploy", exc)
    return Check("Deploy", Status.OK, f"native deploy preflight passes for {app_name}")


def _advisory(loaded: _Loaded | None, project: Path) -> list[Check]:
    try:
        surface = _entrypoint_surface(project)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        remount = Check("Re-mount", Status.WARN, f"could not scan entrypoint: {type(exc).__name__}: {exc}")
    else:
        remount = (Check("Re-mount", Status.WARN, "entrypoint adds: " + ", ".join(surface),
                         "Re-mount these on the durable app (see python/examples/data-triage-agent/app.py).")
                   if surface else Check("Re-mount", Status.OK, "no hand-added FastAPI surface"))
    if loaded is None:
        return [remount, Check("History", Status.SKIP, "config did not load; declared stores unknown")]
    stores = [f"{label}={store.type}"
              for label, store in (("session", loaded.config.session), ("memory", loaded.config.memory))
              if store is not None and store.type != "managed"]
    history = (Check("History", Status.WARN, "non-managed stores: " + ", ".join(stores) +
                     " — existing history will not move to a managed store")
               if stores else Check("History", Status.OK, "no non-managed stores declared"))
    return [remount, history]


def check_durable_readiness(project: Path) -> list[Check]:
    """Run the durable readiness stages for the apx project at ``project``."""
    project = project.resolve()
    results: list[Check] = []
    with _isolated(project) as harness:
        loaded = _stage_config(project)
        if isinstance(loaded, Check):
            if loaded.status is Status.SKIP:
                return [loaded]
            return _cascade([loaded]) + _advisory(None, project)
        results.append(Check("Config", Status.OK,
                             f"durable target valid; agent imported ({type(loaded.agent).__name__} "
                             f"{getattr(loaded.agent, '_name', loaded.config.name)})"))
        results.extend(_stage_compile(loaded))
        if results[-1].status is Status.OK:
            with contextlib.ExitStack() as stack:
                started = _stage_startup(loaded, lambda: _entrypoint_surface(project), stack)
                if isinstance(started, Check):
                    results.append(started)
                else:
                    results.append(Check("Startup", Status.OK,
                                         "lifespan ok; /readyz runtime_store=ok (in-memory store; Lakebase not exercised offline)" + ("; /mcp live" if started.mcp else "")))
                    results.append(_stage_request(loaded, started, harness))
        if len(results) == 4 and all(c.status is Status.OK for c in results):
            results.append(_stage_deploy(loaded))
    return _cascade(results) + _advisory(loaded, project)
