"""Genie Agents as code.

A Genie Agent (the Management API still calls it a *space*) is declared in a
repo file: ``title``, ``description``, ``warehouse_id`` and the
``serialized_space`` (version 2) written as readable YAML or JSON instead of
the escaped string the API takes. This module

* validates ``serialized_space`` against the documented rules, offline;
* pulls a live space into a config file;
* plans (diffs) a config against the live space it resolves to; and
* pushes it: create when no space matches, update when one does.

Push never deletes or trashes a space. The match is the space ID recorded for
the current workspace host under ``deployments``, else a unique title match.
Promotion is the same file pushed with a different ``--profile`` (and
``--warehouse``): each workspace host records its own space ID.

Rules: https://docs.databricks.com/aws/en/genie-agents/conversation-api
"""

from __future__ import annotations

import difflib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from .genie import MAX_GENIE_VOLUMES

SPACE_VERSION = 2
MAX_STRING_CHARS = 25_000
MAX_ITEMS = 10_000
MAX_TEXT_INSTRUCTIONS = 1

_ID = re.compile(r"[0-9a-f]{32}")
_THREE_PART = re.compile(r"[^.\s]+\.[^.\s]+\.[^.\s]+")
_VOLUME_PATH = re.compile(r"/Volumes/[^/\s]+/[^/\s]+/[^/\s]+")
_RELATIONSHIP = re.compile(
    r"--rt=FROM_RELATIONSHIP_TYPE_(MANY_TO_ONE|ONE_TO_MANY|ONE_TO_ONE|MANY_TO_MANY)--"
)
_CONFIG_KEYS = ("title", "description", "warehouse_id", "parent_path", "deployments", "serialized_space")

# ID-bearing collections. IDs must be unique within each group (documented rule).
_QUESTION_GROUP = (("config", "sample_questions"), ("benchmarks", "questions"))
_INSTRUCTION_GROUP = (
    ("instructions", "text_instructions"),
    ("instructions", "example_question_sqls"),
    ("instructions", "sql_functions"),
    ("instructions", "join_specs"),
    ("instructions", "sql_snippets", "filters"),
    ("instructions", "sql_snippets", "expressions"),
    ("instructions", "sql_snippets", "measures"),
)
_SNIPPETS = _INSTRUCTION_GROUP[4:]


class GenieSpaceConfigError(ValueError):
    """The config file is malformed or its serialized_space breaks a documented rule."""


@dataclass
class GenieSpaceConfig:
    """A Genie Agent declared in the repo."""

    title: str
    warehouse_id: str
    serialized_space: dict[str, Any]
    description: str | None = None
    parent_path: str | None = None
    deployments: dict[str, str] = field(default_factory=dict)


@dataclass
class GenieSpacePlan:
    """What push would do (or did): ``create``, ``update`` or ``noop``."""

    action: str
    space_id: str | None
    matched_by: str | None
    diff: str


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _items(space: dict[str, Any], path: tuple[str, ...], errors: list[str]) -> list[dict[str, Any]]:
    node: Any = space
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return []
        node = node[key]
    where = ".".join(path)
    if not isinstance(node, list):
        errors.append(f"{where}: must be a list")
        return []
    if not all(isinstance(item, dict) for item in node):
        errors.append(f"{where}: every item must be an object")
        return []
    return node


def _check_sorted(where: str, keys: list[Any], label: str, errors: list[str]) -> None:
    if not all(isinstance(k, (str, tuple)) for k in keys):
        return  # missing keys are reported on their own
    for i in range(1, len(keys)):
        if keys[i] < keys[i - 1]:
            errors.append(
                f"{where}: must be sorted by {label}; {keys[i]!r} at index {i} "
                f"sorts before {keys[i - 1]!r} at index {i - 1}"
            )
            return


def _check_ids(where: str, items: list[dict[str, Any]], seen: dict[str, str], errors: list[str]) -> None:
    for i, item in enumerate(items):
        value = item.get("id")
        if not isinstance(value, str) or not _ID.fullmatch(value):
            errors.append(
                f"{where}[{i}].id: must be a 32-character lowercase hex string "
                f"without hyphens, got {value!r}"
            )
            continue
        if value in seen:
            errors.append(f"{where}[{i}].id: {value} duplicates {seen[value]}")
        else:
            seen[value] = f"{where}[{i}]"


def _non_empty_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value) and any(
        v.strip() for v in value
    )


def _check_limits(node: Any, where: str, errors: list[str]) -> None:
    if isinstance(node, str) and len(node) > MAX_STRING_CHARS:
        errors.append(f"{where}: string is {len(node)} characters; the limit is {MAX_STRING_CHARS}")
    elif isinstance(node, list):
        if len(node) > MAX_ITEMS:
            errors.append(f"{where}: {len(node)} items; the limit is {MAX_ITEMS}")
        for i, item in enumerate(node):
            _check_limits(item, f"{where}[{i}]", errors)
    elif isinstance(node, dict):
        for key, item in node.items():
            _check_limits(item, f"{where}.{key}" if where else str(key), errors)


def _check_data_sources(space: dict[str, Any], errors: list[str]) -> None:
    columns: dict[tuple[str, str], str] = {}
    for kind in ("tables", "metric_views"):
        path = ("data_sources", kind)
        where = ".".join(path)
        items = _items(space, path, errors)
        for i, item in enumerate(items):
            identifier = item.get("identifier")
            if not isinstance(identifier, str) or not _THREE_PART.fullmatch(identifier):
                errors.append(
                    f"{where}[{i}].identifier: must be a three-level catalog.schema.table name, "
                    f"got {identifier!r}"
                )
                continue
            configs = _items(item, ("column_configs",), errors)
            _check_sorted(f"{where}[{i}].column_configs", [c.get("column_name") for c in configs],
                          "column_name", errors)
            for j, column in enumerate(configs):
                name = column.get("column_name")
                if not isinstance(name, str) or not name:
                    errors.append(f"{where}[{i}].column_configs[{j}].column_name: required")
                    continue
                key = (identifier, name)
                if key in columns:
                    errors.append(
                        f"{where}[{i}].column_configs[{j}]: column {identifier}.{name} "
                        f"is already configured at {columns[key]}"
                    )
                else:
                    columns[key] = f"{where}[{i}].column_configs[{j}]"
        _check_sorted(where, [t.get("identifier") for t in items], "identifier", errors)
    for i, item in enumerate(_items(space, ("data_sources", "mcp_connections"), errors)):
        name = item.get("connection_name")
        if not isinstance(name, str) or not name:
            errors.append(f"data_sources.mcp_connections[{i}].connection_name: required")
    volumes = _items(space, ("data_sources", "volumes"), errors)
    if len(volumes) > MAX_GENIE_VOLUMES:
        errors.append(
            f"data_sources.volumes: {len(volumes)} volumes; at most {MAX_GENIE_VOLUMES} per agent"
        )
    seen: set[str] = set()
    for i, item in enumerate(volumes):
        where = f"data_sources.volumes[{i}]"
        path = item.get("path")
        if not isinstance(path, str) or not _VOLUME_PATH.fullmatch(path):
            errors.append(f"{where}.path: must be a /Volumes/<catalog>/<schema>/<volume> path, got {path!r}")
        elif path in seen:
            errors.append(f"{where}.path: {path} is already declared")
        else:
            seen.add(path)
        if "description" in item and not _non_empty_strings(item["description"]):
            errors.append(f"{where}.description: must be a non-empty list of strings")


def _check_join_specs(space: dict[str, Any], errors: list[str]) -> None:
    for i, spec in enumerate(_items(space, ("instructions", "join_specs"), errors)):
        where = f"instructions.join_specs[{i}]"
        aliases: list[str] = []
        for side in ("left", "right"):
            ref = spec.get(side)
            if not isinstance(ref, dict):
                errors.append(f"{where}.{side}: must be an object with identifier and alias")
                continue
            identifier = ref.get("identifier")
            if not isinstance(identifier, str) or not _THREE_PART.fullmatch(identifier):
                errors.append(f"{where}.{side}.identifier: must be a three-level name, got {identifier!r}")
            alias = ref.get("alias")
            if isinstance(alias, str) and alias:
                aliases.append(alias)
            else:
                errors.append(f"{where}.{side}.alias: required")
        sql = spec.get("sql")
        if not isinstance(sql, list) or len(sql) != 2 or not all(isinstance(s, str) for s in sql):
            errors.append(
                f"{where}.sql: must be exactly two strings — the join condition and a "
                "relationship annotation such as '--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--'"
            )
            continue
        condition, annotation = sql
        for alias in aliases:
            if f"`{alias}`." not in condition:
                errors.append(
                    f"{where}.sql[0]: join condition must reference alias {alias!r} "
                    f"backtick-quoted, e.g. `{alias}`.`column`"
                )
        if not _RELATIONSHIP.fullmatch(annotation.strip()):
            errors.append(
                f"{where}.sql[1]: must be a relationship annotation --rt=FROM_RELATIONSHIP_TYPE_"
                f"{{MANY_TO_ONE|ONE_TO_MANY|ONE_TO_ONE|MANY_TO_MANY}}--, got {annotation!r}"
            )


def validate_serialized_space(space: Any) -> list[str]:
    """Return every documented-rule violation in *space*; empty means valid.

    Runs offline. Paths in messages are relative to ``serialized_space``.
    """
    if not isinstance(space, dict):
        return ["serialized_space: must be an object"]
    errors: list[str] = []
    version = space.get("version")
    if version != SPACE_VERSION:
        errors.append(f"version: must be {SPACE_VERSION}, got {version!r}")

    for group in (_QUESTION_GROUP, _INSTRUCTION_GROUP):
        seen: dict[str, str] = {}
        for path in group:
            where = ".".join(path)
            items = _items(space, path, errors)
            _check_ids(where, items, seen, errors)
            if path[-1] == "sql_functions":
                keys: list[Any] = [(f.get("id"), f.get("identifier")) for f in items]
                if all(isinstance(a, str) and isinstance(b, str) for a, b in keys):
                    _check_sorted(where, keys, "(id, identifier)", errors)
            else:
                _check_sorted(where, [item.get("id") for item in items], "id", errors)

    texts = _items(space, ("instructions", "text_instructions"), [])
    if len(texts) > MAX_TEXT_INSTRUCTIONS:
        errors.append(
            f"instructions.text_instructions: {len(texts)} entries; at most "
            f"{MAX_TEXT_INSTRUCTIONS} text instruction is allowed per agent"
        )
    for path in _SNIPPETS:
        where = ".".join(path)
        for i, snippet in enumerate(_items(space, path, [])):
            if not _non_empty_strings(snippet.get("sql")):
                errors.append(f"{where}[{i}].sql: must be a non-empty list of SQL strings")
    for i, question in enumerate(_items(space, ("benchmarks", "questions"), [])):
        answer = question.get("answer")
        if (
            not isinstance(answer, list)
            or len(answer) != 1
            or not isinstance(answer[0], dict)
            or answer[0].get("format") != "SQL"
        ):
            errors.append(
                f"benchmarks.questions[{i}].answer: must be exactly one answer with format 'SQL'"
            )
    _check_data_sources(space, errors)
    _check_join_specs(space, errors)
    _check_limits(space, "", errors)
    return errors


def validate_config(config: GenieSpaceConfig) -> list[str]:
    """Return every problem in *config*; serialized_space paths are prefixed."""
    errors: list[str] = []
    if not config.title.strip():
        errors.append("title: required")
    if not config.warehouse_id.strip():
        errors.append("warehouse_id: required")
    errors.extend(f"serialized_space.{e}" for e in validate_serialized_space(config.serialized_space))
    return errors


# ---------------------------------------------------------------------------
# Config file IO
# ---------------------------------------------------------------------------


def _is_json(path: Path) -> bool:
    return path.suffix.lower() == ".json"


def _yaml() -> Any:
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.width = 4096  # keep SQL lines unwrapped
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _read_raw(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if _is_json(path) else _yaml().load(text)


def _write_raw(path: Path, data: Any) -> None:
    if _is_json(path):
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return
    buffer = io.StringIO()
    _yaml().dump(data, buffer)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def _plain(data: Any) -> Any:
    """Convert ruamel round-trip containers to plain dicts/lists."""
    return json.loads(json.dumps(data))


def load_config(path: Path) -> GenieSpaceConfig:
    """Read a Genie Agent config from YAML (default) or ``.json``."""
    try:
        data = _plain(_read_raw(path))
    except Exception as exc:
        raise GenieSpaceConfigError(f"{path}: cannot parse: {exc}") from exc
    if not isinstance(data, dict):
        raise GenieSpaceConfigError(f"{path}: must be a mapping of {', '.join(_CONFIG_KEYS)}")
    problems = [f"{k}: unknown key (expected one of {', '.join(_CONFIG_KEYS)})"
                for k in data if k not in _CONFIG_KEYS]
    for key in ("title", "warehouse_id"):
        if not isinstance(data.get(key), str):
            problems.append(f"{key}: required string")
    for key in ("description", "parent_path"):
        if data.get(key) is not None and not isinstance(data[key], str):
            problems.append(f"{key}: must be a string")
    if not isinstance(data.get("serialized_space"), dict):
        problems.append("serialized_space: required mapping")
    deployments = data.get("deployments", {})
    if not isinstance(deployments, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in deployments.items()
    ):
        problems.append("deployments: must map workspace host to space ID")
    if problems:
        raise GenieSpaceConfigError(f"{path}:\n  " + "\n  ".join(problems))
    return GenieSpaceConfig(
        title=data["title"],
        warehouse_id=data["warehouse_id"],
        serialized_space=data["serialized_space"],
        description=data.get("description"),
        parent_path=data.get("parent_path"),
        deployments=dict(deployments),
    )


def dump_config(config: GenieSpaceConfig, path: Path) -> None:
    """Write *config* as readable YAML (or JSON for a ``.json`` path)."""
    data: dict[str, Any] = {"title": config.title}
    if config.description is not None:
        data["description"] = config.description
    data["warehouse_id"] = config.warehouse_id
    if config.parent_path is not None:
        data["parent_path"] = config.parent_path
    if config.deployments:
        data["deployments"] = dict(config.deployments)
    data["serialized_space"] = config.serialized_space
    _write_raw(path, data)


def record_deployment(path: Path, host: str, space_id: str) -> None:
    """Record *space_id* for *host* in the file, keeping the rest of it (and YAML comments)."""
    data = _read_raw(path)
    deployments = data.get("deployments")
    if deployments is None:
        data["deployments"] = {host: space_id}
    else:
        deployments[host] = space_id
    _write_raw(path, data)


# ---------------------------------------------------------------------------
# Workspace operations (Genie Management API)
# ---------------------------------------------------------------------------


def workspace_host(ws: Any) -> str:
    return str(ws.config.host).rstrip("/")


def _document(title: str, description: str | None, warehouse_id: str | None,
              serialized_space: dict[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {"title": title, "warehouse_id": warehouse_id,
                           "serialized_space": serialized_space}
    if description:
        doc["description"] = description
    return doc


def _live_space_dict(space: Any) -> dict[str, Any]:
    if not space.serialized_space:
        raise GenieSpaceConfigError(
            f"Genie space {space.space_id} returned no serialized_space; "
            "exporting it needs edit permission on the space"
        )
    return json.loads(space.serialized_space)


def _find_by_title(ws: Any, title: str) -> list[str]:
    matches: list[str] = []
    page_token: str | None = None
    while True:
        response = ws.genie.list_spaces(page_token=page_token)
        matches.extend(s.space_id for s in response.spaces or [] if s.title == title)
        page_token = response.next_page_token
        if not page_token:
            return matches


@dataclass
class _Match:
    space: Any
    matched_by: str


def _resolve(ws: Any, config: GenieSpaceConfig) -> _Match | None:
    from databricks.sdk.errors import NotFound

    recorded = config.deployments.get(workspace_host(ws))
    if recorded:
        try:
            return _Match(ws.genie.get_space(recorded, include_serialized_space=True), "recorded id")
        except NotFound:
            click.echo(f"recorded space {recorded} not found on this workspace; matching by title",
                       err=True)
    matches = _find_by_title(ws, config.title)
    if len(matches) > 1:
        raise GenieSpaceConfigError(
            f"{len(matches)} Genie spaces are titled {config.title!r} ({', '.join(matches)}); "
            "record the one to manage under deployments"
        )
    if matches:
        return _Match(ws.genie.get_space(matches[0], include_serialized_space=True), "title")
    return None


def pull(ws: Any, space_id: str, existing: GenieSpaceConfig | None = None) -> GenieSpaceConfig:
    """Export a live space as a config, recording its ID for this workspace."""
    space = ws.genie.get_space(space_id, include_serialized_space=True)
    deployments = dict(existing.deployments) if existing else {}
    deployments[workspace_host(ws)] = space_id
    return GenieSpaceConfig(
        title=space.title,
        warehouse_id=space.warehouse_id,
        serialized_space=_live_space_dict(space),
        description=space.description or None,
        parent_path=existing.parent_path if existing else None,
        deployments=deployments,
    )


def _diff(before: dict[str, Any] | None, after: dict[str, Any]) -> str:
    def lines(doc: dict[str, Any] | None) -> list[str]:
        return json.dumps(doc, indent=2, sort_keys=True).splitlines() if doc else []

    return "\n".join(difflib.unified_diff(lines(before), lines(after), "live", "local", lineterm=""))


def plan(ws: Any, config: GenieSpaceConfig, warehouse_id: str | None = None) -> GenieSpacePlan:
    """Diff *config* against the space it resolves to on *ws*. Read-only."""
    errors = validate_config(config)
    if errors:
        raise GenieSpaceConfigError("invalid Genie Agent config:\n  " + "\n  ".join(errors))
    desired = _document(config.title, config.description, warehouse_id or config.warehouse_id,
                        config.serialized_space)
    match = _resolve(ws, config)
    if match is None:
        return GenieSpacePlan("create", None, None, _diff(None, desired))
    live = match.space
    diff = _diff(_document(live.title, live.description, live.warehouse_id, _live_space_dict(live)),
                 desired)
    return GenieSpacePlan("update" if diff else "noop", live.space_id, match.matched_by, diff)


def push(ws: Any, config: GenieSpaceConfig, *, warehouse_id: str | None = None,
         dry_run: bool = False) -> GenieSpacePlan:
    """Create or update the space for *config*. Never deletes; dry-run only plans."""
    result = plan(ws, config, warehouse_id)
    if dry_run or result.action == "noop":
        return result
    warehouse = warehouse_id or config.warehouse_id
    serialized = json.dumps(config.serialized_space)
    if result.action == "create":
        created = ws.genie.create_space(
            warehouse_id=warehouse, serialized_space=serialized, title=config.title,
            description=config.description, parent_path=config.parent_path,
        )
        result.space_id = created.space_id
    else:
        ws.genie.update_space(
            result.space_id, title=config.title, description=config.description,
            warehouse_id=warehouse, serialized_space=serialized,
        )
    return result


# ---------------------------------------------------------------------------
# CLI: apx-agent resources genie-space ...
# ---------------------------------------------------------------------------


def _client(profile: str | None) -> Any:
    from .cli import _require_sdk

    return _require_sdk(profile)


def _load_or_fail(path: Path) -> GenieSpaceConfig:
    try:
        return load_config(path)
    except GenieSpaceConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _echo_plan(result: GenieSpacePlan) -> None:
    target = f" {result.space_id} (matched by {result.matched_by})" if result.space_id else ""
    click.echo(f"{result.action}{target}")
    if result.diff:
        click.echo(result.diff)


_PROFILE = click.option("--profile", default=None, help="Databricks config profile (the target workspace).")
_WAREHOUSE = click.option("--warehouse", "warehouse_id", default=None,
                          help="SQL warehouse ID to use instead of the file's warehouse_id (promotion).")
_FILE = click.argument("file", type=click.Path(dir_okay=False, path_type=Path))


@click.group("genie-space")
def genie_space() -> None:
    """Genie Agents as code: validate, pull, plan and push a Genie Agent config file."""


@genie_space.command("validate")
@_FILE
def validate_cmd(file: Path) -> None:
    """Check FILE against the documented serialized_space rules. Offline."""
    errors = validate_config(_load_or_fail(file))
    if errors:
        raise click.ClickException(f"{file}: {len(errors)} problem(s):\n  " + "\n  ".join(errors))
    click.echo(f"{file}: valid")


@genie_space.command("pull")
@click.argument("space_id")
@_FILE
@_PROFILE
def pull_cmd(space_id: str, file: Path, profile: str | None) -> None:
    """Export live space SPACE_ID to FILE (YAML, or JSON for .json)."""
    existing = _load_or_fail(file) if file.exists() else None
    try:
        config = pull(_client(profile), space_id, existing)
    except GenieSpaceConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    dump_config(config, file)
    click.echo(f"wrote {file} from space {space_id}")
    for error in validate_config(config):
        click.echo(f"warning: {error}", err=True)


@genie_space.command("plan")
@_FILE
@_PROFILE
@_WAREHOUSE
def plan_cmd(file: Path, profile: str | None, warehouse_id: str | None) -> None:
    """Show what push would do: create, update (with a diff) or noop."""
    config = _load_or_fail(file)
    try:
        _echo_plan(plan(_client(profile), config, warehouse_id))
    except GenieSpaceConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@genie_space.command("push")
@_FILE
@_PROFILE
@_WAREHOUSE
@click.option("--dry-run", is_flag=True, help="Print the plan without changing the workspace.")
def push_cmd(file: Path, profile: str | None, warehouse_id: str | None, dry_run: bool) -> None:
    """Create or update the Genie Agent declared in FILE. Never deletes a space."""
    config = _load_or_fail(file)
    ws = _client(profile)
    try:
        result = push(ws, config, warehouse_id=warehouse_id, dry_run=dry_run)
    except GenieSpaceConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_plan(result)
    host = workspace_host(ws)
    if dry_run:
        click.echo("dry run: no changes made")
    elif result.space_id and config.deployments.get(host) != result.space_id:
        record_deployment(file, host, result.space_id)
        click.echo(f"recorded {result.space_id} for {host} in {file}")
