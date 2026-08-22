"""Offline unit tests for the Copilot quota fetcher (stdlib only).

Run from the repo root:  python tests/test_copilot.py
No network access happens here — every HTTP and token boundary is mocked.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from io import BytesIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota_providers import copilot as mod  # noqa: E402


class _FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload: dict):
    def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return _opener


# A canned /copilot_internal/user payload shaped like the live response.
_LIVE_SHAPE = {
    "login": "octocat",
    "copilot_plan": "individual",
    "chat_enabled": True,
    "token_based_billing": True,
    "quota_reset_date": "2026-09-01",
    "quota_snapshots": {
        "chat": {
            "percent_remaining": 100.0,
            "has_quota": True,
            "quota_remaining": 200.0,
            "entitlement": 200,
            "unlimited": False,
            "quota_reset_at": 0,
        },
        "completions": {
            "percent_remaining": 40.5,
            "has_quota": True,
            "quota_remaining": 810.0,
            "entitlement": 2000,
            "unlimited": False,
            "quota_reset_at": 0,
        },
        "premium_interactions": {
            "percent_remaining": 0.0,
            "has_quota": False,
            "quota_remaining": 0.0,
            "entitlement": 0,
            "unlimited": False,
            "quota_reset_at": 0,
        },
    },
}


def _fetch_with_payload(payload, **resolver_overrides):
    resolvers = dict({"token": "gho_test"}, **resolver_overrides)
    with mock.patch.object(mod, "resolve_github_token", return_value=resolvers["token"]), \
            mock.patch.object(mod.urllib.request, "urlopen", _urlopen_returning(payload)):
        return mod.fetch_copilot_quota()


class CredentialTests(unittest.TestCase):
    def test_no_token_is_no_credentials(self):
        with mock.patch.object(mod, "resolve_github_token", return_value=None):
            result = mod.fetch_copilot_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")

    def test_resolver_crash_never_propagates(self):
        def boom():
            raise RuntimeError("subprocess exploded")

        with mock.patch.object(mod, "resolve_github_token", side_effect=boom):
            result = mod.fetch_copilot_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")

    def test_gh_cli_fallback_success(self):
        proc = mock.Mock(returncode=0, stdout="gho_abc\n")
        with mock.patch.dict(sys.modules, {"hermes_cli": None}):
            with mock.patch.object(mod.subprocess, "run", return_value=proc):
                self.assertEqual(mod.resolve_github_token(), "gho_abc")

    def test_gh_cli_fallback_failure(self):
        proc = mock.Mock(returncode=1, stdout="")
        with mock.patch.dict(sys.modules, {"hermes_cli": None}):
            with mock.patch.object(mod.subprocess, "run", return_value=proc):
                self.assertIsNone(mod.resolve_github_token())


class PayloadParsingTests(unittest.TestCase):
    def test_live_shape_windows_and_plan(self):
        windows, plan, details = mod.parse_usage_payload(_LIVE_SHAPE)
        labels = [w.label for w in windows]
        # premium_interactions has has_quota=false + zero entitlement → skipped
        self.assertEqual(labels, ["Chat", "Completions"])
        by_label = {w.label: w for w in windows}
        self.assertAlmostEqual(by_label["Completions"].used_percent, 59.5)
        self.assertAlmostEqual(by_label["Chat"].used_percent, 0.0)
        for w in windows:
            self.assertTrue(w.reset_at and w.reset_at.startswith("2026-09-01"))
        self.assertEqual(plan, "Individual")
        self.assertEqual(details, ["Billing: token-based"])

    def test_percent_fraction_rescaled(self):
        payload = {
            "copilot_plan": "business",
            "quota_snapshots": {
                "chat": {"percent_remaining": 0.25, "has_quota": True},
            },
        }
        windows, plan, _ = mod.parse_usage_payload(payload)
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0].used_percent, 75.0)
        self.assertEqual(plan, "Business")

    def test_unlimited_skipped(self):
        payload = {
            "quota_snapshots": {
                "chat": {"percent_remaining": 50.0, "has_quota": True, "unlimited": True},
                "completions": {"percent_remaining": 10.0, "has_quota": True, "unlimited": False},
            },
        }
        windows, _, _ = mod.parse_usage_payload(payload)
        self.assertEqual([w.label for w in windows], ["Completions"])

    def test_missing_quota_not_rendered_as_used(self):
        payload = {
            "quota_snapshots": {
                "premium_interactions": {
                    "percent_remaining": 100.0,
                    "has_quota": False,
                    "entitlement": 0,
                    "quota_remaining": 0,
                },
            },
        }
        windows, plan, details = mod.parse_usage_payload(payload)
        self.assertEqual((windows, plan, details), ([], None, []))

    def test_camelcase_snapshot_key(self):
        payload = {
            "quotaSnapshots": {
                "chat": {"percent_remaining": 80.0, "has_quota": True},
            },
        }
        windows, _, _ = mod.parse_usage_payload(payload)
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0].used_percent, 20.0)

    def test_garbage_payloads(self):
        for garbage in (None, [], "nope", {}, {"quota_snapshots": "x"}):
            windows, plan, details = mod.parse_usage_payload(garbage)
            self.assertEqual((windows, plan, details), ([], None, []))

    def test_reset_date_variants(self):
        iso, _, _ = mod.parse_usage_payload(
            {"quota_snapshots": {"chat": {"percent_remaining": 1.0}},
             "quota_reset_date_utc": "2026-09-01T00:00:00Z"}
        )
        bare, _, _ = mod.parse_usage_payload(
            {"quota_snapshots": {"chat": {"percent_remaining": 1.0}},
             "quota_reset_date": "2026-09-01"}
        )
        none, _, _ = mod.parse_usage_payload(
            {"quota_snapshots": {"chat": {"percent_remaining": 1.0}}}
        )
        self.assertTrue(iso[0].reset_at.startswith("2026-09-01T"))
        self.assertTrue(bare[0].reset_at.startswith("2026-09-01T"))
        self.assertIsNone(none[0].reset_at)


class NetworkTests(unittest.TestCase):
    def test_happy_path_result(self):
        result = _fetch_with_payload(_LIVE_SHAPE)
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(result.has_data())
        self.assertEqual(result.label, "copilot")

    def test_http_401_maps_to_auth_failed(self):
        import urllib.error

        def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.HTTPError(mod._API_URL, 401, "Unauthorized", None, None)

        with mock.patch.object(mod, "resolve_github_token", return_value="gho_test"), \
                mock.patch.object(mod.urllib.request, "urlopen", _opener):
            result = mod.fetch_copilot_quota()
        self.assertEqual(result.unavailable_reason, "auth-failed")

    def test_http_429_maps_to_http_code(self):
        import urllib.error

        def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.HTTPError(mod._API_URL, 429, "Slow down", None, None)

        with mock.patch.object(mod, "resolve_github_token", return_value="gho_test"), \
                mock.patch.object(mod.urllib.request, "urlopen", _opener):
            result = mod.fetch_copilot_quota()
        self.assertEqual(result.unavailable_reason, "http-429")

    def test_bad_json(self):
        def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
            return _FakeResponse(b"<html>not json</html>")

        with mock.patch.object(mod, "resolve_github_token", return_value="gho_test"), \
                mock.patch.object(mod.urllib.request, "urlopen", _opener):
            result = mod.fetch_copilot_quota()
        self.assertEqual(result.unavailable_reason, "bad-json")

    def test_empty_snapshots_no_data(self):
        result = _fetch_with_payload({"login": "octocat"})
        self.assertEqual(result.unavailable_reason, "no-data")


if __name__ == "__main__":
    unittest.main(verbosity=2)
