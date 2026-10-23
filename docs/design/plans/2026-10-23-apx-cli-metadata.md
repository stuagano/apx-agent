# Sub-project 3b-1: homes for CLI metadata — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI metadata keys get their own homes. `profile` and `experiment` go to `[tool.apx]`. The registration keys go to `[tool.apx.deploy]`. The writers edit only their own section, and `migrate-config` moves these keys.

**Architecture:**
- **Key lists.** `_inspection.py` defines the two allow-lists (`DEV_KEYS`, `REGISTRATION_KEYS`).
- **Reader.** `cli._read_apx_agent_config` keeps its name and flat-dict result. It merges `[tool.apx.agent]`, then the allow-listed keys from the new tables, then `.apx.local`.
- **Writer.** A section-aware `_set_toml_key` replaces `_save_to_pyproject`'s whole-file regex.
- **Migration.** `_migrate_config.plan_migration` routes these keys into their new tables. The CLI command migrates `.apx.local`, with a backup, and also reads back the metadata dict.

**Tech Stack:** Python ≥3.10, `tomllib`, click, pytest, ctk.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "Sub-project 3b-1: homes for CLI metadata".

## Global Constraints

- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate: `make check`, plus `pre-commit run --files <changed files>`. `--all-files` fails on main for unrelated example lint debt.
  - Before pushing, also run the PR CI subset under xdist: `uv run pytest $(bash ../scripts/pr-pytest-paths.sh) -n auto --timeout=90 --timeout-method=thread`. In zsh, word-split it with `${=paths}`.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **Key lists, verbatim:**
  - `DEV_KEYS = ("profile", "experiment")`
  - `REGISTRATION_KEYS = ("registered_model", "catalog", "schema", "supervisor_id", "registry_table", "tools_table")`
- **Conflict message, verbatim:** `{key} is declared in both [tool.apx.agent] and {home} with different values; keep {home}.`, where `{home}` is `[tool.apx]` or `[tool.apx.deploy]`.
- **Never read the older `[tool.apx.deploy]` keys** (`target`, `bundle_name`, `app_name`) into this dict. `[tool.apx.metadata]` and other `[tool.apx.*]` tables are untouched.
- **Commits** end with `Co-authored-by: Isaac <no-reply@databricks.com>`. Use long flags (`git commit --quiet --message …`), and don't combine `grep -n` with `git commit` in one command (a hook misreads it). Never `git stash`.

## Review Focus

1. **A `[tool.apx]` header that already exists with other keys** (or a `[tool.apx.metadata]` table next to it). Expected: the writer and migration add `profile`/`experiment` without touching other keys or tables. Pinned in Tasks 2 and 3.
2. **`.apx.local` with only the old `[tool.apx.agent] profile`, and no `pyproject.toml` `[tool.apx.agent]`** (a project that's already migrated). Expected: `migrate-config` still migrates `.apx.local`, instead of returning "nothing to migrate". Pinned in Task 3.
3. **A key quoted with an embedded `"`** (a profile name containing a quote). Expected: the writer escapes it, and `tomllib` reads it back. Pinned in Task 2.
4. **A malformed `.apx.local`.** Expected: the reader treats it as empty, as today, with no crash. Pinned in Task 1.
5. **A key in `[tool.apx.agent]` and its new home with equal values.** Expected: no conflict. Pinned in Task 1.

---

### Task 1: The reader learns the new homes

**Files:**
- Modify: `python/src/apx_agent/_inspection.py` (add the key lists near `_AGENT_SECTION`)
- Modify: `python/src/apx_agent/cli.py:214-254` (`_read_apx_agent_config`)
- Test: `python/tests/test_cli_metadata.py` (create)

**Interfaces:**
- Produces: `_inspection.DEV_KEYS` and `_inspection.REGISTRATION_KEYS` (tuples of str). `_read_apx_agent_config(pyproject_path: Path | None = None) -> dict[str, Any]` has the same signature, and raises `click.ClickException` on a conflict.

- [ ] **Step 1: Write the failing tests** (`python/tests/test_cli_metadata.py`)

```python
"""CLI metadata homes: [tool.apx] (dev loop) and [tool.apx.deploy] (registration) — sub-project 3b-1."""

from __future__ import annotations

import tomllib
from pathlib import Path

import click
import pytest

from apx_agent.cli import _read_apx_agent_config


def _write(tmp_path: Path, body: str, local: str | None = None) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "p"\nversion = "0"\n\n' + body)
    if local is not None:
        (tmp_path / ".apx.local").write_text(local)
    return path


@pytest.mark.parametrize(("body", "key", "value"), [
    ('[tool.apx]\nprofile = "dev"\n', "profile", "dev"),
    ('[tool.apx]\nexperiment = "/Users/x/exp"\n', "experiment", "/Users/x/exp"),
    ('[tool.apx.deploy]\nregistered_model = "c.s.m"\n', "registered_model", "c.s.m"),
    ('[tool.apx.deploy]\nsupervisor_id = "sup-1"\n', "supervisor_id", "sup-1"),
    ('[tool.apx.agent]\nregistered_model = "c.s.m"\n', "registered_model", "c.s.m"),
])
def test_each_key_resolves_from_its_new_or_old_home(tmp_path: Path, body: str, key: str, value: str) -> None:
    assert _read_apx_agent_config(_write(tmp_path, body))[key] == value


def test_apx_local_wins_and_is_read_from_both_layouts(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx]\nprofile = "committed"\n', local='[tool.apx]\nprofile = "mine"\n')
    assert _read_apx_agent_config(path)["profile"] == "mine"
    (tmp_path / ".apx.local").write_text('[tool.apx.agent]\nprofile = "legacy-local"\n')
    assert _read_apx_agent_config(path)["profile"] == "legacy-local"


def test_new_home_overrides_the_old_table_only_when_equal(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx.agent]\nprofile = "a"\n\n[tool.apx]\nprofile = "a"\n')
    assert _read_apx_agent_config(path)["profile"] == "a"
    path = _write(tmp_path, '[tool.apx.agent]\nprofile = "a"\n\n[tool.apx]\nprofile = "b"\n')
    with pytest.raises(click.ClickException,
                       match=r"profile is declared in both \[tool.apx.agent\] and \[tool.apx\] with different values; keep \[tool.apx\]."):
        _read_apx_agent_config(path)


def test_older_deploy_keys_never_reach_the_dict(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx.agent]\ntarget = "durable_agent_server"\n\n'
                            '[tool.apx.deploy]\ntarget = "apps"\nbundle_name = "b"\napp_name = "a"\nunknown = 1\n')
    config = _read_apx_agent_config(path)
    assert config == {"target": "durable_agent_server"}


def test_malformed_apx_local_reads_as_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx]\nprofile = "dev"\n', local="not = [valid toml")
    assert _read_apx_agent_config(path)["profile"] == "dev"


def test_every_in_repo_example_reads_the_same_as_its_agent_table() -> None:
    root = Path(__file__).resolve().parents[1]
    for path in sorted(p for p in root.glob("examples/**/pyproject.toml") if ".venv" not in p.parts):
        legacy = tomllib.loads(path.read_text()).get("tool", {}).get("apx", {}).get("agent", {})
        assert _read_apx_agent_config(path) == legacy, path
```

The last test assumes no example has an `.apx.local`. Check with `find python/examples -name .apx.local`. If one exists, merge its `[tool.apx.agent]` into `legacy` in the test.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_cli_metadata.py -q -p no:cacheprovider`
Expected: FAIL on the new-home cases (`KeyError: 'profile'`) and on the conflict case (DID NOT RAISE). The old-home, older-deploy-keys, malformed and examples tests may already pass, because they describe today's behavior.

- [ ] **Step 3: Implement.**

In `_inspection.py`, directly after `_AGENT_SECTION = ("tool", "apx", "agent")`:

```python
DEV_KEYS = ("profile", "experiment")
"""Dev-loop CLI keys: top-level [tool.apx] (and .apx.local [tool.apx]). Sub-project 3b-1."""
REGISTRATION_KEYS = ("registered_model", "catalog", "schema", "supervisor_id", "registry_table", "tools_table")
"""Registration/publishing CLI keys: [tool.apx.deploy]. Sub-project 3b-1."""
```

In `cli.py`, replace the body of `_read_apx_agent_config` (keep the signature, and update the docstring's first paragraph to name the new homes):

```python
def _read_apx_agent_config(pyproject_path: Path | None = None) -> dict[str, Any]:
    """CLI view of the project's apx settings as one flat dict.

    Merges, lowest to highest: ``[tool.apx.agent]``; the dev keys from top-level
    ``[tool.apx]`` and the registration keys from ``[tool.apx.deploy]``
    (sub-project 3b-1); then ``.apx.local`` (gitignored: ``[tool.apx]`` and its
    older ``[tool.apx.agent]`` table). A key declared in ``[tool.apx.agent]``
    and its new home with different values is an error.

    Returns an empty dict if the files are missing, malformed, or declare nothing.
    """
    import tomllib

    from ._inspection import DEV_KEYS, REGISTRATION_KEYS

    def _apx(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = tomllib.loads(path.read_text())
        except Exception:
            return {}
        tool = data.get("tool", {})
        apx = tool.get("apx", {}) if isinstance(tool, dict) else {}
        return apx if isinstance(apx, dict) else {}

    def _table(apx: dict[str, Any], key: str) -> dict[str, Any]:
        value = apx.get(key, {})
        return value if isinstance(value, dict) else {}

    cwd = pyproject_path.parent if pyproject_path else Path.cwd()
    committed = _apx(pyproject_path or cwd / "pyproject.toml")
    legacy = _table(committed, "agent")
    homes = {k: ("[tool.apx]", committed[k]) for k in DEV_KEYS if k in committed}
    deploy = _table(committed, "deploy")
    homes.update({k: ("[tool.apx.deploy]", deploy[k]) for k in REGISTRATION_KEYS if k in deploy})
    for key, (home, value) in homes.items():
        if key in legacy and legacy[key] != value:
            raise click.ClickException(
                f"{key} is declared in both [tool.apx.agent] and {home} with different values; keep {home}.")
    local = _apx(cwd / ".apx.local")
    local_keys = {**_table(local, "agent"), **{k: local[k] for k in DEV_KEYS if k in local}}
    return {**legacy, **{k: v for k, (_, v) in homes.items()}, **local_keys}
```

Remove the old `try: import tomllib / except ImportError: import tomli` block. The package floor is 3.11-compatible here, because `tomllib` is used elsewhere unconditionally; check with `grep -n "import tomllib" python/src/apx_agent/_inspection.py`.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_cli_metadata.py tests/test_cli.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_inspection.py python/src/apx_agent/cli.py python/tests/test_cli_metadata.py
git commit --quiet --message "feat: CLI metadata reads [tool.apx] and [tool.apx.deploy]

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: Section-scoped writers

**Files:**
- Modify: `python/src/apx_agent/cli.py:570-614` (`_save_to_pyproject`) and `cli.py:~11931-11938` (supervisor-create message)
- Test: `python/tests/test_cli_metadata.py`

**Interfaces:**
- Consumes: `DEV_KEYS`, `REGISTRATION_KEYS` (Task 1).
- Produces:
  - `_set_toml_key(path: Path, section: str, key: str, value: str) -> None` edits only within `[section]`, creating the section and the file if they're missing.
  - `_save_to_pyproject(key, value, *, local=False) -> bool` keeps its signature.
  - `local=True` goes to `.apx.local` `[tool.apx]`.
  - `REGISTRATION_KEYS` go to `[tool.apx.deploy]`.
  - Any other key goes to `[tool.apx.agent]`, returning False if that header is missing, as today.

- [ ] **Step 1: Write the failing tests** (append to `python/tests/test_cli_metadata.py`)

```python
def test_profile_picker_writes_apx_local_tool_apx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _save_profile_to_pyproject

    _write(tmp_path, "")
    monkeypatch.chdir(tmp_path)
    assert _save_profile_to_pyproject('dev "quoted"') is True
    local = tomllib.loads((tmp_path / ".apx.local").read_text())
    assert local == {"tool": {"apx": {"profile": 'dev "quoted"'}}}


def test_supervisor_id_lands_in_tool_apx_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _save_to_pyproject

    _write(tmp_path, '[tool.apx.deploy]\napp_name = "mcp-x"\n')
    monkeypatch.chdir(tmp_path)
    assert _save_to_pyproject("supervisor_id", "sup-1") is True
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert data["tool"]["apx"]["deploy"] == {"app_name": "mcp-x", "supervisor_id": "sup-1"}


def test_writer_never_touches_a_same_named_key_in_another_table(tmp_path: Path) -> None:
    from apx_agent.cli import _set_toml_key

    path = _write(tmp_path, '[tool.apx]\nprofile = "old"\n\n[tool.apx.metadata]\nprofile = "keep"\n\n[tool.other]\nprofile = "keep"\n')
    _set_toml_key(path, "tool.apx", "profile", "new")
    data = tomllib.loads(path.read_text())
    assert data["tool"]["apx"]["profile"] == "new"
    assert data["tool"]["apx"]["metadata"] == {"profile": "keep"}
    assert data["tool"]["other"] == {"profile": "keep"}


def test_writer_adds_to_an_existing_section_with_other_keys(tmp_path: Path) -> None:
    from apx_agent.cli import _set_toml_key

    path = _write(tmp_path, '[tool.apx]\nexperiment = "/e"\n')
    _set_toml_key(path, "tool.apx", "profile", "dev")
    assert tomllib.loads(path.read_text())["tool"]["apx"] == {"experiment": "/e", "profile": "dev"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_cli_metadata.py -q -p no:cacheprovider -k "writer or picker or supervisor"`
Expected: FAIL with `ImportError: cannot import name '_set_toml_key'`.

- [ ] **Step 3: Implement.** Replace `_save_to_pyproject` in `cli.py` (keep `_save_profile_to_pyproject` as it is):

```python
def _set_toml_key(path: Path, section: str, key: str, value: str) -> None:
    """Set ``key = "<value>"`` inside ``[section]`` only, creating the section/file if missing."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    line = f'{key} = "{escaped}"\n'
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    header = f"[{section}]"
    start = next((i for i, text in enumerate(lines) if text.strip() == header), None)
    if start is None:
        prefix = "" if not lines or lines[-1].endswith("\n") else "\n"
        path.write_text("".join(lines) + prefix + ("\n" if lines else "") + header + "\n" + line)
        return
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if pattern.match(lines[i]):
            lines[i] = line
            break
    else:
        lines.insert(start + 1, line)
    path.write_text("".join(lines))


def _save_to_pyproject(key: str, value: str, *, local: bool = False) -> bool:
    """Write ``key = "<value>"`` into the key's home table (sub-project 3b-1).

    ``local=True`` writes ``.apx.local`` ``[tool.apx]`` (gitignored, personal
    settings like ``profile``). Registration keys go to ``[tool.apx.deploy]``.
    Anything else still goes to an existing ``[tool.apx.agent]``. Returns True on success.
    """
    from ._inspection import REGISTRATION_KEYS

    if local:
        _set_toml_key(Path.cwd() / ".apx.local", "tool.apx", key, value)
        return True
    target = Path.cwd() / "pyproject.toml"
    if not target.exists():
        return False
    if key in REGISTRATION_KEYS:
        _set_toml_key(target, "tool.apx.deploy", key, value)
        return True
    if "[tool.apx.agent]" not in target.read_text():
        return False
    _set_toml_key(target, "tool.apx.agent", key, value)
    return True
```

In the supervisor-create flow (`cli.py:~11936-11938`), change the fallback hint from `[tool.apx.agent]` to `[tool.apx.deploy]`:

```python
            click.echo("\nAdd this to your shared pyproject.toml so every agent can advertise with zero args:")
            click.echo("\n  [tool.apx.deploy]")
            click.echo(f'  supervisor_id = "{supervisor_id}"')
```

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_cli_metadata.py tests/test_cli.py -q -p no:cacheprovider`
Expected: PASS. `tests/test_cli.py:6138` and `:6149` patch `_save_to_pyproject`, so their behavior is unchanged.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_cli_metadata.py
git commit --quiet --message "fix: CLI writers edit only their own TOML section

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: `migrate-config` moves the metadata keys

**Files:**
- Modify: `python/src/apx_agent/_migrate_config.py` (`cut_sections`, `plan_migration`; add `migrate_local`)
- Modify: `python/src/apx_agent/cli.py` (`migrate_config` command, ~line 4985)
- Test: `python/tests/test_migrate_config.py`

**Interfaces:**
- Consumes: `DEV_KEYS`, `REGISTRATION_KEYS` (Task 1); `_set_toml_key` (Task 2); `_read_apx_agent_config` (Task 1).
- Produces:
  - `cut_sections(text, prefix, *, children: bool = True) -> str`. With `children=False` it removes only the exact `[prefix]` section.
  - `plan_migration` routes `REGISTRATION_KEYS` into `[tool.apx.deploy]` and `DEV_KEYS` into `[tool.apx]`. These keys are never listed in `Migration.manual`.
  - `migrate_local(text: str) -> str | None` returns the new `.apx.local` text, or `None` if there's nothing to move.

- [ ] **Step 1: Write the failing tests** (append to `python/tests/test_migrate_config.py`)

```python
def test_metadata_keys_move_to_their_homes(tmp_path: Path) -> None:
    text = _PYPROJECT.replace('[tool.apx.agent]\n',
                              '[tool.apx.agent]\nregistered_model = "c.s.m"\nprofile = "dev"\nexperiment = "/e"\n')
    text += '\n[tool.apx]\nkeep = 1\n\n[tool.apx.metadata]\napp-name = "x"\n'
    agent = Agent(name="inspector", tools=[], model="system.ai.claude-sonnet-4-6",
                  instructions="Inspect tables.", examples=["Show tables", "Count rows"])
    migration = plan_migration(_write(tmp_path, text), agent)
    data = tomllib.loads(migration.text)
    assert migration.manual == [] and migration.removed_agent_table is True
    assert data["tool"]["apx"]["deploy"]["registered_model"] == "c.s.m"
    assert data["tool"]["apx"]["profile"] == "dev" and data["tool"]["apx"]["experiment"] == "/e"
    assert data["tool"]["apx"]["keep"] == 1
    assert data["tool"]["apx"]["metadata"] == {"app-name": "x"}


def test_data_triage_registered_model_is_not_manual(tmp_path: Path) -> None:
    real = Path(__file__).resolve().parents[1] / "examples" / "data-triage-agent" / "pyproject.toml"
    migration = plan_migration(_write(tmp_path, real.read_text()), Agent(name="data_triage_agent", tools=[]))
    data = tomllib.loads(migration.text)
    assert "registered_model" not in migration.manual
    assert data["tool"]["apx"]["deploy"]["registered_model"] == "serverless_stable_7def3z_catalog.apx.mcp_data_triage_agent"
    assert data["tool"]["apx"]["deploy"]["app_name"] == "mcp-data-triage-agent"


def test_migrate_local_moves_the_legacy_profile() -> None:
    from apx_agent._migrate_config import migrate_local

    out = migrate_local('[tool.apx.agent]\nprofile = "dev"\n')
    assert out is not None and tomllib.loads(out) == {"tool": {"apx": {"profile": "dev"}}}
    assert migrate_local('[tool.apx]\nprofile = "dev"\n') is None


def test_cli_migrates_apx_local_with_a_backup_even_when_pyproject_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, _AGENT_PY_BARE)
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nspace = "s"\n')
    _commit_all(root)
    (root / ".apx.local").write_text('[tool.apx.agent]\nprofile = "dev"\n')
    result = _run(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert tomllib.loads((root / ".apx.local").read_text()) == {"tool": {"apx": {"profile": "dev"}}}
    assert (root / ".apx.local.bak").read_text() == '[tool.apx.agent]\nprofile = "dev"\n'
    assert "migrated .apx.local" in result.output
```

`_repo` commits everything, `.apx.local` included unless it's gitignored. Write `.apx.local` *after* `_commit_all`, as shown, so the dirty-file check (which looks only at `pyproject.toml`) stays clean.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_migrate_config.py -q -p no:cacheprovider -k "metadata or data_triage or local"`
Expected: FAIL. `manual` lists `registered_model`/`profile`/`experiment`, and `migrate_local` doesn't exist.

- [ ] **Step 3: Implement.**

In `_migrate_config.py`, make `cut_sections` take `children`:

```python
def cut_sections(text: str, prefix: str, *, children: bool = True) -> str:
    """Remove ``[prefix]`` (and, with *children*, every ``[prefix.*]``) section."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        match = _HEADER.match(line)
        if match:
            name = match.group(1)
            skipping = name == prefix or (children and name.startswith(prefix + "."))
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))
```

In `plan_migration`, directly after `deploy.update(raw.get("deploy", {}))`, route the metadata keys:

```python
    from ._inspection import DEV_KEYS, REGISTRATION_KEYS

    deploy.update({k: raw[k] for k in REGISTRATION_KEYS if k in raw})
    top = {k: v for k, v in apx.items() if not isinstance(v, dict)}
    top.update({k: raw[k] for k in DEV_KEYS if k in raw})
```

Change `remaining` so the moved keys aren't kept or listed as manual:

```python
    moved = ("name", "host", "deploy", "module", *DEV_KEYS, *REGISTRATION_KEYS)
    remaining = {k: v for k, v in raw.items() if k not in moved}
```

Before appending the deploy block, rewrite the exact `[tool.apx]` section:

```python
    from ._project_gen import _write_toml_section

    new = cut_sections(new, "tool.apx", children=False)
    if top:
        top_lines: list[str] = []
        _write_toml_section(top_lines, "tool.apx", top)
        new = new.rstrip("\n") + "\n\n" + "\n".join(top_lines)
```

Move the `from ._project_gen import _write_toml_section` import to the top of `plan_migration`, so it's imported once. Add `migrate_local` at module level:

```python
def migrate_local(text: str) -> str | None:
    """``.apx.local``: move dev keys from its old ``[tool.apx.agent]`` to ``[tool.apx]``; None if nothing to do."""
    from ._inspection import DEV_KEYS
    from ._project_gen import _write_toml_section

    data = tomllib.loads(text)
    apx = data.get("tool", {}).get("apx", {})
    legacy = apx.get("agent", {})
    move = {k: legacy[k] for k in DEV_KEYS if k in legacy}
    if not move:
        return None
    rest = {k: v for k, v in legacy.items() if k not in move}
    top = {**{k: v for k, v in apx.items() if not isinstance(v, dict)}, **move}
    out = cut_sections(cut_sections(text, "tool.apx.agent"), "tool.apx", children=False).rstrip("\n")
    lines: list[str] = []
    _write_toml_section(lines, "tool.apx", top)
    if rest:
        _write_toml_section(lines, "tool.apx.agent", rest)
    return ((out + "\n\n") if out else "") + "\n".join(lines).rstrip("\n") + "\n"
```

In `cli.py`'s `migrate_config`:

1. Replace the early "nothing to migrate" check, so `.apx.local` is handled even when `pyproject.toml` is already migrated:

```python
    local_path = cwd / ".apx.local"
    local_text = local_path.read_text() if local_path.exists() else None
    local_new = _migrate_config.migrate_local(local_text) if local_text is not None else None
    if local_new is not None:
        import shutil

        shutil.copyfile(local_path, cwd / ".apx.local.bak")
        local_path.write_text(local_new)
        click.echo("migrated .apx.local to [tool.apx] (backup: .apx.local.bak)")
    if "agent" not in tomllib.loads(pyproject.read_text()).get("tool", {}).get("apx", {}):
        if local_new is None:
            click.echo("nothing to migrate: no [tool.apx.agent] in pyproject.toml")
        return
```

2. Capture the moved metadata before the edit, next to `before = _load_agent_config(...)`. Only the 3b-1 keys are compared. Runtime fields and `module` leave the dict on purpose when the table goes, and Task 3 of 3a already checks those.

```python
    from ._inspection import DEV_KEYS, REGISTRATION_KEYS

    def _meta() -> dict[str, Any]:
        flat = _read_apx_agent_config(pyproject)
        return {k: flat[k] for k in (*DEV_KEYS, *REGISTRATION_KEYS) if k in flat}

    before_meta = _meta()
```

3. In the read-back `try`, add the metadata comparison to `same`:

```python
        same = (before is not None and after is not None
                and resolve_settings(agent, after).model_dump(exclude=skip)
                == resolve_settings(agent, before).model_dump(exclude=skip)
                and _meta() == before_meta)
```

Also catch `click.ClickException` there alongside `ValueError` (the reader raises it on a conflict): `except (ValueError, click.ClickException):`.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_migrate_config.py tests/test_cli_metadata.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Gate**

Run: `make check`, then `pre-commit run --files $(git diff --name-only origin/main...HEAD)`, then the PR CI subset under xdist (Global Constraints).
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add python/src/apx_agent/_migrate_config.py python/src/apx_agent/cli.py python/tests/test_migrate_config.py
git commit --quiet --message "feat: migrate-config moves CLI metadata keys and .apx.local

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 4: Docs and changelog

**Files:**
- Modify: `docs/reference/pyproject-toml.md` (the `[tool.apx.deploy]` section added in #990)
- Modify: `CHANGELOG.md` (`### Added` under `## [Unreleased]`)

- [ ] **Step 1: Reference.** After the `[tool.apx.deploy]` TOML block, add:

````markdown
`[tool.apx.deploy]` also holds the registration and publishing keys:
`registered_model`, `catalog`, `schema`, `supervisor_id`, `registry_table` and
`tools_table`. Dev-loop keys live in top-level `[tool.apx]`:

```toml
[tool.apx]
experiment = "/Users/you@example.com/orders"   # MLflow experiment for run/eval/trace
# profile lives in the gitignored .apx.local, under [tool.apx]
```
````

- [ ] **Step 2: Changelog** under `### Added`:

```markdown
- **CLI metadata homes.** `profile` and `experiment` live in `[tool.apx]` (`profile` in `.apx.local`), and registration keys (`registered_model`, `catalog`, `schema`, `supervisor_id`, `registry_table`, `tools_table`) in `[tool.apx.deploy]`. `migrate-config` moves them, and CLI writers now edit only their own TOML section.
```

- [ ] **Step 3: Verify and commit**

Run: `pre-commit run --files docs/reference/pyproject-toml.md CHANGELOG.md` and `cd python && uv run pytest tests/test_docs* -q -p no:cacheprovider`.

```bash
git add docs/reference/pyproject-toml.md CHANGELOG.md
git commit --quiet --message "docs: CLI metadata homes

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Out of this plan

- Agent-setting readers (3b-2) and generators (3b-3).
- Migrating the in-repo projects (3c), and rejecting `[tool.apx.agent]` (3d).
