import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BRIEF = _ROOT / "nonprofit-saas-landscape-2025-2026.md"
_PLAYBOOK = _ROOT / "prompts" / "discovery_playbook.md"
_CATALOG = _ROOT / "data" / "component_catalog.json"

# In-memory override for the playbook, set at runtime via the dev panel so the
# discovery instructions can be tuned on the fly. When None, the on-disk
# playbook file is used.
_playbook_override: str | None = None


def set_playbook_override(text: str | None) -> None:
    """Set (or clear, with None) a runtime override for the playbook text."""
    global _playbook_override
    _playbook_override = text


def playbook_is_override() -> bool:
    return _playbook_override is not None


def get_playbook() -> str:
    """The active playbook: the runtime override if set, else the on-disk file."""
    if _playbook_override is not None:
        return _playbook_override
    return _PLAYBOOK.read_text()


def load_catalog() -> list[dict]:
    return json.loads(_CATALOG.read_text())


def _skills_section() -> str:
    """Enumerate the runtime-registered skills in the system prompt so the agent
    KNOWS they exist and is told to use them. Each skill is also passed to the agent
    as a callable tool of the same name (see server.tools.active_tools), but a tool's
    description alone is only a hint — without naming the skills in the instructions
    and directing their use, the model tends to improvise (e.g. ad-hoc web research)
    instead of calling them. Returns "" when no skills are registered.

    Imported locally to avoid any import cycle with server.tools.
    """
    from server.tools import list_skills

    skills = list_skills()
    if not skills:
        return ""
    rows = "\n".join(f"- **{s['name']}** — {s['description']}" for s in skills)
    return (
        "# SKILLS AVAILABLE TO YOU (call these as tools)\n"
        "Each skill below is exposed to you as a callable tool of the SAME name that "
        "returns detailed step-by-step guidance. When a skill's description matches the "
        "current situation, you MUST call that tool FIRST and follow its guidance before "
        "responding — do not improvise an equivalent from memory or from ad-hoc research.\n"
        f"{rows}\n\n"
    )


def build_system_prompt() -> str:
    playbook = get_playbook()
    brief = _BRIEF.read_text()
    catalog = load_catalog()
    catalog_md = "\n".join(
        f"- **{c['name']}** (engine {c['engine']}): {c['description']} "
        f"[config: {', '.join(c['config_outline'])}]"
        for c in catalog
    )
    return (
        f"{playbook}\n\n"
        f"{_skills_section()}"
        f"# COMPONENT CATALOG (the 'Run in Databricks' options you may name)\n{catalog_md}\n\n"
        f"# RESEARCH BRIEF (your grounding knowledge)\n{brief}\n"
    )
