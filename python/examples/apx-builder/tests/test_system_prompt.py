"""Tests for the updated system_prompt.py."""
from system_prompt import get_system_prompt


def test_get_system_prompt_returns_string():
    prompt = get_system_prompt("user@example.com")
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_user_email_is_embedded():
    prompt = get_system_prompt("alice@databricks.com")
    assert "alice@databricks.com" in prompt


def test_build_phase_uses_write_tool():
    prompt = get_system_prompt("user@example.com")
    assert "Write" in prompt, "Build phase must reference the Write tool for file creation"


def test_build_phase_uses_upload_folder():
    prompt = get_system_prompt("user@example.com")
    assert "manage_workspace_files" in prompt


def test_build_phase_uses_create_and_deploy_app():
    prompt = get_system_prompt("user@example.com")
    assert "create_and_deploy_app" in prompt or "mcp__apx__create_and_deploy_app" in prompt


def test_discovery_has_five_questions():
    prompt = get_system_prompt("user@example.com")
    # All five discovery questions must be present
    assert "What should your agent do?" in prompt
    assert "Which tables or data sources should it use?" in prompt
    assert "Should it connect to any Genie spaces?" in prompt
    assert "Should it be able to answer questions about data lineage?" in prompt
    assert "What should we call this agent?" in prompt


def test_discovery_references_genie_step():
    prompt = get_system_prompt("user@example.com")
    assert "Genie" in prompt or "genie" in prompt


def test_no_backtick_rule_present():
    prompt = get_system_prompt("user@example.com")
    lower = prompt.lower()
    assert "backtick" in lower or "code formatting" in lower


def test_phase_3_plain_english_tables():
    prompt = get_system_prompt("user@example.com")
    phase3_start = prompt.index("## Phase 3")
    phase3_section = prompt[phase3_start:].lower()
    assert "plain english" in phase3_section or "natural" in phase3_section


def test_no_jargon_rule():
    lower = get_system_prompt("user@example.com").lower()
    assert "jargon" in lower or "plain english" in lower


def test_old_tool_names_not_present():
    """scaffold_project, deploy_agent, poll_deployment must not appear in the prompt."""
    prompt = get_system_prompt("user@example.com")
    assert "scaffold_project" not in prompt
    assert "deploy_agent" not in prompt
    assert "poll_deployment" not in prompt
