from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    if "quota_plugin_opencode_go" in sys.modules:
        return sys.modules["quota_plugin_opencode_go"]
    # Import directly from the repo root (same pattern as tests/test_fetchers.py).
    from quota_providers import opencode_go

    return opencode_go


class CredentialResolutionTests(unittest.TestCase):
    def test_env_key_wins_and_is_trimmed(self):
        mod = load_module()
        original = mod.os.environ.get("OPENCODE_API_KEY")
        try:
            mod.os.environ["OPENCODE_API_KEY"] = '  "oc_abc123"  '
            self.assertEqual(mod.resolve_api_key(), "oc_abc123")
        finally:
            if original is None:
                del mod.os.environ["OPENCODE_API_KEY"]
            else:
                mod.os.environ["OPENCODE_API_KEY"] = original

    def test_auth_json_api_record(self):
        mod = load_module()
        key = mod._extract_api_key({"type": "api", "key": "oc_xyz"})
        self.assertEqual(key, "oc_xyz")

    def test_auth_json_oauth_payload_record(self):
        mod = load_module()
        record = {
            "type": "oauth",
            "access": "token",
            "refresh": "refresh",
            "payload": {"zenApiKey": "oc_zen"},
        }
        self.assertEqual(mod._extract_api_key(record), "oc_zen")

    def test_no_credentials_anywhere(self):
        mod = load_module()
        with mock.patch.dict(mod.os.environ, {}, clear=False):
            # Every accepted name, not just the first: Hermes' own opencode-go
            # provider exports OPENCODE_GO_API_KEY, so clearing only
            # OPENCODE_API_KEY left a real key visible and this test failed on
            # any machine that runs the plugin (passing in CI, where the env is
            # empty).
            for name in mod._ENV_KEYS:
                mod.os.environ.pop(name, None)
            self.assertIsNone(mod._read_env_api_key())

    def test_opencode_go_env_key_is_accepted(self):
        """Hermes' own opencode-go chat provider stores the key under this name."""
        mod = load_module()
        with mock.patch.dict(mod.os.environ, {}, clear=False):
            mod.os.environ.pop("OPENCODE_API_KEY", None)
            mod.os.environ["OPENCODE_GO_API_KEY"] = "sk-go-key"
            self.assertEqual(mod._read_env_api_key(), "sk-go-key")

    def test_opencode_api_key_wins_over_go_alias(self):
        mod = load_module()
        with mock.patch.dict(mod.os.environ, {}, clear=False):
            mod.os.environ["OPENCODE_API_KEY"] = "sk-zen-key"
            mod.os.environ["OPENCODE_GO_API_KEY"] = "sk-go-key"
            self.assertEqual(mod._read_env_api_key(), "sk-zen-key")


class LivePayloadTests(unittest.TestCase):
    """Shape returned by GET /zen/go/v1/usage (verified against the live API)."""

    LIVE = {
        "usage": {
            "rolling": {"status": "ok", "percent": 4, "resetsAt": "2026-09-18T14:39:39.695Z"},
            "weekly": {"status": "ok", "percent": 1, "resetsAt": "2026-09-21T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-16T16:51:09.000Z"},
        }
    }

    def test_all_three_windows_parse_with_absolute_resets(self):
        mod = load_module()
        windows = mod.parse_usage_payload(self.LIVE, now=0)
        self.assertEqual([w.label for w in windows], ["5-hour", "Weekly", "Monthly"])
        self.assertEqual([w.used_percent for w in windows], [4.0, 1.0, 0.0])
        self.assertIn("2026-09-18T14:39:39", windows[0].reset_at)
        self.assertIn("2026-10-16T16:51:09", windows[2].reset_at)

    def test_windows_stay_visible_when_rolling_is_zero(self):
        """percent 0 is real data, not a missing value."""
        mod = load_module()
        payload = {"usage": {"rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-18T14:39:39.695Z"}}}
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].used_percent, 0.0)


class TransientFailureTests(unittest.TestCase):
    """The usage endpoint flaps with 503 'Go usage is unavailable' (~20-40% of
    calls, independent of User-Agent); a single attempt is not enough."""

    LIVE_BODY = json.dumps(LivePayloadTests.LIVE).encode()

    def _response(self, body):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        return _Resp()

    def _patch_urlopen(self, mod, outcomes):
        """outcomes: list of 'ok' | 503 | 500 | URLError."""
        import urllib.error

        calls = {"n": 0}

        def fake_urlopen(*args, **kwargs):
            index = min(calls["n"], len(outcomes) - 1)
            calls["n"] += 1
            kind = outcomes[index]
            if kind == "ok":
                return self._response(self.LIVE_BODY)
            if kind == "URLError":
                raise urllib.error.URLError("connection reset")
            raise urllib.error.HTTPError(mod._API_URL, kind, "boom", hdrs=None, fp=None)

        mod.urlopen_no_redirect = fake_urlopen
        return calls

    def _run(self, mod, outcomes, attempts=4):
        original = mod.urlopen_no_redirect
        try:
            calls = self._patch_urlopen(mod, outcomes)
            result = mod.fetch_usage("sk-test", attempts=attempts, _sleep=lambda _s: None)
        finally:
            mod.urlopen_no_redirect = original
        return result, calls["n"]

    def test_503_is_retried_until_success(self):
        mod = load_module()
        result, calls = self._run(mod, [503, 503, "ok"])
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([w.used_percent for w in result.windows], [4.0, 1.0, 0.0])
        self.assertEqual(calls, 3)

    def test_network_error_is_retried(self):
        mod = load_module()
        result, calls = self._run(mod, ["URLError", "ok"])
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(calls, 2)

    def test_persistent_503_reports_vendor_wording(self):
        mod = load_module()
        result, calls = self._run(mod, [503], attempts=4)
        self.assertEqual(result.unavailable_reason, "usage-unavailable")
        self.assertEqual(calls, 4)
        self.assertEqual(result.windows, [])

    def test_auth_failure_is_never_retried(self):
        mod = load_module()
        result, calls = self._run(mod, [401])
        self.assertEqual(result.unavailable_reason, "auth-failed")
        self.assertEqual(calls, 1)


class PayloadParsingTests(unittest.TestCase):
    def test_canonical_codexbar_shape(self):
        mod = load_module()
        payload = {
            "rollingUsage": {"usagePercent": 42.5, "resetInSec": 3600},
            "weeklyUsage": {"usagePercent": 10, "resetInSec": 86400},
            "monthlyUsage": {"usagePercent": 5, "resetInSec": 2592000},
            "renewsAt": "2026-09-01T00:00:00Z",
        }
        windows = mod.parse_usage_payload(payload, now=1_000_000)
        labels = [w.label for w in windows]
        self.assertEqual(labels, ["5-hour", "Weekly", "Monthly"])
        self.assertAlmostEqual(windows[0].used_percent, 42.5)
        self.assertTrue(all(w.reset_at for w in windows))

    def test_fraction_percent_is_rescaled(self):
        mod = load_module()
        payload = {"rollingUsage": {"usagePercent": 0.25, "resetInSec": 60}}
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertAlmostEqual(windows[0].used_percent, 25.0)

    def test_integer_percent_one_is_not_rescaled(self):
        mod = load_module()
        payload = {"rollingUsage": {"percent": 1, "resetInSec": 60}}
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertAlmostEqual(windows[0].used_percent, 1.0)

    def test_used_over_limit_computation(self):
        mod = load_module()
        payload = {"rollingUsage": {"used": 3.0, "limit": 12.0, "resetInSec": 30}}
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertAlmostEqual(windows[0].used_percent, 25.0)

    def test_snake_case_aliases(self):
        mod = load_module()
        payload = {
            "rolling_usage": {"used_percent": 50, "reset_in_sec": 10},
        }
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0].used_percent, 50.0)

    def test_nested_data_wrapper(self):
        mod = load_module()
        payload = {
            "data": {
                "rollingUsage": {"percentUsed": 80, "resetSeconds": 120},
                "weeklyUsage": {"percentUsed": 20, "resetSeconds": 7200},
            }
        }
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertEqual([w.label for w in windows], ["5-hour", "Weekly"])

    def test_reset_at_absolute_timestamp(self):
        mod = load_module()
        payload = {"rollingUsage": {"usagePercent": 1, "resetAt": "2026-08-21T23:59:59Z"}}
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertIn("2026-08-21T23:59:59+00:00", str(windows[0].reset_at))

    def test_epoch_millis_reset(self):
        mod = load_module()
        payload = {"rollingUsage": {"usagePercent": 1, "resetAt": 1_800_000_000_000}}
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertIsNotNone(windows[0].reset_at)

    def test_missing_rolling_window_yields_nothing(self):
        mod = load_module()
        payload = {"weeklyUsage": {"usagePercent": 10, "resetInSec": 100}}
        self.assertEqual(mod.parse_usage_payload(payload, now=0), [])

    def test_percent_clamped_to_hundred(self):
        mod = load_module()
        payload = {"rollingUsage": {"usagePercent": 150, "resetInSec": 10}}
        windows = mod.parse_usage_payload(payload, now=0)
        self.assertAlmostEqual(windows[0].used_percent, 100.0)


class FetcherContractTests(unittest.TestCase):
    def test_registered_under_expected_id(self):
        from quota_providers.registry import get_fetcher

        mod = load_module()
        self.assertIsNotNone(get_fetcher("opencode-go"))
        self.assertTrue(callable(mod.fetch_opencode_go_quota))

    def test_missing_credentials_is_fail_open(self):
        """No credentials must be no-credentials, with nothing sent.

        This previously called the fetcher directly, which resolved
        opencode.ai for real whenever a key was present anywhere on the
        machine, and asserted only isinstance(result, QuotaResult) -- true for
        a correct result and a total failure alike.
        """
        from unittest import mock

        mod = load_module()
        with mock.patch.object(mod, "_read_env_api_key", return_value=None), \
             mock.patch.object(mod, "_load_auth_file_key", return_value=None), \
             mock.patch.object(mod.urllib.request, "urlopen") as urlopen:
            result = mod.fetch_opencode_go_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")
        self.assertEqual(result.windows, [])
        urlopen.assert_not_called()

    def test_http_error_mapping(self):
        mod = load_module()

        class FakeHTTPError(Exception):
            code = 401

        # Simulate via urllib error path using monkeypatched urlopen.
        import urllib.error

        def raise_401(*args, **kwargs):
            raise urllib.error.HTTPError(mod._API_URL, 401, "nope", hdrs=None, fp=None)  # type: ignore[arg-type]

        original = mod.urlopen_no_redirect
        try:
            mod.urlopen_no_redirect = raise_401
            result = mod.fetch_usage("fake-key")
            self.assertEqual(result.unavailable_reason, "auth-failed")
        finally:
            mod.urlopen_no_redirect = original


def _safe_call(mod):
    try:
        return mod.fetch_opencode_go_quota()
    except Exception as exc:  # pragma: no cover - contract violation
        raise AssertionError(f"fetcher raised instead of failing open: {exc}") from exc


if __name__ == "__main__":
    unittest.main()
