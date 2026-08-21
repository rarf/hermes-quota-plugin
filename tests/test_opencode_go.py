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
            mod.os.environ.pop("OPENCODE_API_KEY", None)
            self.assertIsNone(mod._read_env_api_key())


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
        from quota_providers.base import QuotaResult

        mod = load_module()
        result = mod.fetch_opencode_go_quota.__wrapped__() if hasattr(
            mod.fetch_opencode_go_quota, "__wrapped__"
        ) else None
        # Direct call may hit the network only if a real key exists; assert the
        # contract instead: whatever comes back is a QuotaResult, never an raise.
        result = result or _safe_call(mod)
        self.assertIsInstance(result, QuotaResult)

    def test_http_error_mapping(self):
        mod = load_module()

        class FakeHTTPError(Exception):
            code = 401

        # Simulate via urllib error path using monkeypatched urlopen.
        import urllib.error

        def raise_401(*args, **kwargs):
            raise urllib.error.HTTPError(mod._API_URL, 401, "nope", hdrs=None, fp=None)  # type: ignore[arg-type]

        original = mod.urllib.request.urlopen
        try:
            mod.urllib.request.urlopen = raise_401
            result = mod.fetch_usage("fake-key")
            self.assertEqual(result.unavailable_reason, "auth-failed")
        finally:
            mod.urllib.request.urlopen = original


def _safe_call(mod):
    try:
        return mod.fetch_opencode_go_quota()
    except Exception as exc:  # pragma: no cover - contract violation
        raise AssertionError(f"fetcher raised instead of failing open: {exc}") from exc


if __name__ == "__main__":
    unittest.main()
