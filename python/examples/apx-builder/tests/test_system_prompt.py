from system_prompt import SYSTEM_PROMPT


def test_system_prompt_is_string():
    assert isinstance(SYSTEM_PROMPT, str)
    assert len(SYSTEM_PROMPT) > 0


def test_system_prompt_tool_order():
    """scaffold_project must appear before deploy_agent, which must appear before poll_deployment."""
    scaffold_idx = SYSTEM_PROMPT.index("scaffold_project")
    deploy_idx = SYSTEM_PROMPT.index("deploy_agent")
    poll_idx = SYSTEM_PROMPT.index("poll_deployment")
    assert scaffold_idx < deploy_idx, "scaffold_project must appear before deploy_agent"
    assert deploy_idx < poll_idx, "deploy_agent must appear before poll_deployment"


def test_system_prompt_url_rule():
    """The prompt must contain 'poll_deployment' and a prohibition on sharing the URL early."""
    assert "poll_deployment" in SYSTEM_PROMPT
    # Case-insensitive check for the exact prohibition phrase
    assert "never share" in SYSTEM_PROMPT.lower(), (
        "Expected 'NEVER share' (or 'never share') to appear in the prompt"
    )


def test_system_prompt_ws_not_in_tool_calls():
    """The Phase 2 build section must NOT instruct the LLM to pass ws as an explicit argument.

    After the fix, ws should not appear inside the tool call signatures shown to the LLM.
    The only permitted mention is the explanatory note that ws is injected automatically.
    """
    # Locate the Phase 2 section
    build_start = SYSTEM_PROMPT.index("## Phase 2: Build")
    build_end = SYSTEM_PROMPT.index("## Phase 3: Finish")
    build_section = SYSTEM_PROMPT[build_start:build_end]

    # The explanatory note is allowed to mention ws.
    # What must NOT appear is ws inside a tool-call signature, i.e. after "scaffold_project("
    # or "deploy_agent(" or "poll_deployment(".
    for call_snippet in [
        "scaffold_project(",
        "deploy_agent(",
        "poll_deployment(",
    ]:
        call_start = build_section.find(call_snippet)
        if call_start == -1:
            continue  # tool not shown in this section — nothing to check
        call_start += len(call_snippet)
        close_paren = build_section.index(")", call_start)
        args_text = build_section[call_start:close_paren]
        assert "ws" not in args_text.split(","), (
            f"'ws' must not appear as an explicit argument in {call_snippet}...) — "
            f"it is injected automatically. Found args: {args_text!r}"
        )


def test_system_prompt_tables_rendering():
    """Phase 3 must instruct rendering {tables} in plain English, not as a Python list."""
    # Locate the Phase 3 section
    phase3_start = SYSTEM_PROMPT.index("## Phase 3: Finish")
    phase3_section = SYSTEM_PROMPT[phase3_start:]
    lower = phase3_section.lower()
    assert "plain english" in lower or "natural" in lower, (
        "Phase 3 should instruct the LLM to render {tables} in plain English or naturally"
    )


def test_system_prompt_no_jargon_rule():
    lower = SYSTEM_PROMPT.lower()
    assert "jargon" in lower or "plain english" in lower or "plain language" in lower
