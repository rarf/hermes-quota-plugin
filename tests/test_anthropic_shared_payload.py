"""Anthropic usage payload shared across profiles.

Profiles normally use one Claude login, and the usage endpoint rate-limits per
token. Each profile polling on its own is what produced persistent HTTP 429s.
Mocks only; no network. Proves: a fresh payload is reused for the same token
without an HTTP call, it is never reused for another token, it expires, a 429
(or an active cooldown) serves the last good payload within the stale window
instead of a blank card, beyond that window the 429 surfaces, the share file
never holds the raw token, the state lives in the Hermes root rather than the
profile home, and corrupt or hostile share state fails open.
"""
import json
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

_PAYLOAD = {"limits": [{"kind": "session", "percent": 12}]}


def _ok_response(payload=_PAYLOAD):
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return resp


def _http_error(code):
    return urllib.error.HTTPError(builtin._ANTHROPIC_USAGE_URL, code, "x", None, None)


class AnthropicSharedPayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.share = self.dir / builtin._ANTHROPIC_SHARED_FILENAME
        self.patch = mock.patch.object(
            builtin, "_backoff_state_path",
            return_value=str(self.dir / builtin._ANTHROPIC_BACKOFF_FILENAME),
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._tmp.cleanup()

    def _age_share(self, seconds):
        data = json.loads(self.share.read_text())
        for entry in data.values():
            entry["at"] -= seconds
        self.share.write_text(json.dumps(data))

    def test_fresh_payload_is_reused_without_http(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()) as op:
            first = builtin._request_anthropic_usage("tok-a")
            second = builtin._request_anthropic_usage("tok-a")
        self.assertEqual(first, (_PAYLOAD, None))
        self.assertEqual(second, (_PAYLOAD, None))
        self.assertEqual(op.call_count, 1, "second profile must reuse the shared payload")

    def test_payload_is_never_shared_across_tokens(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()):
            builtin._request_anthropic_usage("tok-a")
        other = {"limits": [{"kind": "session", "percent": 77}]}
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response(other)) as op:
            payload, reason = builtin._request_anthropic_usage("tok-b")
        self.assertIsNone(reason)
        self.assertEqual(payload, other)
        self.assertEqual(op.call_count, 1)

    def test_expired_payload_triggers_a_fresh_read(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()):
            builtin._request_anthropic_usage("tok-a")
        self._age_share(builtin._ANTHROPIC_SHARED_TTL_S + 1)
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()) as op:
            builtin._request_anthropic_usage("tok-a")
        self.assertEqual(op.call_count, 1)

    def test_429_serves_last_good_payload_within_stale_window(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()):
            builtin._request_anthropic_usage("tok-a")
        self._age_share(builtin._ANTHROPIC_SHARED_TTL_S + 1)
        opener = mock.MagicMock(side_effect=_http_error(429))
        with mock.patch.object(builtin, "urlopen_no_redirect", opener):
            on_429 = builtin._request_anthropic_usage("tok-a")
            in_cooldown = builtin._request_anthropic_usage("tok-a")
        self.assertEqual(on_429, (_PAYLOAD, None))
        self.assertEqual(in_cooldown, (_PAYLOAD, None))
        self.assertEqual(opener.call_count, 1, "no HTTP call while cooling down")
        self.assertTrue(builtin._anthropic_cooling_down("tok-a"), "429 still opens the cooldown")

    def test_429_beyond_stale_window_surfaces(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()):
            builtin._request_anthropic_usage("tok-a")
        self._age_share(builtin._ANTHROPIC_SHARED_STALE_MAX_S + 1)
        with mock.patch.object(builtin, "urlopen_no_redirect", mock.MagicMock(side_effect=_http_error(429))):
            payload, reason = builtin._request_anthropic_usage("tok-a")
            payload2, reason2 = builtin._request_anthropic_usage("tok-a")
        self.assertIsNone(payload)
        self.assertEqual(reason, "http-429")
        self.assertIsNone(payload2)
        self.assertEqual(reason2, "rate-limited")

    def test_non_429_errors_do_not_serve_stale_payload(self):
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()):
            builtin._request_anthropic_usage("tok-a")
        self._age_share(builtin._ANTHROPIC_SHARED_TTL_S + 1)
        for code, expected in ((401, "auth-failed"), (500, "http-500")):
            with self.subTest(code=code):
                with mock.patch.object(builtin, "urlopen_no_redirect", mock.MagicMock(side_effect=_http_error(code))):
                    payload, reason = builtin._request_anthropic_usage("tok-a")
                self.assertIsNone(payload)
                self.assertEqual(reason, expected)

    def test_share_file_never_holds_the_raw_token(self):
        secret = "sk-ant-oat01-SYNTHETIC-SECRET"
        with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()):
            builtin._request_anthropic_usage(secret)
        self.assertNotIn(secret, self.share.read_text())

    def test_corrupt_or_hostile_share_state_fails_open(self):
        key = builtin._backoff_key("tok-a")
        bodies = (
            "not json{",
            "[1, 2, 3]",
            '"string"',
            json.dumps({key: "x"}),
            json.dumps({key: {"at": "now", "payload": _PAYLOAD}}),
            json.dumps({key: {"at": True, "payload": _PAYLOAD}}),
            json.dumps({key: {"at": time.time(), "payload": [1]}}),
            json.dumps({key: {"at": time.time() + 10 ** 9, "payload": _PAYLOAD}}),
            '{"%s": {"at": NaN, "payload": {}}}' % key,
            '{"%s": {"at": Infinity, "payload": {}}}' % key,
        )
        for body in bodies:
            with self.subTest(body=body):
                self.share.write_text(body)
                with mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()) as op:
                    payload, reason = builtin._request_anthropic_usage("tok-a")
                self.assertIsNone(reason)
                self.assertEqual(payload, _PAYLOAD)
                self.assertEqual(op.call_count, 1)

    def test_unwritable_share_still_returns_payload(self):
        with mock.patch.object(builtin, "_backoff_state_path", return_value="/nonexistent-dir/x/state.json"), \
             mock.patch.object(builtin, "urlopen_no_redirect", return_value=_ok_response()):
            self.assertEqual(builtin._request_anthropic_usage("tok-a"), (_PAYLOAD, None))


class SharedStateHomeTests(unittest.TestCase):
    def test_state_lives_in_hermes_root_not_profile_home(self):
        fake = mock.MagicMock()
        fake.get_default_hermes_root.return_value = Path("/h/root")
        fake.get_hermes_home.return_value = Path("/h/root/profiles/work")
        with mock.patch.dict(sys.modules, {"hermes_constants": fake}):
            self.assertEqual(builtin._shared_state_home(), "/h/root")
            self.assertEqual(
                builtin._backoff_state_path(),
                str(Path("/h/root") / builtin._ANTHROPIC_BACKOFF_FILENAME),
            )

    def test_older_core_falls_back_to_profile_home(self):
        fake = mock.MagicMock(spec=["get_hermes_home"])
        fake.get_hermes_home.return_value = Path("/h/root/profiles/work")
        with mock.patch.dict(sys.modules, {"hermes_constants": fake}):
            self.assertEqual(builtin._shared_state_home(), "/h/root/profiles/work")


if __name__ == "__main__":
    unittest.main()
