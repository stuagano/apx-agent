"""Claim-vs-reality for the generated per-agent platform client (#946).

Generation writes a Python module. ``ctk.verify`` reads that file back, and
importing it proves the declared operation is a real method: the declared
arguments are the signature, the call hits the SDK attribute, and the SDK
result is returned unchanged.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType

import pytest

from apx_agent import Agent, PlatformOperation, declare_platform_operations, generate_platform_client
from ctk import Artifact, verify


def _load(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import generated client {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_client_method_calls_sdk_boundary(tmp_path: Path) -> None:
    agent = Agent(name="sales", tools=[])
    declare_platform_operations(
        agent,
        [PlatformOperation("list_catalogs", "catalogs.list", ("include_browse",))],
    )
    path = generate_platform_client(agent, tmp_path)
    verify(
        Artifact(
            str(path),
            min_bytes=200,
            must_contain="def list_catalogs(self, *, include_browse: bool | None = None) -> Iterator[CatalogInfo]:",
        ),
        Artifact(
            str(path),
            must_contain="return self._workspace.catalogs.list(include_browse=include_browse)",
        ),
        Artifact(str(path), must_not_contain="clusters.list"),
        Artifact(str(path), must_not_contain="https://"),
        Artifact(str(path), must_not_contain="DATABRICKS_TOKEN"),
    )

    module = _load(path, "apx_generated_sales_platform_client")
    signature = inspect.signature(module.SalesPlatformClient.list_catalogs)
    assert [name for name in signature.parameters if name != "self"] == ["include_browse"]
    assert signature.parameters["include_browse"].kind is inspect.Parameter.KEYWORD_ONLY

    row = ["catalog-row"]

    class _Catalogs:
        def __init__(self) -> None:
            self.include_browse: bool | None = None

        def list(self, *, include_browse: bool | None = None) -> list[str]:
            self.include_browse = include_browse
            return row

    class _Workspace:
        def __init__(self) -> None:
            self.catalogs = _Catalogs()

    workspace = _Workspace()
    client = module.SalesPlatformClient(workspace)
    assert client.list_catalogs(include_browse=True) is row
    assert workspace.catalogs.include_browse is True
    with pytest.raises(TypeError):
        client.list_catalogs(include_browse=True, page_token="hidden")
    assert not hasattr(client, "list_clusters")


def test_required_field_is_a_real_argument(tmp_path: Path) -> None:
    agent = Agent(name="sales", tools=[])
    declare_platform_operations(
        agent,
        [PlatformOperation("get_catalog", "catalogs.get", ("name",))],
    )
    path = generate_platform_client(agent, tmp_path)
    verify(Artifact(str(path), min_bytes=100, must_contain="def get_catalog(self, *, name: str) -> CatalogInfo:"))
    module = _load(path, "apx_generated_sales_get_catalog")

    class _Catalogs:
        def get(self, *, name: str) -> dict[str, str]:
            return {"name": name}

    class _Workspace:
        catalogs = _Catalogs()

    client = module.SalesPlatformClient(_Workspace())
    assert client.get_catalog(name="main") == {"name": "main"}
    with pytest.raises(TypeError):
        client.get_catalog()


def test_regenerate_touches_only_that_agent(tmp_path: Path) -> None:
    sales = Agent(name="sales", tools=[])
    support = Agent(name="support", tools=[])
    declare_platform_operations(
        sales,
        [PlatformOperation("list_catalogs", "catalogs.list", ("include_browse",))],
    )
    declare_platform_operations(support, [PlatformOperation("list_clusters", "clusters.list")])
    sales_path = generate_platform_client(sales, tmp_path)
    support_path = generate_platform_client(support, tmp_path)
    sales_bytes = sales_path.read_bytes()
    sales_mtime = sales_path.stat().st_mtime_ns
    support_bytes = support_path.read_bytes()
    support_mtime = support_path.stat().st_mtime_ns

    assert generate_platform_client(sales, tmp_path).read_bytes() == sales_bytes
    assert sales_path.stat().st_mtime_ns == sales_mtime

    declare_platform_operations(
        sales,
        [PlatformOperation("list_catalogs", "catalogs.list", ("include_browse", "max_results"))],
    )
    assert generate_platform_client(sales, tmp_path).read_bytes() != sales_bytes
    assert support_path.read_bytes() == support_bytes
    assert support_path.stat().st_mtime_ns == support_mtime
    module = _load(sales_path, "apx_generated_sales_wider")
    signature = inspect.signature(module.SalesPlatformClient.list_catalogs)
    assert [name for name in signature.parameters if name != "self"] == ["include_browse", "max_results"]


def test_removing_an_operation_removes_its_method(tmp_path: Path) -> None:
    agent = Agent(name="sales", tools=[])
    declare_platform_operations(
        agent,
        [
            PlatformOperation("list_catalogs", "catalogs.list", ("include_browse",)),
            PlatformOperation("list_clusters", "clusters.list"),
        ],
    )
    path = generate_platform_client(agent, tmp_path)
    both = _load(path, "apx_generated_sales_both")
    assert hasattr(both.SalesPlatformClient, "list_clusters")

    declare_platform_operations(
        agent,
        [PlatformOperation("list_catalogs", "catalogs.list", ("include_browse",))],
    )
    generate_platform_client(agent, tmp_path)
    verify(Artifact(str(path), must_contain="def list_catalogs(", must_not_contain="clusters.list"))
    one = _load(path, "apx_generated_sales_one")
    assert hasattr(one.SalesPlatformClient, "list_catalogs")
    assert not hasattr(one.SalesPlatformClient, "list_clusters")


def test_wildcard_unknown_and_duplicate_declarations_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="wildcard"):
        PlatformOperation("list_all", "catalogs.*")
    agent = Agent(name="sales", tools=[])
    with pytest.raises(ValueError, match="not a WorkspaceClient operation"):
        declare_platform_operations(agent, [PlatformOperation("missing", "catalogs.not_a_method")])
    with pytest.raises(ValueError, match="has no request field"):
        declare_platform_operations(
            agent,
            [PlatformOperation("list_catalogs", "catalogs.list", ("not_a_field",))],
        )
    with pytest.raises(ValueError, match="requires request field 'name'"):
        declare_platform_operations(agent, [PlatformOperation("get_catalog", "catalogs.get")])
    with pytest.raises(ValueError, match="duplicate platform operation name"):
        declare_platform_operations(
            agent,
            [
                PlatformOperation("list_catalogs", "catalogs.list", ("include_browse",)),
                PlatformOperation("list_catalogs", "clusters.list"),
            ],
        )
    assert agent._platform_operations == ()
    unnamed = Agent(tools=[])
    with pytest.raises(ValueError, match="Python identifier"):
        generate_platform_client(unnamed, tmp_path)
