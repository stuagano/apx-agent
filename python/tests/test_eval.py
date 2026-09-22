"""Tests for _eval.py — MLflow evaluation bridge."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from apx_agent._eval import (
    _extract_non_stream_text,
    _resolve_endpoint_token,
    app_predict_fn,
    endpoint_predict_fn,
)


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

    def test_predict_returns_last_message_text(self):
        """A multi-item ResponsesAgent envelope: final answer wins.

        ``output[0]`` is a function call with no text; the last message item
        holds the answer. The old ``output[0]["content"][0]["text"]`` read
        would KeyError into the raw-payload fallback.
        """
        predict = app_predict_fn("http://my-agent.com")
        resp = self._mock_post(
            {
                "output": [
                    {"type": "function_call", "name": "lookup", "arguments": "{}"},
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Final answer."}
                        ],
                    },
                ]
            }
        )

        with patch("httpx.post", return_value=resp):
            assert predict("question") == "Final answer."


class TestExtractNonStreamText:
    def test_untyped_item_is_treated_as_message(self):
        data = {"output": [{"content": [{"text": "plain"}]}]}
        assert _extract_non_stream_text(data) == "plain"

    def test_skips_typed_non_message_items(self):
        data = {
            "output": [
                {"type": "function_call_output", "call_id": "c", "output": "x"},
                {"type": "message", "content": [{"type": "output_text", "text": "ans"}]},
            ]
        }
        assert _extract_non_stream_text(data) == "ans"

    def test_fallback_on_unexpected_shape(self):
        data = {"unexpected": "shape"}
        assert "unexpected" in _extract_non_stream_text(data)
# endpoint_predict_fn / eval_against_endpoint — HTTP path
# ---------------------------------------------------------------------------


class _FakeUrlopenContext:
    """Context manager mimicking urllib.request.urlopen's iter/read surface."""

    def __init__(self, lines: list[bytes] | None = None, body: bytes | None = None):
        self._lines = lines or []
        self._body = body or b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._body


class TestResolveEndpointToken:
    def test_explicit_token_wins(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_TOKEN", "env-token")
        assert _resolve_endpoint_token("explicit") == "explicit"

    def test_env_var_fallback(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_TOKEN", "env-token")
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        assert _resolve_endpoint_token() == "env-token"

    def test_cli_profile_fallback(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
        result = MagicMock()
        result.stdout = json.dumps({"access_token": "cli-token"})
        result.returncode = 0
        with patch("apx_agent._eval._subprocess.run", return_value=result) as run:
            assert _resolve_endpoint_token() == "cli-token"
            cmd = run.call_args[0][0]
            assert cmd[:3] == ["databricks", "auth", "token"]
            assert "--profile" in cmd and "DEFAULT" in cmd

    def test_explicit_profile_arg(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        result = MagicMock()
        result.stdout = json.dumps({"access_token": "cli-token"})
        result.returncode = 0
        with patch("apx_agent._eval._subprocess.run", return_value=result) as run:
            assert _resolve_endpoint_token(profile="MY-PROF") == "cli-token"
            cmd = run.call_args[0][0]
            assert "MY-PROF" in cmd

    def test_error_when_no_source(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        with pytest.raises(RuntimeError, match="Could not resolve a Databricks token"):
            _resolve_endpoint_token()


class TestEndpointPredictFn:
    """Verify the predict function built around urllib + SSE/non-stream."""

    def test_stream_accumulates_text_chunks(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")

        lines = [
            b'data: {"text": "Hello"}\n',
            b'data: {"text": ", "}\n',
            b'data: {"text": "world"}\n',
            b'\n',
            b'data: [DONE]\n',
        ]
        fake = _FakeUrlopenContext(lines=lines)

        predict = endpoint_predict_fn("https://app.example.com", stream=True)
        with patch("urllib.request.urlopen", return_value=fake) as mock_open:
            result = predict({"question": "hi"})
            assert result == "Hello, world"
            # Confirm the request payload + URL.
            req = mock_open.call_args[0][0]
            assert req.full_url == "https://app.example.com/invocations"
            payload = json.loads(req.data.decode())
            assert payload == {
                "input": [{"role": "user", "content": "hi"}],
                "stream": True,
            }
            assert req.headers.get("Authorization") == "Bearer T"
            assert req.get_method() == "POST"

    def test_non_stream_returns_response_text(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")

        body = json.dumps(
            {"output": [{"content": [{"text": "Final answer."}]}]}
        ).encode()
        fake = _FakeUrlopenContext(body=body)

        predict = endpoint_predict_fn("https://app.example.com", stream=False)
        with patch("urllib.request.urlopen", return_value=fake) as mock_open:
            result = predict("what is 2+2?")
            assert result == "Final answer."
            req = mock_open.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert payload == {
                "input": [{"role": "user", "content": "what is 2+2?"}],
                "stream": False,
            }

    def test_extracts_question_from_messages(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")
        lines = [b'data: {"text": "ok"}\n']
        fake = _FakeUrlopenContext(lines=lines)
        predict = endpoint_predict_fn("https://app.example.com", stream=True)
        with patch("urllib.request.urlopen", return_value=fake) as mock_open:
            predict(
                {
                    "messages": [
                        {"role": "system", "content": "ignored"},
                        {"role": "user", "content": "real question"},
                    ]
                }
            )
            req = mock_open.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert payload["input"] == [{"role": "user", "content": "real question"}]

    def test_url_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")
        lines = [b'data: {"text": "ok"}\n']
        fake = _FakeUrlopenContext(lines=lines)
        predict = endpoint_predict_fn("https://app.example.com/", stream=True)
        with patch("urllib.request.urlopen", return_value=fake) as mock_open:
            predict("x")
            req = mock_open.call_args[0][0]
            assert req.full_url == "https://app.example.com/invocations"

    def test_stream_skips_malformed_lines(self, monkeypatch):
        """Non-data lines and malformed JSON shouldn't crash the predict fn."""
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")
        lines = [
            b'event: open\n',
            b'data: not-json\n',
            b'data: {"text": "good"}\n',
            b'random noise\n',
        ]
        fake = _FakeUrlopenContext(lines=lines)
        predict = endpoint_predict_fn("https://app.example.com", stream=True)
        with patch("urllib.request.urlopen", return_value=fake):
            assert predict("x") == "good"

    def test_token_resolved_at_factory_time(self, monkeypatch):
        """`endpoint_predict_fn` resolves the token once at construction."""
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        with pytest.raises(RuntimeError, match="Could not resolve"):
            endpoint_predict_fn("https://app.example.com")

    def test_non_stream_returns_last_message_text(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")
        body = json.dumps(
            {
                "output": [
                    {"type": "function_call", "name": "lookup", "arguments": "{}"},
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Final answer."}
                        ],
                    },
                ]
            }
        ).encode()
        fake = _FakeUrlopenContext(body=body)

        predict = endpoint_predict_fn("https://app.example.com", stream=False)
        with patch("urllib.request.urlopen", return_value=fake):
            assert predict("question") == "Final answer."

    def test_stream_accumulates_delta_when_no_completed_event(self, monkeypatch):
        """Real SSE deltas ride on ``delta``, not ``text``."""
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")
        lines = [
            b'data: {"delta": "Hel"}\n',
            b'data: {"delta": "lo"}\n',
        ]
        fake = _FakeUrlopenContext(lines=lines)
        predict = endpoint_predict_fn("https://app.example.com", stream=True)
        with patch("urllib.request.urlopen", return_value=fake):
            assert predict("x") == "Hello"

    def test_stream_prefers_completed_response(self, monkeypatch):
        """A terminal ``response.completed`` event wins over accumulated text."""
        monkeypatch.setenv("DATABRICKS_TOKEN", "T")
        completed = {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "message",
                        "content": [{"text": "Final answer."}],
                    }
                ]
            },
        }
        lines = [
            b'data: {"text": "noise"}\n',
            b'data: {"delta": "Hel"}\n',
            ("data: " + json.dumps(completed) + "\n").encode(),
        ]
        fake = _FakeUrlopenContext(lines=lines)
        predict = endpoint_predict_fn("https://app.example.com", stream=True)
        with patch("urllib.request.urlopen", return_value=fake):
            assert predict("x") == "Final answer."
