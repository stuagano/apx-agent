"""`apx-agent doctor --durable`: run-not-read readiness for durable_agent_server.

Exercises an apx project the way the native runtime would — compile, start the
lifespan, serve a request, run the deploy preflight — fully offline: temp cwd,
fake workspace clients, refused outbound network (httpx and sockets), scripted
model. Nothing under the project is written (no bytecode either); everything
patched is restored on exit.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

from ._doctor import Check, Status

STAGES = ("Config", "Compile", "Startup", "Request", "Deploy")
SENTINEL = "apx-doctor-sentinel-token"
PEER_URL = "https://apx-doctor-peer.invalid"
SDK_FIX = 'uv run --with "databricks-agentbricks>=0.4.0,<0.5" apx-agent doctor --durable --offline'
AGENT_MODULE_FIX = ("The native durable runtime imports `agent:agent` (as _serve.create_app does); "
                    "the pyproject `module =` key is not used by the durable host. "
                    "Expose the agent as `agent` in agent.py at the project root.")


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


OFFLINE = "doctor --durable is offline: outbound network refused"


def _refuse(*args: Any, **kwargs: Any) -> Any:
    import httpx

    raise httpx.ConnectError("doctor --durable is offline: outbound HTTP refused")


async def _refuse_async(*args: Any, **kwargs: Any) -> Any:
    _refuse()


def _fake_make_client(*args: Any, **kwargs: Any) -> _FakeWorkspaceClient:
    return _FakeWorkspaceClient()


def _is_local(host: Any) -> bool:
    import ipaddress

    if host is None or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard_sockets(stack: contextlib.ExitStack) -> None:
    """Refuse every non-loopback connect and name lookup (requests, urllib, raw sockets).

    In-process ASGI traffic (TestClient) opens no socket; AF_UNIX paths and
    loopback stay allowed. Lookups are refused too, so a hostname never leaves
    the process.
    """
    import socket

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_create, real_lookup = socket.create_connection, socket.getaddrinfo

    def _check(address: Any) -> None:
        if isinstance(address, tuple) and not _is_local(address[0]):
            raise ConnectionRefusedError(OFFLINE)

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        _check(address)
        return real_create(address, *args, **kwargs)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        _check((host,))
        return real_lookup(host, *args, **kwargs)

    for target, attr, value in ((socket.socket, "connect", connect), (socket.socket, "connect_ex", connect_ex),
                                (socket, "create_connection", create_connection), (socket, "getaddrinfo", getaddrinfo)):
        stack.enter_context(mock.patch.object(target, attr, value))


def _holders(wanted: dict[int, Any]) -> list[Any]:
    """[(module, attr, replacement)] for every module global bound to a key of ``wanted``.

    Scans all of sys.modules, not just apx_agent: SDK modules (e.g. the agent
    runtime's workspace helper) bind WorkspaceClient by name at import.
    """
    hits = []
    for name, module in list(sys.modules.items()):
        if name == __name__ or not isinstance(module, ModuleType):
            continue
        for attr, value in list(vars(module).items()):
            if id(value) in wanted and value is wanted[id(value)][0]:
                hits.append((module, attr, wanted[id(value)][1]))
    return hits


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

    import databricks.sdk

    from . import _defaults

    real_sdk_client = databricks.sdk.WorkspaceClient
    real_make_client = _defaults._make_workspace_client
    fakes = {id(real_sdk_client): (real_sdk_client, _FakeWorkspaceClient),
             id(real_make_client): (real_make_client, _fake_make_client)}
    reals = {id(fake): (fake, real) for real, fake in fakes.values()}

    def _restore_leaks() -> None:
        # Modules first imported during the run bound the fakes by name and are
        # not covered by the entry patches; point them back at the real objects.
        for module, attr, real in _holders(reals):
            setattr(module, attr, real)

    # Same-named modules already imported from elsewhere (another project's
    # `agent`) would shadow the project's own; set them aside for the run.
    # `agent` is always set aside: it is the module the native runtime imports, so
    # a project without one must not pick up another project's.
    tops = {"agent"} | {p.stem for p in project.glob("*.py")} | {p.parent.name for p in project.glob("*/__init__.py")}
    env, cwd, path, modules = dict(os.environ), Path.cwd(), list(sys.path), set(sys.modules)
    bytecode = sys.dont_write_bytecode
    with tempfile.TemporaryDirectory(prefix="apx-doctor-") as tmp, contextlib.ExitStack() as stack:
        stack.callback(_restore_leaks)  # registered first, so it runs after every patch is undone
        shadowed = {name: sys.modules.pop(name) for name in list(sys.modules) if name.partition(".")[0] in tops}
        stack.callback(sys.modules.update, shadowed)  # after the project's own modules are unloaded
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
        for module, attr, fake in _holders(fakes):
            stack.enter_context(mock.patch.object(module, attr, fake))
        _guard_sockets(stack)
        _disable_tracing(stack)
        try:
            sys.dont_write_bytecode = True  # importing agent.py must not write <project>/__pycache__
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
            sys.dont_write_bytecode = bytecode
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
        from ._inspection import _agent_config_fields
        from ._wiring import resolve_agent

        # Validate the declared fields once, as durable: validating the current
        # target first rejects durable-only settings (e.g. deploy.space) mid-migration.
        fields = _agent_config_fields(project / "pyproject.toml", ("tool", "apx", "agent"))
        if fields is None:
            return Check("Config", Status.FAIL, "could not load [tool.apx.agent]")
        config = AgentConfig.model_validate({**fields, "target": "durable_agent_server"})
        # The native host imports `agent:agent` (as _serve.create_app does); the
        # pyproject `module =` key is not read by the durable runtime.
        agent = resolve_agent("agent:agent", config, ws=_FakeWorkspaceClient())
        origin = getattr(sys.modules.get("agent"), "__file__", None)
        if origin is None or not Path(origin).resolve().is_relative_to(project):
            return Check("Config", Status.FAIL, f"`agent` resolved outside the project ({origin})",
                         AGENT_MODULE_FIX)
    except BaseException as exc:
        check = _fail("Config", exc)
        missing = exc.__cause__ if isinstance(exc.__cause__, ModuleNotFoundError) else exc
        if isinstance(missing, ModuleNotFoundError) and missing.name == "agent":
            return Check("Config", Status.FAIL, check.detail, AGENT_MODULE_FIX)
        return check
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


def _mount_lifecycle(app: Any) -> Any:
    """The MCP mount's live lifecycle; unset when its startup never ran."""
    try:
        return app.state._apx_mount_state
    except AttributeError:
        return None


def _stage_startup(loaded: _Loaded, surface: Callable[[], list[str]], stack: contextlib.ExitStack) -> _Started | Check:
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver

    from ._agents import LlmAgent
    from ._runtime_targets import compile_agent

    try:
        # A hand-written entrypoint that cannot be parsed is a Startup failure.
        mcp = any(s.startswith("mount_mcp_endpoints") for s in surface())
        # The managed Session Store is provisioned at deploy; substitute memory.
        config = loaded.config.model_copy(update={"session": None})
        checkpointer = InMemorySaver() if isinstance(loaded.agent, LlmAgent) else None
        app = compile_agent(loaded.agent, config=config, target="durable_agent_server",
                            model="apx-doctor-model", service_ws=_FakeWorkspaceClient(),
                            checkpointer=checkpointer)
        if mcp:
            from ._wiring import mount_mcp_endpoints

            mount_mcp_endpoints(app, loaded.agent)
        client = stack.enter_context(TestClient(app))
        ready = client.get("/readyz")
        if ready.status_code != 200 or ready.json().get("checks", {}).get("runtime_store") != "ok":
            return Check("Startup", Status.FAIL, f"/readyz {ready.status_code}: {ready.text[:200]}")
        if mcp and _mount_lifecycle(app) is None:
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


def _invocation() -> dict[str, Any]:
    import uuid

    return {"id": str(uuid.uuid4()), "session_id": "apx-doctor",
            "input": [{"role": "user", "content": "apx doctor readiness probe"}]}


def _stage_request(loaded: _Loaded, started: _Started, harness: _Harness) -> Check:
    from databricks_agentkit.runtime.auth import RequestAuthContext

    from . import _durable_agent
    from ._durable_agent import durable_request_headers

    seen: list[tuple[bool, bool]] = []

    def _spy(auth: Any, *, principal: str, forward: bool) -> Any:
        seen.append((auth._local, forward))
        return durable_request_headers(auth, principal=principal, forward=forward)

    headers = {"X-Forwarded-User": "apx-doctor", "X-Forwarded-Access-Token": SENTINEL}
    try:
        with mock.patch.object(_durable_agent, "durable_request_headers", _spy):
            # Deployed context: Apps ingress headers are trusted.
            os.environ.pop("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", None)
            os.environ["DATABRICKS_APP_NAME"] = "apx-doctor"
            os.environ["DATABRICKS_HOST"] = "https://apx-doctor.invalid"
            body = _invocation()
            response = started.client.post("/api/invocations", json=body, headers=headers)
            if response.status_code != 200:
                return Check("Request", Status.FAIL, f"/api/invocations {response.status_code}: {response.text[:200]}")
            saved = started.client.get(f"/api/invocations/{body['id']}", headers=headers)
            if saved.status_code != 200:
                return Check("Request", Status.FAIL, f"GET /api/invocations/<id> {saved.status_code}: {saved.text[:200]}")
            deployed_seen = list(seen)
            # Local context: the same headers arrive, but durable must forward nothing.
            seen.clear()
            os.environ["DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL"] = "true"
            os.environ.pop("DATABRICKS_APP_NAME", None)
            local_response = started.client.post("/api/invocations", json=_invocation(), headers=headers)
            if local_response.status_code != 200:
                return Check("Request", Status.FAIL,
                             f"local /api/invocations {local_response.status_code}: {local_response.text[:200]}")
            local_seen = list(seen)
        if any(SENTINEL in r.text for r in (response, saved, local_response)):
            return Check("Request", Status.FAIL, "request-user token persisted in the invocation")
        delegates = _delegates(loaded.agent)
        # Without delegates durable has no request-user auth to resolve, so no call is expected.
        if (delegates or deployed_seen) and (not deployed_seen or deployed_seen[-1] != (False, bool(delegates))):
            return Check("Request", Status.FAIL, "durable did not install the request-user resolver "
                         f"(observed (local, forward) = {deployed_seen[-1] if deployed_seen else 'no call'}, "
                         f"expected (False, {bool(delegates)}))")
        if any(forward for _, forward in local_seen):
            return Check("Request", Status.FAIL, "durable would forward credentials from a local context "
                         f"(observed (local, forward) = {local_seen[-1]})")
        # The scripted model never calls tools, so drive each delegate with the
        # forward decisions durable made above: True when deployed, never when local.
        for fn in delegates:
            deployed = RequestAuthContext(token=SENTINEL, principal="apx-doctor", local=False)
            harness.captured.clear()
            _call_delegate(fn, durable_request_headers(deployed, principal="apx-doctor", forward=deployed_seen[-1][1]))
            want = {"X-Forwarded-Access-Token": SENTINEL, "Authorization": f"Bearer {SENTINEL}"}
            if harness.captured != [want]:
                return Check("Request", Status.FAIL, f"delegate {fn.__name__} forwarded {sorted(harness.captured[0]) if harness.captured else 'nothing'}")
            local = RequestAuthContext(token=None, principal="local-developer", local=True)
            harness.captured.clear()
            _call_delegate(fn, durable_request_headers(local, principal="local-developer", forward=False))
            if harness.captured != [{}]:
                return Check("Request", Status.FAIL, f"delegate {fn.__name__} forwarded credentials from a local context")
    except BaseException as exc:
        return _fail("Request", exc)
    finally:
        os.environ.pop("DATABRICKS_APP_NAME", None)
        os.environ["DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL"] = "true"
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

    declared = loaded.config.deploy
    if declared is not None and declared.backend == "apps_api":
        # Rows 3 and 4 are declaration-driven. A missing databricks.yml is expected
        # and must not SKIP this stage.
        try:
            from ._apps_api_deploy import preflight_apps_api

            return Check("Deploy", Status.OK, preflight_apps_api(loaded.project, loaded.config, loaded.agent))
        except click.ClickException as exc:
            return Check("Deploy", Status.FAIL, exc.message, "Fix the Apps API declaration and re-run (first rejection only).")
        except BaseException as exc:
            return _fail("Deploy", exc)
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
        declared_space = declared.space if declared is not None else None
        if declared_space is not None:
            # Row 2. A declared App Space on the Agent Bricks path goes through
            # apx's own bundle path (any app name, e.g. mcp-); scopes are online.
            from ._app_space import validate_space_bundle

            if validate_space_bundle(doc, bundle_key=bundle_key) != declared_space:
                return Check("Deploy", Status.FAIL, "databricks.yml space disagrees with [tool.apx.agent.deploy] space")
            return Check("Deploy", Status.OK, f"App Space deploy preflight passes for {app_name} (space {declared_space}; "
                         "inherited scopes and resources are checked online at deploy)")
        # Row 1. The real deploy needs --profile and runs the app; this checks the bundle.
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


def _close_lifespan(stack: contextlib.ExitStack, results: list[Check]) -> list[Check]:
    """Exit the TestClient (lifespan shutdown); a failing shutdown is a Startup FAIL, not a crash."""
    try:
        stack.close()
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        detail = f"lifespan shutdown failed: {type(exc).__name__}: {exc}"
        prior = next(c for c in results if c.name == "Startup")
        if prior.status is Status.FAIL:
            detail = f"{prior.detail}; {detail}"
        return [c for c in results if c.name not in ("Startup", "Request")] + [Check("Startup", Status.FAIL, detail)]
    return results


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
            stack = contextlib.ExitStack()
            started = _stage_startup(loaded, lambda: _entrypoint_surface(project), stack)
            if isinstance(started, Check):
                results.append(started)
            else:
                results.append(Check("Startup", Status.OK,
                                     "lifespan ok; /readyz runtime_store=ok (in-memory store; Lakebase not exercised offline)" + ("; /mcp live" if started.mcp else "")))
                results.append(_stage_request(loaded, started, harness))
            results = _close_lifespan(stack, results)
        if len(results) == 4 and all(c.status is Status.OK for c in results):
            results.append(_stage_deploy(loaded))
    return _cascade(results) + _advisory(loaded, project)
