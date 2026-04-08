"""Tests for _eval.py — MLflow evaluation bridge."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apx_agent._eval import app_predict_fn


class TestAppPredictFn:
    def test_returns_callable(self):
        predict = app_predict_fn("http://my-agent.com")
        assert callable(predict)

    def _mock_post(self, return_json: dict, status_code: int = 200):
        """Return a mock for httpx.post."""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = return_json
        return mock_response

    def test_predict_with_string_input(self):
        predict = app_predict_fn("http://my-agent.com", token="fake-token")
        resp = self._mock_post({"output": [{"content": [{"text": "Hello!"}]}]})

        with patch("httpx.post", return_value=resp) as mock_post:
            result = predict("Hello")
            assert result == "Hello!"
            call_json = mock_post.call_args.kwargs["json"]
            assert call_json["input"][0]["role"] == "user"
            assert call_json["input"][0]["content"] == "Hello"

    def test_predict_with_messages_dict(self):
        predict = app_predict_fn("http://my-agent.com")
        resp = self._mock_post({"output": [{"content": [{"text": "Response"}]}]})

        with patch("httpx.post", return_value=resp) as mock_post:
            result = predict({"messages": [{"role": "user", "content": "Hi"}]})
            assert result == "Response"
            call_json = mock_post.call_args.kwargs["json"]
            assert call_json["input"] == [{"role": "user", "content": "Hi"}]

    def test_predict_with_auth_header(self):
        predict = app_predict_fn("http://my-agent.com", token="my-token")
        resp = self._mock_post({"output": [{"content": [{"text": "ok"}]}]})

        with patch("httpx.post", return_value=resp) as mock_post:
            predict("test")
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer my-token"

    def test_predict_without_auth(self):
        predict = app_predict_fn("http://my-agent.com")
        resp = self._mock_post({"output": [{"content": [{"text": "ok"}]}]})

        with patch("httpx.post", return_value=resp) as mock_post:
            predict("test")
            headers = mock_post.call_args.kwargs["headers"]
            assert "Authorization" not in headers

    def test_predict_fallback_on_unexpected_format(self):
        predict = app_predict_fn("http://my-agent.com")
        resp = self._mock_post({"unexpected": "format"})

        with patch("httpx.post", return_value=resp):
            result = predict("test")
            assert "unexpected" in result

    def test_url_trailing_slash_stripped(self):
        predict = app_predict_fn("http://my-agent.com/")
        resp = self._mock_post({"output": [{"content": [{"text": "ok"}]}]})

        with patch("httpx.post", return_value=resp) as mock_post:
            predict("test")
            url = mock_post.call_args[0][0]
            assert url == "http://my-agent.com/invocations"
