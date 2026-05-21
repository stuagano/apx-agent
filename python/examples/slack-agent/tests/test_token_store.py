import pytest
import token_store


@pytest.fixture(autouse=True)
def clear_store():
    token_store._store.clear()
    yield
    token_store._store.clear()


def test_get_missing_returns_none():
    assert token_store.get_token("U999") is None


def test_set_then_get_returns_token():
    token_store.set_token("U123", "dapi-abc")
    assert token_store.get_token("U123") == "dapi-abc"


def test_set_overwrites_existing():
    token_store.set_token("U123", "old-token")
    token_store.set_token("U123", "new-token")
    assert token_store.get_token("U123") == "new-token"


def test_clear_removes_token():
    token_store.set_token("U123", "dapi-abc")
    token_store.clear_token("U123")
    assert token_store.get_token("U123") is None


def test_clear_missing_is_noop():
    token_store.clear_token("U999")  # must not raise
