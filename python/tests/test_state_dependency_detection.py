from apx_agent import Dependencies
from apx_agent._inspection import _inspect_tool_fn, _is_state_dependency, _state_param_name


def test_is_state_dependency_true_for_state_alias():
    assert _is_state_dependency(Dependencies.State) is True


def test_is_state_dependency_false_for_plain_and_depends():
    assert _is_state_dependency(str) is False
    assert _is_state_dependency(Dependencies.UserClient) is False


def test_state_param_excluded_from_schema_and_not_a_dep():
    def tool(name: str, state: Dependencies.State) -> str:
        return name

    sig = _inspect_tool_fn(tool)
    assert _state_param_name(tool) == "state"
    assert "state" not in sig.plain_params       # excluded from LLM schema
    assert "state" not in sig.dep_param_names     # not a FastAPI dep
    assert "name" in sig.plain_params


def test_state_param_name_none_when_absent():
    def tool(name: str) -> str:
        return name

    assert _state_param_name(tool) is None
