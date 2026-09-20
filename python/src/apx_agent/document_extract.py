"""document_extract_tool — parse a UC volume file and extract a declared schema.

Compiles ``ai_parse_document`` + ``ai_extract`` against a required SQL
warehouse. The LLM sees a path *inside* a declared Unity Catalog volume; APX
binds that path and the committed extract schema. It never concatenates the
path into SQL, never copies PDF bytes into the App, and never falls back to
PyMuPDF / vision.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time
and ``get_type_hints()`` in ``_inspection.py`` sees the real ``Annotated[...]``
type, not a string.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._errors import ToolError
from ._sql import run_sql
from ._tool_config import ToolConfigError

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class _VolumeRef:
    fqn: str
    root: str

_EXTRACT_SQL = """\
WITH parsed AS (
  SELECT ai_parse_document(content) AS doc
  FROM READ_FILES(:volume_path, format => 'binaryFile')
)
SELECT ai_extract(doc, :extract_schema) AS extracted
FROM parsed
"""


def _posix_norm(path: str) -> str:
    """Collapse ``.`` / ``..`` without touching the filesystem.

    Volume paths exist in Unity Catalog, not necessarily on the App local disk,
    so ``Path.resolve()`` is the wrong primitive.
    """
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def _parse_volume(volume: str) -> _VolumeRef:
    if not isinstance(volume, str) or not volume.strip():
        raise ToolConfigError(
            "volume is required and must be catalog.schema.volume."
        )
    parts = volume.strip().split(".")
    if len(parts) != 3 or not all(_IDENT.match(part) for part in parts):
        raise ToolConfigError(
            f"volume must be catalog.schema.volume with SQL identifiers; "
            f"got {volume!r}."
        )
    catalog, schema, name = parts
    return _VolumeRef(
        fqn=f"{catalog}.{schema}.{name}",
        root=f"/Volumes/{catalog}/{schema}/{name}",
    )


def _load_schema_object(schema: Any) -> dict[str, Any]:
    if isinstance(schema, dict):
        return schema
    if not isinstance(schema, str) or not schema.strip():
        raise ToolConfigError(
            "schema is required and must be a JSON object or a path to one."
        )
    text = schema.strip()
    if text.startswith("{"):
        try:
            obj: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolConfigError(f"schema JSON is invalid: {exc}") from exc
        if not isinstance(obj, dict):
            raise ToolConfigError("schema JSON must be an object.")
        return obj
    path = Path(text)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise ToolConfigError(f"schema file not found: {text}")
    try:
        obj = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ToolConfigError(f"schema file is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ToolConfigError(f"schema file {text} must contain a JSON object.")
    return obj


def bind_volume_path(volume_root: str, user_path: str) -> str:
    """Return the full ``/Volumes/...`` path if ``user_path`` stays inside the volume."""
    if not isinstance(user_path, str) or not user_path.strip():
        raise ToolError("path is required and must be a file inside the declared volume.")
    if "\x00" in user_path:
        raise ToolError("path must not contain a NUL byte.")
    raw = user_path.strip()
    root_norm = _posix_norm(volume_root)
    candidate = raw if raw.startswith("/") else f"{root_norm}/{raw}"
    resolved = _posix_norm(candidate)
    if resolved != root_norm and not resolved.startswith(root_norm + "/"):
        raise ToolError(
            f"path {user_path!r} is outside the declared volume {volume_root}."
        )
    if resolved == root_norm:
        raise ToolError(
            f"path {user_path!r} must name a file inside the declared volume, "
            "not the volume root."
        )
    return resolved


def document_extract_tool(
    *,
    warehouse_id: str,
    volume: str,
    schema: dict[str, Any] | str,
    name: str = "extract_document",
    description: str | None = None,
) -> Any:
    """Return a tool that parses one volume file and extracts a declared schema.

    The LLM supplies a path inside ``volume``. APX binds that full
    ``/Volumes/...`` path and the factory-time extract schema into owned SQL::

        WITH parsed AS (
          SELECT ai_parse_document(content) AS doc
          FROM READ_FILES(:volume_path, format => 'binaryFile')
        )
        SELECT ai_extract(doc, :extract_schema) AS extracted
        FROM parsed

    Identity is the calling user. There is no App-side PDF parse and no
    vision / PyMuPDF fallback — warehouse or AI-function failure raises
    ``ToolError``.

    Args:
        warehouse_id: Required SQL warehouse that can run ``ai_parse_document``
            and ``ai_extract``. Declared as a ``DatabricksSQLWarehouse`` resource.
        volume: Unity Catalog volume as ``catalog.schema.volume``.
        schema: JSON Schema object, a JSON-object string, or a path to a
            committed schema file. Resolved once at factory time.
        name: Tool name shown to the LLM. Defaults to ``"extract_document"``.
        description: Tool description shown to the LLM.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    if not isinstance(warehouse_id, str) or not warehouse_id.strip():
        raise ToolConfigError("warehouse_id is required.")
    warehouse_id = warehouse_id.strip()
    volume_ref = _parse_volume(volume)
    volume_fqn = volume_ref.fqn
    volume_root = volume_ref.root
    schema_obj = _load_schema_object(schema)
    schema_json = json.dumps(schema_obj, separators=(",", ":"), sort_keys=True)

    _desc = description or (
        f"Extract structured fields from a document in Unity Catalog volume "
        f"`{volume_fqn}` using ai_parse_document and ai_extract. Provide a path "
        f"inside that volume. Runs as the calling user against warehouse "
        f"`{warehouse_id}`."
    )

    async def _extract_document(path: str, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        volume_path = bind_volume_path(volume_root, path)
        try:
            rows = run_sql(
                ws,
                _EXTRACT_SQL,
                warehouse_id=warehouse_id,
                parameters=[
                    {"name": "volume_path", "value": volume_path, "type": "STRING"},
                    {"name": "extract_schema", "value": schema_json, "type": "STRING"},
                ],
            )
        except ToolError:
            raise
        except Exception as exc:
            logger.warning("document_extract failed for %s: %s", volume_path, exc)
            raise ToolError(
                f"ai_parse_document / ai_extract failed for {volume_path}: {exc}"
            ) from exc
        if not rows:
            raise ToolError(
                f"ai_parse_document / ai_extract returned no rows for {volume_path}."
            )
        row = rows[0]
        extracted = row.get("extracted", row)
        return {
            "path": volume_path,
            "extracted": extracted,
            "citations": row.get("citations"),
        }

    return build_tool(
        _extract_document,
        name=name,
        description=_desc,
        resources=[ResourceSpec("sql_warehouse", warehouse_id)],
    )
