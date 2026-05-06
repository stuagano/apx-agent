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
    assert "upload_folder" in prompt or "mcp__databricks__upload_folder" in prompt


def test_build_phase_uses_create_and_deploy_app():
    prompt = get_system_prompt("user@example.com")
    assert "create_and_deploy_app" in prompt or "mcp__apx__create_and_deploy_app" in prompt


def test_discovery_references_execute_sql():
    prompt = get_system_prompt("user@example.com")
    assert "execute_sql" in prompt or "mcp__databricks__execute_sql" in prompt


def test_discovery_references_get_genie():
    prompt = get_system_prompt("user@example.com")
    assert "get_genie" in prompt or "mcp__databricks__get_genie" in prompt


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
