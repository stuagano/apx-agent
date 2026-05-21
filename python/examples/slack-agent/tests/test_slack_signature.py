import hashlib
import hmac
import time

from webhook import _verify_slack_signature

SECRET = "test-signing-secret"


def _make_sig(body: bytes, timestamp: str) -> str:
    basestring = f"v0:{timestamp}:{body.decode()}"
    return "v0=" + hmac.new(SECRET.encode(), basestring.encode(), hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    sig = _make_sig(body, ts)
    assert _verify_slack_signature(body, ts, sig, SECRET) is True


def test_invalid_signature_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    assert _verify_slack_signature(body, ts, "v0=badhex", SECRET) is False


def test_stale_timestamp_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()) - 400)  # 6+ minutes old
    sig = _make_sig(body, ts)
    assert _verify_slack_signature(body, ts, sig, SECRET) is False


def test_non_numeric_timestamp_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    assert _verify_slack_signature(body, "not-a-number", "v0=anything", SECRET) is False


def test_empty_signature_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    assert _verify_slack_signature(body, ts, "", SECRET) is False
