from system_prompt import SYSTEM_PROMPT


def test_system_prompt_is_string():
    assert isinstance(SYSTEM_PROMPT, str)
    assert len(SYSTEM_PROMPT) > 0


def test_system_prompt_contains_discovery_instructions():
    lower = SYSTEM_PROMPT.lower()
    assert "one question at a time" in lower or "one at a time" in lower


def test_system_prompt_contains_build_phase():
    lower = SYSTEM_PROMPT.lower()
    assert "scaffold" in lower
    assert "deploy" in lower
    assert "poll" in lower


def test_system_prompt_url_rule():
    lower = SYSTEM_PROMPT.lower()
    # Must mention poll_deployment AND a prohibition on sharing the URL before it returns
    has_poll = "poll_deployment" in lower
    has_before_rule = (
        ("never share" in lower and "url" in lower)
        or ("url" in lower and "before" in lower)
        or ("not ready until" in lower)
    )
    assert has_poll
    assert has_before_rule


def test_system_prompt_no_jargon_rule():
    lower = SYSTEM_PROMPT.lower()
    assert "jargon" in lower or "plain english" in lower or "plain language" in lower
