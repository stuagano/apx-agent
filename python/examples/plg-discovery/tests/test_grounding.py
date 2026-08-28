from server.grounding import build_system_prompt, load_catalog
from server import tools


def teardown_function():
    tools.reset_runtime()  # skills are process-global; don't leak between tests


def test_catalog_loads_and_has_teaser_components():
    cat = load_catalog()
    names = {c["name"] for c in cat}
    assert "Donor Management" in names
    assert "Finance & Impact Reporting" in names


def test_system_prompt_includes_playbook_brief_catalog_and_contract():
    p = build_system_prompt()
    assert "STAGE 1" in p  # playbook stages present
    assert "Urban Gleaners" in p  # brief content injected
    assert "Donor Management" in p  # catalog injected
    assert "```json" in p  # artifact contract explained
    assert "Keep&Integrate" in p  # decision vocabulary present


def test_no_skills_section_when_none_registered():
    tools.reset_runtime()
    p = build_system_prompt()
    assert "SKILLS AVAILABLE TO YOU" not in p


def test_registered_skill_is_named_and_directed_in_prompt():
    tools.reset_runtime()
    tools.set_skill(
        "nonprofit_discovery",
        "Use whenever the user names a nonprofit, .org domain, or EIN.",
        "## Methodology\n1. Pull the 990.",
    )
    p = build_system_prompt()
    assert "SKILLS AVAILABLE TO YOU" in p  # section present
    assert "nonprofit_discovery" in p  # skill named
    assert "Use whenever the user names a nonprofit" in p  # its 'use when' shown
    assert "MUST call" in p  # directed, not just listed
