"""External-HTTP tool factories backed by governed Unity Catalog connections.

``http_tool`` calls an external HTTP service through the Unity Catalog
**HTTP connection proxy**::

    https://<workspace>/api/2.0/unity-catalog/connections/<connection>/proxy/<path>

The request carries the calling user's Databricks token (OBO). Databricks
authenticates the caller, checks ``USE CONNECTION`` on the connection, injects
the external service's credential stored in the connection, and forwards the
request to ``{host}{base_path}{path}``. The app never sees the third-party
credential, and no SQL warehouse is involved. The tool declares a
``ResourceSpec("uc_connection", ...)`` so deploy adds the connection as an App
resource with the ``USE_CONNECTION`` permission.

Set up the connection once (governed by your data team)::

    CREATE CONNECTION my_api TYPE HTTP
      OPTIONS (host 'https://api.example.com', port '443', base_path '/v1',
               bearer_token SECRET('scope', 'key'));
    GRANT USE CONNECTION ON CONNECTION my_api TO `user@example.com`;

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
import warnings
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"})

CONNECTION_PROXY_PATH = "/api/2.0/unity-catalog/connections/{connection}/proxy"
_PROXY_TIMEOUT_S = 30.0


def connection_proxy_url(host: str, connection: str, path: str) -> str:
    """Return the UC HTTP connection proxy URL for ``path`` on ``connection``.

    ``connection`` is percent-encoded as one path segment, so a ``/`` in a name
    cannot escape into a different API route. ``path`` follows ``/proxy``;
    Databricks forwards it to ``{host}{base_path}{path}``.
    """
    encoded = quote(connection, safe="")
    base = host.rstrip("/") + CONNECTION_PROXY_PATH.format(connection=encoded)
    if not path:
        return base + "/"
    return base + (path if path.startswith("/") else "/" + path)


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

    The request goes to the Unity Catalog connection proxy
    (``/api/2.0/unity-catalog/connections/<connection>/proxy/<path>``) with the
    calling user's Databricks token. Databricks enforces ``USE CONNECTION`` per
    user and injects the external credential from the connection; the app never
    holds it.

    The LLM controls two optional inputs:

      * ``query`` — a ``dict[str, str]`` of URL query parameters.
      * ``body`` — a JSON-serialisable dict sent as the request body.

    ``method`` and ``path`` are fixed at factory time (developer-supplied,
    trusted), so each ``http_tool`` exposes one specific API operation. The
    tool returns ``{"status_code": int, "text": str}``, or ``{"error": str}``
    when the request cannot be sent.

    Args:
        connection: Name of the Unity Catalog HTTP connection. Declared as a
            ``uc_connection`` resource so deploy adds it as an App resource
            with ``USE_CONNECTION``.
        method: HTTP method — one of GET, POST, PUT, DELETE, PATCH.
        path: Path appended to the connection's ``base_path``. Fixed at factory
            time.
        name: LLM-facing tool name. Defaults to ``"http_request"``.
        description: LLM-facing description. When omitted, generated from the
            method, path, and connection.
        warehouse_id: Deprecated and ignored (emits ``DeprecationWarning``).
            The connection proxy does not run on a SQL warehouse.
        max_chars: Truncate the response ``text`` to this many characters.

    Returns:
        An apx-agent tool callable — register it in ``Agent(tools=[...])``.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec, require_user_api_scopes
    from ._tool_factory import build_tool, resolve_description

    _method = method.upper()
    if _method not in _ALLOWED_METHODS:
        raise ValueError(
            f"http_tool: method must be one of {sorted(_ALLOWED_METHODS)}, got {method!r}"
        )
    if warehouse_id is not None:
        warnings.warn(
            "http_tool(warehouse_id=...) is ignored: requests go through the Unity "
            "Catalog connection proxy, which needs no SQL warehouse. Remove the argument.",
            DeprecationWarning,
            stacklevel=2,
        )

    _path = path if path is not None else ""

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
        # The URL is built from trusted factory-time values only. LLM input
        # travels as httpx query params (encoded) and a JSON body.
        url = connection_proxy_url(ws.config.host, connection, _path)
        # The Databricks token authenticates to the proxy; Databricks strips it
        # before forwarding and injects the connection's own credential.
        headers = dict(ws.config.authenticate())
        content: bytes | None = None
        if body is not None:
            content = _json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        params = {str(k): str(v) for k, v in query.items()} if query else None
        try:
            async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT_S) as client:
                resp = await client.request(
                    _method, url, params=params, content=content, headers=headers
                )
        except httpx.HTTPError as e:
            logger.warning(
                "http_tool %s %s via connection %s failed: %s", _method, _path, connection, e
            )
            return {"error": str(e)}

        text = resp.text
        if len(text) > max_chars:
            text = text[:max_chars] + f"… [truncated to {max_chars} chars]"
        return {"status_code": resp.status_code, "text": text}

    tool = build_tool(
        _http_request,
        name=name,
        description=_desc,
        resources=[ResourceSpec("uc_connection", connection)],
    )
    return require_user_api_scopes(tool, ["catalog.connections"])


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

        resp = httpx.get(spec, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()  # an HTML/text error page would otherwise parse as a YAML string
        raw = resp.text
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
        warehouse_id: Deprecated and ignored (emits ``DeprecationWarning``).
            Each ``http_tool`` uses the connection proxy, not a SQL warehouse.
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
    from ._tool_config import _check_allowlist

    if warehouse_id is not None:
        warnings.warn(
            "openapi_tool(warehouse_id=...) is ignored: the generated http_tools go "
            "through the Unity Catalog connection proxy. Remove the argument.",
            DeprecationWarning,
            stacklevel=2,
        )
    _check_allowlist(None, "openapi", {"spec": spec})
    doc = _load_openapi_spec(spec)
    if not isinstance(doc, dict):
        # A non-JSON/HTML response YAML-parses to a str; guard before `.get`.
        raise ValueError(
            f"openapi_tool: {spec!r} did not parse to a spec object "
            f"(got {type(doc).__name__}); check the URL/path returns OpenAPI JSON or YAML."
        )
    paths = doc.get("paths") or {}

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
                )
            )

    if spec.startswith(("http://", "https://")):
        from dataclasses import replace

        from ._tool import ToolMetadata, get_tool_metadata

        # The spec is fetched from the app process at startup (App Space egress check).
        for tool in tools:
            tool._apx_tool = replace(get_tool_metadata(tool) or ToolMetadata(), remote_url=spec)
    return tools
