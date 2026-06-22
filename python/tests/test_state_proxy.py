from apx_agent._state_proxy import StateProxy


def test_reads_pass_through_and_clean_by_default():
    p = StateProxy({"a": 1})
    assert p["a"] == 1
    assert p.get("missing") is None
    assert "a" in p
    assert p.dirty is False
    assert p.delta == {}


def test_setitem_tracked_into_delta():
    p = StateProxy({"a": 1})
    p["b"] = 2
    assert p["b"] == 2          # readable after write
    assert p.dirty is True
    assert p.delta == {"b": 2}


def test_last_write_wins_within_delta():
    p = StateProxy({})
    p["k"] = 1
    p["k"] = 2
    assert p.delta == {"k": 2}


def test_update_and_setdefault_and_pop_tracked():
    p = StateProxy({"x": 0})
    p.update({"y": 9})
    assert p.setdefault("z", 7) == 7
    assert p.setdefault("x", 100) == 0      # existing key unchanged, not tracked
    assert p.delta == {"y": 9, "z": 7}


def test_missing_key_raises_keyerror():
    p = StateProxy({})
    import pytest
    with pytest.raises(KeyError):
        _ = p["nope"]


def test_none_source_behaves_as_empty():
    p = StateProxy(None)
    assert p.get("a") is None
    p["a"] = 1
    assert p.delta == {"a": 1}
