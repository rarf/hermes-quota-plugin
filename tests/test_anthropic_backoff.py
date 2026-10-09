"""Anthropic usage 429 backoff: a rate-limited account must stop being polled.

Mocks only; no network. Covers: a 429 opens a per-account cooldown, the next
read inside the cooldown makes no HTTP call, the cooldown is per token, an
expired cooldown allows a fresh read, the state never holds the raw token,
non-429 errors never open a cooldown, and corrupt or hostile state fails open.
"""
import json
import logging
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quota_providers import builtin  # noqa: E402

_OK_BODY = b'{"limits": []}'


def _http_error(code):
    return urllib.error.HTTPError(builtin._ANTHROPIC_USAGE_URL, code, "x", None, None)


def _ok_response():
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = _OK_BODY
    return resp


class AnthropicBackoffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "quota_anthropic_backoff.json"
        self.patch_path = mock.patch.object(
            builtin, "_backoff_state_path", return_value=str(self.state)
        )
        self.patch_path.start()

    def tearDown(self):
        self.patch_path.stop()
        self._tmp.cleanup()

    def test_429_opens_cooldown_and_next_read_skips_network(self):
        opener = mock.Mock(side_effect=_http_error(429))
        with mock.patch.object(builtin, "urlopen_no_redirect", opener):
            payload, reason = builtin._request_anthropic_usage("tok-a")
            self.assertIsNone(payload)
            self.assertEqual(reason, "http-429")
            self.assertEqual(opener.call_count, 1)
            payload2, reason2 = builtin._request_anthropic_usage("tok-a")
        self.assertIsNone(payload2)
        self.assertEqual(reason2, "rate-limited")
        self.assertEqual(opener.call_count, 1, "no HTTP call while cooling down")

    def test_cooldown_is_per_token(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", mock.Mock(side_effect=_http_error(429))):
            builtin._request_anthropic_usage("tok-a")
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()) as second:
            _payload, reason = builtin._request_anthropic_usage("tok-b")
        self.assertIsNone(reason)
        self.assertEqual(second.call_count, 1)

    def test_expired_cooldown_allows_a_fresh_read(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", mock.Mock(side_effect=_http_error(429))):
            builtin._request_anthropic_usage("tok-a")
        data = json.loads(self.state.read_text())
        self.assertTrue(data, "429 must have written a cooldown entry")
        self.assertTrue(builtin._anthropic_cooling_down("tok-a"), "cooldown active before expiry")
        expired = {k: time.time() - 1 for k in data}
        self.state.write_text(json.dumps(expired))
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()) as again:
            _payload, reason = builtin._request_anthropic_usage("tok-a")
        self.assertIsNone(reason)
        self.assertEqual(again.call_count, 1)

    def test_state_file_holds_digest_not_raw_token(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", mock.Mock(side_effect=_http_error(429))):
            builtin._request_anthropic_usage("SECRET-TOKEN-123")
        text = self.state.read_text()
        self.assertNotIn("SECRET-TOKEN-123", text)
        self.assertIn(builtin._backoff_key("SECRET-TOKEN-123"), text)

    def test_no_secret_in_logs_or_reasons_on_429_and_cooldown(self):
        # assertNoLogs needs Python 3.10+; this host runs 3.14.
        with self.assertNoLogs(level=logging.DEBUG):
            with mock.patch.object(builtin, "urlopen_no_redirect", mock.Mock(side_effect=_http_error(429))):
                _p, first = builtin._request_anthropic_usage("SECRET-TOKEN-123")
                _p, second = builtin._request_anthropic_usage("SECRET-TOKEN-123")
        self.assertNotIn("SECRET-TOKEN-123", first)
        self.assertNotIn("SECRET-TOKEN-123", second)
        self.assertEqual(second, "rate-limited")

    def test_auth_and_server_errors_do_not_open_cooldown(self):
        for code, reason in ((401, "auth-failed"), (403, "auth-failed"), (500, "http-500")):
            with self.subTest(code=code):
                if self.state.exists():
                    self.state.unlink()
                with mock.patch.object(builtin, "urlopen_no_redirect", mock.Mock(side_effect=_http_error(code))) as op:
                    _payload, got = builtin._request_anthropic_usage("tok-a")
                    self.assertEqual(got, reason)
                    builtin._request_anthropic_usage("tok-a")
                self.assertEqual(op.call_count, 2, "non-429 must not be cooled down")
                self.assertFalse(self.state.exists(), "non-429 must not write state")

    def test_infinite_and_far_future_state_fails_open(self):
        key = builtin._backoff_key("tok-a")
        hostile_values = (
            "Infinity",
            "1e999",
            "NaN",
            repr(time.time() + 10 ** 9),
            "true",
        )
        for raw in hostile_values:
            with self.subTest(value=raw):
                self.state.write_text('{"%s": %s}' % (key, raw))
                self.assertFalse(
                    builtin._anthropic_cooling_down("tok-a"),
                    f"hostile value {raw} must not lock the account",
                )
                # Discriminating check: rejected values are dropped from the
                # loaded state, so this fails if validation ever accepts them
                # (NaN and true pass the cooling_down check on their own).
                self.assertEqual(builtin._load_backoff(), {}, f"{raw} must be rejected")
                with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()) as op:
                    _payload, reason = builtin._request_anthropic_usage("tok-a")
                self.assertIsNone(reason)
                self.assertEqual(op.call_count, 1)

    def test_corrupt_or_non_dict_state_fails_open(self):
        for body in ("not json{", "[1, 2, 3]", '"string"'):
            with self.subTest(body=body):
                self.state.write_text(body)
                with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()) as op:
                    _payload, reason = builtin._request_anthropic_usage("tok-a")
                self.assertIsNone(reason)
                self.assertEqual(op.call_count, 1)

    def test_unwritable_state_still_returns_http_429(self):
        with mock.patch.object(builtin, "_backoff_state_path", return_value="/nonexistent-dir/x/state.json"):
            with mock.patch.object(builtin, "urlopen_no_redirect", mock.Mock(side_effect=_http_error(429))):
                payload, reason = builtin._request_anthropic_usage("tok-a")
        self.assertIsNone(payload)
        self.assertEqual(reason, "http-429")


if __name__ == "__main__":
    unittest.main()
