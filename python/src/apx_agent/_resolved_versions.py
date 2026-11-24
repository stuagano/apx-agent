"""Artifact versions APX resolved at startup, recorded on every agent trace (#1247).

Agent Bricks declares an artifact and MLflow holds its versions. When APX
resolves an exact version at startup (a Prompt Registry pin today), it records
it here once per process. ``safe_span`` then stamps every AGENT span from this
one place, so each artifact kind lands on traces the same way:

* ``apx.<kind>.key`` / ``apx.<kind>.uri`` / ``apx.<kind>.version`` as span
  attributes and trace tags, comma-joined in key order;
* the upstream MLflow link for that kind, where MLflow has one. For prompts
  that is the ``mlflow.linkedPrompts`` trace tag ``mlflow.genai.load_prompt``
  writes when it runs inside a trace. APX builds the value with MLflow's own
  ``update_linked_prompts_tag`` rather than its own format.

The store is a module global, not a ``ContextVar``: startup runs in the server
lifespan and requests run in other tasks, so a context-local value would not
reach request traces.

To add a kind (model or endpoint versions), add its three ``AuditAttrs`` keys to
``_TAG_KEYS`` and record it where APX resolves the version. Record only exact
versions, never a live alias.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._audit import AuditAttrs

# kind -> (key, uri, version) tag names. A kind missing here cannot be recorded.
_TAG_KEYS: dict[str, tuple[str, str, str]] = {
    "prompt": (AuditAttrs.PROMPT_KEY, AuditAttrs.PROMPT_URI, AuditAttrs.PROMPT_VERSION),
}


@dataclass(frozen=True)
class ResolvedVersion:
    """One exact artifact version APX resolved: ``prompts:/main.prompts.writer/7`` is
    kind ``prompt``, name ``main.prompts.writer``, version ``"7"``."""

    kind: str
    key: str
    name: str
    uri: str
    version: str


_RESOLVED: dict[str, list[ResolvedVersion]] = {}


def record_resolved(kind: str, versions: list[ResolvedVersion]) -> None:
    """Replace every recorded version of ``kind``. An empty list clears it."""
    if kind not in _TAG_KEYS:
        raise ValueError(f"No trace tag keys for artifact kind {kind!r}; add them to _TAG_KEYS.")
    wrong = [version for version in versions if version.kind != kind]
    if wrong:
        raise ValueError(f"record_resolved({kind!r}) got versions of kind {wrong[0].kind!r}.")
    if versions:
        _RESOLVED[kind] = sorted(versions, key=lambda version: version.key)
    else:
        _RESOLVED.pop(kind, None)


def resolved_versions() -> list[ResolvedVersion]:
    """Everything recorded in this process, by kind then key."""
    return [version for kind in sorted(_RESOLVED) for version in _RESOLVED[kind]]


def clear_resolved() -> None:
    """Forget every recorded version. Tests call this so later traces stay untagged."""
    _RESOLVED.clear()


def version_tags(versions: list[ResolvedVersion]) -> dict[str, str]:
    """``apx.<kind>.*`` tags for ``versions``, comma-joined in key order within each kind."""
    tags: dict[str, str] = {}
    for kind in sorted({version.kind for version in versions}):
        group = sorted((version for version in versions if version.kind == kind), key=lambda item: item.key)
        key_tag, uri_tag, version_tag = _TAG_KEYS[kind]
        tags[key_tag] = ",".join(version.key for version in group)
        tags[uri_tag] = ",".join(version.uri for version in group)
        tags[version_tag] = ",".join(version.version for version in group)
    return tags


def upstream_trace_tags(versions: list[ResolvedVersion]) -> dict[str, str]:
    """The native MLflow trace link for each kind that has one (prompts only today)."""
    prompts = [version for version in versions if version.kind == "prompt"]
    if not prompts:
        return {}
    try:
        from mlflow.tracing.constant import TraceTagKey
        from mlflow.tracing.utils.prompt import update_linked_prompts_tag
    except ImportError:
        # An MLflow without the native link: the apx.prompt.* tags still record the pin.
        return {}

    # update_linked_prompts_tag reads only .name and .version, so a
    # ResolvedVersion stands in for the PromptVersion load_prompt returned.
    return {TraceTagKey.LINKED_PROMPTS: update_linked_prompts_tag(None, prompts)}


def stamp_resolved_versions(span: Any) -> None:
    """Stamp every recorded version on ``span`` and its trace. No versions → no-op.

    The ``apx.*`` keys go on every AGENT span. The upstream link is written
    only from the root span, before agent code runs: a later
    ``mlflow.genai.load_prompt`` in the same trace merges into that tag, and a
    nested AGENT span would otherwise overwrite what it merged.
    """
    from ._mlflow_tracing import set_span_attribute, set_trace_tags

    versions = resolved_versions()
    if not versions:
        return
    tags = version_tags(versions)
    for key, value in tags.items():
        set_span_attribute(span, key, value)
    if getattr(span, "parent_id", None) is None:
        tags = {**tags, **upstream_trace_tags(versions)}
    set_trace_tags(tags)
