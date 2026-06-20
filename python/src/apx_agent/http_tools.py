"""External-HTTP tool factories backed by governed Unity Catalog connections.

``http_tool`` calls an external HTTP service through a UC **HTTP connection**
using Databricks' ``http_request`` SQL function, executed under the calling
user's identity (OBO). The credential lives in the connection — the app never
sees it, and Unity Catalog enforces ``USE_CONNECTION`` per user. The tool
declares a ``ResourceSpec("uc_connection", ...)`` so the compile/deploy path
wires the grant automatically.

Set up the connection once (governed by your data team)::

    CREATE CONNECTION my_api TYPE HTTP
      OPTIONS (host 'https://api.example.com', port '443', base_path '/v1',
               bearer_token SECRET('scope', 'key'));

Then expose an operation as a tool::

    from apx_agent import Agent, http_tool

    agent = Agent(tools=[
        http_tool("my_api", method="GET", path="/users",
                  name="list_users", description="List users."),
    ])
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"})


def http_tool(
    connection: str,
    *,
    method: str = "GET",
    path: str | None = None,
    name: str = "http_request",
    description: str | None = None,
    warehouse_id: str | None = None,
    max_chars: int = 8000,
) -> Any:
    """Return a tool that calls an external HTTP service via a UC HTTP connection.

    The request runs server-side through Databricks' ``http_request`` SQL
    function under the calling user's identity. Credentials are resolved from
    the Unity Catalog connection (never seen by the app), and ``USE_CONNECTION``
    is enforced per user.

    The LLM controls two optional inputs:

      * ``query`` — a ``dict[str, str]`` of URL query parameters.
      * ``body`` — a JSON-serialisable dict sent as the request body.

    ``method`` and ``path`` are fixed at factory time (developer-supplied,
    trusted), so each ``http_tool`` exposes one specific API operation. The
    tool returns ``{"status_code": int | None, "text": str}``.

    Args:
        connection: Name of the Unity Catalog HTTP connection. Declared as a
            ``uc_connection`` resource so deploy wires the ``USE_CONNECTION``
            grant.
        method: HTTP method — one of GET, POST, PUT, DELETE, PATCH.
        path: Path appended to the connection's ``base_path``. Fixed at factory
            time.
        name: LLM-facing tool name. Defaults to ``"http_request"``.
        description: LLM-facing description. When omitted, generated from the
            method, path, and connection.
        warehouse_id: SQL warehouse to run ``http_request`` on. When set, it is
            declared as a ``sql_warehouse`` resource; when omitted, a warehouse
            is auto-discovered at call time.
        max_chars: Truncate the response ``text`` to this many characters.

    Returns:
        An apx-agent tool callable — register it in ``Agent(tools=[...])``.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._sql import sql_str_literal
    from ._tool_factory import build_tool, resolve_description

    _method = method.upper()
    if _method not in _ALLOWED_METHODS:
        raise ValueError(
            f"http_tool: method must be one of {sorted(_ALLOWED_METHODS)}, got {method!r}"
        )

    _path = path if path is not None else ""
    _conn_lit = sql_str_literal(connection)
    _method_lit = sql_str_literal(_method)
    _path_lit = sql_str_literal(_path)

    _desc = resolve_description(
        description,
        fallback=(
            f"Make a {_method} request to `{_path or '/'}` on the external service "
            f"behind UC connection `{connection}`. Pass `query` for URL query "
            f"parameters and `body` for a JSON request body."
        ),
    )

    async def _http_request(
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        *,
        ws: UserClientDependency,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Placeholder doc — overwritten by build_tool below."""
        from ._sql import run_sql

        # CONN / METHOD / PATH are trusted factory-time values, inlined as
        # escaped literals. Everything from the LLM (query keys+values, body)
        # is passed as bind parameters — never string-interpolated.
        args = [f"CONN => {_conn_lit}", f"METHOD => {_method_lit}", f"PATH => {_path_lit}"]
        params: list[dict[str, str]] = []
        if query:
            pairs: list[str] = []
            for i, (k, v) in enumerate(query.items()):
                kp, vp = f"k{i}", f"v{i}"
                pairs.append(f":{kp}, :{vp}")
                params.append({"name": kp, "value": str(k), "type": "STRING"})
                params.append({"name": vp, "value": str(v), "type": "STRING"})
            args.append(f"PARAMS => map({', '.join(pairs)})")
        if body is not None:
            params.append({"name": "body", "value": _json.dumps(body), "type": "STRING"})
            args.append("JSON => :body")

        sql = (
            "WITH resp AS (SELECT http_request(" + ", ".join(args) + ") AS r) "
            "SELECT r.status_code AS status_code, r.text AS text FROM resp"
        )
        try:
            rows = run_sql(ws, sql, warehouse_id=warehouse_id, parameters=params or None)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "http_tool %s %s via connection %s failed: %s", _method, _path, connection, e
            )
            return {"error": str(e)}

        if not rows:
            return {"status_code": None, "text": ""}
        row = rows[0]
        text = row.get("text") or ""
        if isinstance(text, str) and len(text) > max_chars:
            text = text[:max_chars] + f"… [truncated to {max_chars} chars]"
        return {"status_code": row.get("status_code"), "text": text}

    resources = [ResourceSpec("uc_connection", connection)]
    if warehouse_id:
        resources.append(ResourceSpec("sql_warehouse", warehouse_id))
    return build_tool(_http_request, name=name, description=_desc, resources=resources)


_HTTP_METHOD_KEYS = ("get", "post", "put", "delete", "patch")


def _slug(text: str) -> str:
    """Slugify ``text`` into a safe tool name: lowercase, non-alphanumerics → ``_``."""
    import re

    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return s or "http_request"


def _load_openapi_spec(spec: str) -> dict[str, Any]:
    """Fetch/read ``spec`` and parse it as JSON, falling back to YAML."""
    if spec.startswith("http://") or spec.startswith("https://"):
        import httpx

        raw = httpx.get(spec, timeout=10.0).text
    else:
        with open(spec, encoding="utf-8") as f:
            raw = f.read()

    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        pass

    try:
        import yaml  # lazy import — pyyaml is optional
    except ImportError as e:  # pragma: no cover - depends on environment
        raise ValueError(
            f"openapi_tool: could not parse {spec!r} as JSON, and pyyaml is not "
            "installed to try YAML. Install pyyaml (`pip install pyyaml`) or "
            "provide a JSON spec."
        ) from e

    return yaml.safe_load(raw)


def openapi_tool(
    spec: str,
    connection: str,
    *,
    warehouse_id: str | None = None,
    include: list[str] | None = None,
) -> list:
    """Turn an OpenAPI spec into a list of ``http_tool``s — one per operation.

    Mirrors ``uc_function_toolkit``: where ``uc_function_toolkit`` returns many
    ``uc_function_tool``s, ``openapi_tool`` returns many ``http_tool``s. Each
    operation in the spec (every HTTP method on every path) becomes one tool
    that calls that operation through the governed UC HTTP ``connection``.

    The spec is loaded from a URL (``http://`` / ``https://``) or a local file
    path, parsed as JSON first and YAML second (requires pyyaml).

    Args:
        spec: URL or local file path to an OpenAPI 3.x / Swagger 2.0 spec.
        connection: Unity Catalog HTTP connection name, passed to each
            ``http_tool`` (declared as a ``uc_connection`` resource).
        warehouse_id: Optional SQL warehouse, forwarded to each ``http_tool``.
        include: Optional filter — keep only operations whose ``operationId``
            OR ``path`` contains any of these substrings. ``None`` keeps all.

    Returns:
        A list of ``http_tool`` callables (empty if the spec has no operations).

    Example::

        from apx_agent import Agent, openapi_tool

        tools = openapi_tool(
            "https://petstore3.swagger.io/api/v3/openapi.json",
            connection="petstore_api",
            include=["pet"],
        )
        agent = Agent(tools=tools)
    """
    doc = _load_openapi_spec(spec)
    paths = (doc or {}).get("paths") or {}

    tools: list = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method_key in _HTTP_METHOD_KEYS:
            operation = path_item.get(method_key)
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId")
            name = op_id if op_id else _slug(f"{method_key}_{path}")

            if include is not None:
                haystacks = [str(op_id or ""), str(path)]
                if not any(any(term in h for h in haystacks) for term in include):
                    continue

            desc = operation.get("summary") or operation.get("description") or None

            tools.append(
                http_tool(
                    connection,
                    method=method_key.upper(),
                    path=path,
                    name=name,
                    description=desc,
                    warehouse_id=warehouse_id,
                )
            )

    return tools
