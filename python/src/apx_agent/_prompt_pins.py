"""Load version-pinned Prompt Registry bindings from agent.toml.

Agent Bricks owns the binding table. A value stays ``catalog.schema.name`` so
``AgentProject.load`` and ``prompt_registry.schema_of`` keep working (ADR 0001:
no new ``agent.toml`` keys and no APX binding API). The pin is the upstream
MLflow version URI in a trailing comment on that line:

```toml
[experimental.prompts]
writer = "main.prompts.writer" # prompts:/main.prompts.writer/7
```

Name-only values and ``@alias`` comments are not loaded. APX does not follow a
live alias, and it does not edit the registry.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from ._agents import BaseAgent
    from ._doctor import Check

# ponytail: placeholders are detected and left unfilled. How a caller supplies
# template variables is an open question on #1245; the text stays a named template.
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}|\{(\w+)\}")
_URI_TOKEN = re.compile(r"prompts:/(\S+)")
_BINDING_LINE = re.compile(
    r"""^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<quote>['"])(?P<value>.*?)(?P=quote)\s*(?P<comment>#.*)?$"""
)
_PROMPTS_HEADER = "[experimental.prompts]"
_PROMPT_TAGS: ContextVar[dict[str, str] | None] = ContextVar("apx_prompt_tags", default=None)
_APPLIED: WeakKeyDictionary[BaseAgent, dict[str, LoadedPrompt]] = WeakKeyDictionary()

_GRANT_FIX = (
    "Grant USE SCHEMA, EXECUTE, CREATE FUNCTION, and MANAGE on {schema}. "
    "agentbricks deploy grants these on its own path only; apps_api and App Space do not."
)


class PromptPinError(ValueError):
    """``agent.toml`` has a prompt binding APX cannot pin."""


class PromptLoadError(RuntimeError):
    """A pinned prompt could not be loaded, or the loaded version disagreed with the pin."""


@dataclass(frozen=True)
class PromptBinding:
    """One ``[experimental.prompts]`` entry.

    ``uri`` is set only for an exact ``prompts:/name/version`` pin. ``error`` is
    set when the value would break upstream grant wiring or the comment does
    not match the bound name.
    """

    key: str
    name: str
    uri: str | None = None
    version: int | None = None
    alias: bool = False
    error: str | None = None


@dataclass(frozen=True)
class LoadedPrompt:
    """A pin resolved by ``mlflow.genai.load_prompt``."""

    key: str
    name: str
    uri: str
    version: int
    template: str
    variables: tuple[str, ...]


@dataclass(frozen=True)
class PinPullRequest:
    """The branch, text, and commands that publish a pin bump."""

    branch: str
    title: str
    body: str
    commands: list[list[str]]
    binding: PromptBinding


def current_prompt_tags() -> dict[str, str]:
    """Tags for the prompts applied in this process, or ``{}`` when none are pinned."""
    tags = _PROMPT_TAGS.get()
    if not tags:
        return {}
    return dict(tags)


def reset_prompt_tags() -> None:
    """Drop process-local prompt tags. Tests call this so later traces stay untagged."""
    _PROMPT_TAGS.set(None)


def read_prompt_bindings(project_root: Path) -> list[PromptBinding]:
    """Bindings in ``project_root / agent.toml``.

    An absent file or an absent ``[experimental.prompts]`` table is an empty
    list. A manifest Agent Bricks cannot load raises ``PromptPinError``.
    """
    path = project_root / "agent.toml"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if not any(line.strip() == _PROMPTS_HEADER for line in text.splitlines()):
        return []
    try:
        from databricks_agentbricks.projects.agent_project import AgentProject
    except ImportError as exc:
        raise PromptPinError(
            "Reading [experimental.prompts] needs the apx-agent[agentbricks] extra."
        ) from exc
    try:
        project = AgentProject.load(project_root)
    except Exception as exc:
        from databricks_agentbricks.errors import AgentCliError

        if not isinstance(exc, AgentCliError):
            raise
        raise PromptPinError(str(exc)) from exc
    lines = {match.group("key"): match for match in _section_matches(text)}
    bindings: list[PromptBinding] = []
    for key, name in project.prompts.items():
        bindings.append(_binding_from_line(key, str(name), lines.get(key)))
    return bindings


def loaded_prompts(agent: BaseAgent) -> list[LoadedPrompt]:
    """Pins already applied to ``agent``, in key order."""
    stored = _APPLIED.get(agent)
    if stored is None:
        return []
    return [stored[key] for key in sorted(stored)]


def apply_pinned_prompts(
    agent: BaseAgent,
    project_root: Path,
    *,
    loader: Callable[[str], Any] | None = None,
) -> list[LoadedPrompt]:
    """Load every version pin and, when one plain prompt is selected, use it as instructions.

    Name-only and alias bindings are ignored. A second call with the same URIs
    does not contact the registry again. Startup calls this from ``compile_agent``
    (when ``project_root`` is set) and from ``setup_agent``.
    """
    bindings = read_prompt_bindings(project_root)
    _raise_binding_errors(bindings)
    pins = [binding for binding in bindings if binding.uri is not None and binding.version is not None]
    if not pins:
        existing = loaded_prompts(agent)
        if existing:
            _PROMPT_TAGS.set(prompt_run_tags(existing))
        return existing
    current = loaded_prompts(agent)
    if [prompt.uri for prompt in current] == [binding.uri for binding in sorted(pins, key=lambda item: item.key)]:
        _PROMPT_TAGS.set(prompt_run_tags(current))
        return current
    load = loader if loader is not None else _default_loader
    loaded = [_load_one(binding, load) for binding in sorted(pins, key=lambda item: item.key)]
    _APPLIED[agent] = {prompt.key: prompt for prompt in loaded}
    chosen = _choose_instructions(loaded)
    if chosen is not None and "_instructions" in vars(agent):
        setattr(agent, "_instructions", chosen.template)
    _PROMPT_TAGS.set(prompt_run_tags(loaded))
    return loaded


def prompt_run_tags(loaded: list[LoadedPrompt]) -> dict[str, str]:
    """Trace and eval tags for ``loaded``, keys sorted so the order is stable."""
    if not loaded:
        return {}
    from ._audit import AuditAttrs

    ordered = sorted(loaded, key=lambda prompt: prompt.key)
    return {
        AuditAttrs.PROMPT_KEY: ",".join(prompt.key for prompt in ordered),
        AuditAttrs.PROMPT_URI: ",".join(prompt.uri for prompt in ordered),
        AuditAttrs.PROMPT_VERSION: ",".join(str(prompt.version) for prompt in ordered),
    }


def check_prompt_pins(
    project_root: Path,
    *,
    online: bool,
    auth_ok: bool,
    loader: Callable[[str], Any] | None = None,
) -> list[Check]:
    """Doctor rows for ``[experimental.prompts]``.

    Offline, or when Databricks auth is not configured, pinned rows are reported
    and the registry is not contacted. Name-only and alias rows warn that APX
    does not load them.
    """
    from ._doctor import Check, Status

    try:
        bindings = read_prompt_bindings(project_root)
    except PromptPinError as exc:
        return [Check("Prompt registry", Status.FAIL, str(exc), "Fix [experimental.prompts] in agent.toml.")]
    if not bindings:
        return []
    rows: list[Check] = []
    for binding in bindings:
        rows.append(_check_one(binding, online=online, auth_ok=auth_ok, loader=loader))
    return rows


def bump_prompt_pin(project_root: Path, key: str, version: int) -> PromptBinding:
    """Replace the pinned version of ``key``. Does not create a binding or contact the registry.

    The three-part name is left untouched. The rewritten file is read back and
    must show the new URI before this returns.
    """
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PromptPinError(f"Prompt version must be a positive integer, got {version!r}.")
    current = _require_pin(project_root, key)
    if current.version == version:
        raise PromptPinError(f"{key} is already pinned at {current.uri}.")
    path = project_root / "agent.toml"
    text = path.read_text(encoding="utf-8")
    old_uri = current.uri
    new_uri = f"prompts:/{current.name}/{version}"
    if old_uri is None or old_uri not in text:
        raise PromptPinError(f"{current.uri} is not in {path}.")
    path.write_text(text.replace(old_uri, new_uri, 1), encoding="utf-8")
    updated = _require_pin(project_root, key)
    if updated.uri != new_uri or updated.version != version:
        raise PromptPinError(f"Rewrote {path} but the pin did not become {new_uri}.")
    return updated


def pin_pull_request(project_root: Path, key: str, version: int, *, base: str = "main") -> PinPullRequest:
    """Bump ``key`` and describe the git commands that open the promotion pull request."""
    before = _require_pin(project_root, key)
    updated = bump_prompt_pin(project_root, key, version)
    branch = f"pin-{key}-v{version}"
    title = f"Pin {key} to {updated.uri}"
    body = (
        f"Promote `{key}` from `{before.uri}` to `{updated.uri}`.\n\n"
        "The binding value stays the Unity Catalog name so `agentbricks deploy` can grant "
        "on the prompt schema. This pull request only bumps the pinned version. "
        "APX does not move a registry alias.\n"
    )
    root = str(project_root)
    commands = [
        ["git", "-C", root, "checkout", "-b", branch],
        ["git", "-C", root, "add", "--", "agent.toml"],
        ["git", "-C", root, "commit", "-m", title],
        ["gh", "pr", "create", "--title", title, "--body", body, "--base", base],
    ]
    return PinPullRequest(branch=branch, title=title, body=body, commands=commands, binding=updated)


def open_prompt_pin_pr(
    project_root: Path,
    key: str,
    version: int,
    *,
    base: str = "main",
    runner: Callable[[list[str], Path], str] | None = None,
) -> str:
    """Bump the pin, then run the git and ``gh pr create`` commands.

    ``runner`` receives each command and the project root. The default runner
    uses ``subprocess`` in that directory. Tests pass a runner so nothing is
    pushed. Returns the last command's stdout (the pull request URL from ``gh``).
    """
    plan = pin_pull_request(project_root, key, version, base=base)
    run = runner if runner is not None else _run_command
    output = ""
    for command in plan.commands:
        output = run(command, project_root)
    return output.strip()


def _run_command(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout


def _require_pin(project_root: Path, key: str) -> PromptBinding:
    bindings = read_prompt_bindings(project_root)
    _raise_binding_errors(bindings)
    match = next((binding for binding in bindings if binding.key == key), None)
    if match is None:
        raise PromptPinError(f"No [experimental.prompts] binding {key!r}.")
    if match.uri is None or match.version is None:
        hint = f'{key} = "{match.name}" # prompts:/{match.name}/<version>'
        if match.alias:
            raise PromptPinError(
                f"{key} comments an @alias. APX does not follow aliases. Pin an exact version: {hint}"
            )
        raise PromptPinError(
            f"{key} is not pinned to a version, so APX does not load it. Add a trailing comment: {hint}"
        )
    return match


def _raise_binding_errors(bindings: list[PromptBinding]) -> None:
    errors = [binding.error for binding in bindings if binding.error]
    if errors:
        raise PromptPinError("; ".join(errors))


def _check_one(
    binding: PromptBinding,
    *,
    online: bool,
    auth_ok: bool,
    loader: Callable[[str], Any] | None,
) -> Check:
    from ._doctor import Check, Status

    name = f"Prompt registry ({binding.key})"
    if binding.error:
        return Check(name, Status.FAIL, binding.error, "Fix the [experimental.prompts] line in agent.toml.")
    if binding.uri is None or binding.version is None:
        if binding.alias:
            detail = f"{binding.key} ({binding.name}) comments an @alias; APX does not load it."
            fix = f'Pin an exact version: {binding.key} = "{binding.name}" # prompts:/{binding.name}/<version>'
        else:
            detail = f"{binding.key} ({binding.name}) is not pinned to a version; APX does not load it."
            fix = f'Add a trailing comment: {binding.key} = "{binding.name}" # prompts:/{binding.name}/<version>'
        return Check(name, Status.WARN, detail, fix)
    if not online or not auth_ok:
        why = "registry not contacted" if not online else "Databricks auth is not configured; registry not contacted"
        return Check(
            name,
            Status.SKIP,
            f"{binding.key} pinned at {binding.uri}; {why}",
            "Re-run doctor with workspace auth and without --offline to verify access.",
        )
    load = loader if loader is not None else _default_loader
    try:
        loaded = _load_one(binding, load)
    except PromptLoadError as exc:
        return Check(name, Status.FAIL, str(exc), _GRANT_FIX.format(schema=_schema(binding.name)))
    if loaded.variables:
        detail = (
            f"{binding.key} loaded {loaded.uri} as a template "
            f"(placeholders: {', '.join(loaded.variables)}); not applied as instructions"
        )
    else:
        detail = f"{binding.key} loaded {loaded.uri}"
    return Check(name, Status.OK, detail, None)


def _load_one(binding: PromptBinding, loader: Callable[[str], Any]) -> LoadedPrompt:
    if binding.uri is None or binding.version is None:
        raise PromptLoadError(f"{binding.key} has no version pin.")
    try:
        prompt = loader(binding.uri)
    except PromptLoadError:
        raise
    except Exception as exc:
        raise _load_failure(binding, exc) from exc
    template = _template_of(prompt, binding.uri)
    version = _as_version(prompt.version, binding.uri)
    if version != binding.version:
        raise PromptLoadError(
            f"Pinned {binding.uri} loaded version {version}, not {binding.version}."
        )
    return LoadedPrompt(
        key=binding.key,
        name=binding.name,
        uri=binding.uri,
        version=version,
        template=template,
        variables=tuple(_placeholders(template)),
    )


def _load_failure(binding: PromptBinding, exc: BaseException) -> PromptLoadError:
    text = str(exc).strip()
    if len(text) > 200:
        text = text[:200] + "..."
    return PromptLoadError(
        f"Cannot load pinned prompt {binding.key} ({binding.uri}): {type(exc).__name__}: {text}. "
        f"The loading identity needs USE SCHEMA, EXECUTE, CREATE FUNCTION, and MANAGE on {_schema(binding.name)}. "
        "agentbricks deploy grants those on its own path; apps_api and App Space do not."
    )


def _default_loader(uri: str) -> Any:
    import mlflow
    from mlflow.genai import load_prompt

    mlflow.set_registry_uri("databricks-uc")
    return load_prompt(uri)


def _template_of(prompt: Any, uri: str) -> str:
    template = prompt.template
    if not isinstance(template, str):
        raise PromptLoadError(f"Pinned prompt {uri} did not return a template string.")
    return template


def _schema(name: str) -> str:
    from databricks_agentkit.runtime.prompt_registry import schema_of

    return schema_of(name)


def _as_version(value: Any, uri: str) -> int:
    if isinstance(value, bool):
        raise PromptLoadError(f"Pinned prompt {uri} returned version {value!r}.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit() and not (len(value) > 1 and value.startswith("0")):
        return int(value)
    raise PromptLoadError(f"Pinned prompt {uri} returned version {value!r}.")


def _placeholders(template: str) -> list[str]:
    found: list[str] = []
    for jinja, python in _PLACEHOLDER.findall(template):
        name = jinja if jinja else python
        if name not in found:
            found.append(name)
    return found


def _choose_instructions(loaded: list[LoadedPrompt]) -> LoadedPrompt | None:
    plain = [prompt for prompt in loaded if not prompt.variables]
    for prompt in plain:
        if prompt.key == "instructions":
            return prompt
    if len(loaded) == 1 and len(plain) == 1:
        return plain[0]
    return None


def _upstream_name(name: str) -> bool:
    if any(char.isspace() or char in "/:@" for char in name):
        return False
    parts = name.split(".")
    return len(parts) == 3 and all(parts)


def _binding_from_line(key: str, name: str, match: re.Match[str] | None) -> PromptBinding:
    if not _upstream_name(name):
        return PromptBinding(key=key, name=name, error=_bad_value(key, name))
    if match is None:
        return PromptBinding(
            key=key,
            name=name,
            error=(
                f"{key} is not a single line under [experimental.prompts], so its version comment "
                "cannot be read. Keep it on one line: "
                f'{key} = "{name}" # prompts:/{name}/<version>'
            ),
        )
    if match.group("value") != name:
        return PromptBinding(
            key=key,
            name=name,
            error=f"{key} line value {match.group('value')!r} does not match the loaded name {name!r}.",
        )
    return _classify_comment(key, name, match.group("comment"))


def _bad_value(key: str, value: str) -> str:
    suggested = _suggest_name(value)
    example = (
        f'{key} = "{suggested}" # {value}'
        if suggested is not None and value.startswith("prompts:/") and "@" not in value
        else f'{key} = "catalog.schema.name" # prompts:/catalog.schema.name/<version>'
    )
    return (
        f"[experimental.prompts] {key} = {value!r} is not a catalog.schema.name. "
        "agentbricks deploy grants with schema_of on that value, so a URI in the value breaks the grant. "
        f"Keep the three-part name and pin the version in a trailing comment: {example}"
    )


def _suggest_name(value: str) -> str | None:
    if not value.startswith("prompts:/"):
        return None
    rest = value[len("prompts:/"):]
    if "/" not in rest or "@" in rest:
        return None
    name, version = rest.rsplit("/", 1)
    if not version.isdigit() or not _upstream_name(name):
        return None
    return name


def _classify_comment(key: str, name: str, comment: str | None) -> PromptBinding:
    if comment is None:
        return PromptBinding(key=key, name=name)
    tokens = _URI_TOKEN.findall(comment)
    if not tokens:
        return PromptBinding(key=key, name=name)
    pins: list[tuple[str, int]] = []
    aliases = False
    for token in tokens:
        if "@" in token or "/" not in token:
            aliases = True
            continue
        prompt_name, raw_version = token.rsplit("/", 1)
        if not raw_version.isdigit() or raw_version.startswith("0"):
            return PromptBinding(
                key=key,
                name=name,
                error=f"{key} version in {comment.strip()!r} must be a positive integer without leading zeros.",
            )
        if prompt_name != name:
            return PromptBinding(
                key=key,
                name=name,
                error=(
                    f"{key} is bound to {name} but the comment pins prompts:/{token}. "
                    "The URI name and the binding value must be the same prompt."
                ),
            )
        pins.append((f"prompts:/{token}", int(raw_version)))
    if pins and aliases:
        return PromptBinding(
            key=key,
            name=name,
            error=f"{key} comment mixes a version pin and an @alias. Pin only prompts:/{name}/<version>.",
        )
    if len(pins) > 1:
        return PromptBinding(key=key, name=name, error=f"{key} comment pins more than one version.")
    if not pins:
        return PromptBinding(key=key, name=name, alias=aliases)
    uri, version = pins[0]
    return PromptBinding(key=key, name=name, uri=uri, version=version)


def _section_matches(text: str) -> list[re.Match[str]]:
    capturing = False
    matches: list[re.Match[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _PROMPTS_HEADER:
            capturing = True
            continue
        if capturing and stripped.startswith("[") and stripped.endswith("]"):
            break
        if not capturing:
            continue
        match = _BINDING_LINE.match(stripped)
        if match is not None:
            matches.append(match)
    return matches
