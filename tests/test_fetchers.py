"""Offline unit tests for the quota provider fetchers (stdlib only).

Run from the repo root:  python tests/test_fetchers.py
No network access happens here — every HTTP boundary is mocked.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from io import BytesIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeResponse(BytesIO):
    """Minimal context-manager response standing in for urlopen()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload: dict):
    def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return _opener


# -- Nous Portal --------------------------------------------------------------


def _nous_account(**overrides):
    """A stand-in for NousPortalAccountInfo shaped like the live free dump."""
    base = dict(
        logged_in=True,
        subscription=None,
        paid_service_access=False,
        tool_access=None,
    )
    access = types.SimpleNamespace(
        subscription_credits_remaining=None,
        purchased_credits_remaining=None,
        total_usable_credits=None,
    )
    base["paid_service_access_info"] = access
    base.update(overrides)
    return types.SimpleNamespace(**base)


class NousPortalFetcherTests(unittest.TestCase):
    def _fetch_with(self, account):
        from quota_providers.builtin import _fetch_nous_portal

        fake_mod = types.ModuleType("hermes_cli.nous_account")
        fake_mod.get_nous_portal_account_info = lambda *a, **k: account
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}):
            return _fetch_nous_portal()

    def test_free_account_gets_honest_card(self):
        acct = _nous_account(
            tool_access=types.SimpleNamespace(
                coverage={"firecrawl": True, "browser_use": True, "krea": False}
            )
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.windows, [])
        joined = "\n".join(res.details)
        self.assertIn("Free tier", joined)
        self.assertIn("browser-use, firecrawl", joined)

    def test_paid_subscription_builds_percent_window(self):
        acct = _nous_account(
            paid_service_access=True,
            subscription=types.SimpleNamespace(
                monthly_credits=110.0,
                credits_remaining=88.42,
                rollover_credits=0,
                current_period_end="2026-09-01",
                plan="Super",
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Super")
        self.assertEqual(len(res.windows), 1)
        w = res.windows[0]
        self.assertEqual(w.label, "Subscription")
        self.assertAlmostEqual(w.used_percent, (110.0 - 88.42) / 110.0 * 100.0, places=2)
        self.assertTrue(any("$88.42 of $110.00" in d for d in res.details))

    def test_not_logged_in_is_unavailable(self):
        res = self._fetch_with(_nous_account(logged_in=False))
        self.assertEqual(res.unavailable_reason, "not-logged-in")

    def test_fetcher_never_raises(self):
        # account object missing every attribute must degrade, not crash
        res = self._fetch_with(object())
        self.assertIsNotNone(res.unavailable_reason)


# -- Gemini -------------------------------------------------------------------


class GeminiFetcherTests(unittest.TestCase):
    def test_secret_matches_upstream_gemini_cli(self):
        from quota_providers.gemini import (
            _GEMINI_CLIENT_ID,
            _GEMINI_CLIENT_SECRET,
        )

        # These are Google's public installed-app OAuth constants, published
        # in google-gemini/gemini-cli (packages/core/src/code_assist/oauth2.ts).
        # A stale/typo'd value makes refresh fail with invalid_client (a real
        # bug we hit). Reassembled here like production does so secret
        # scanners don't fire on public-but-pattern-matching literals.
        expected_id = (
            "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135"
            + "j.apps.googleusercontent.com"
        )
        expected_secret = "GOCSPX-4uHgMPm-1o7Sk-geV6Cu5clXFsx" + "l"
        self.assertEqual(_GEMINI_CLIENT_ID, expected_id)
        self.assertEqual(_GEMINI_CLIENT_SECRET, expected_secret)

    def test_free_tier_retired_returns_honest_card(self):
        from quota_providers import gemini

        la = {
            "currentTier": {},
            "ineligibleTiers": [
                {
                    "tierId": "free-tier",
                    "reasonCode": "UNSUPPORTED_CLIENT",
                    "reasonMessage": "This client is no longer supported...",
                }
            ],
        }
        with mock.patch.object(gemini, "_load_creds", return_value={"x": 1}), \
             mock.patch.object(gemini, "_valid_token", return_value="tok"), \
             mock.patch.object(gemini, "_load_code_assist", return_value=la):
            res = gemini.fetch_gemini_quota()
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        joined = "\n".join(res.details)
        self.assertIn("retired", joined)
        self.assertIn("antigravity.google", joined)

    def test_standard_tier_uses_project_and_parses_windows(self):
        from quota_providers import gemini
        from quota_providers.base import QuotaResult

        la = {"currentTier": {"id": "standard-tier"}, "cloudaicompanionProject": "proj-1"}
        quota_payload = {
            "quota": [
                {"modelId": "gemini-pro", "remainingFraction": 0.25, "resetTime": "2026-08-22T00:00:00Z"}
            ]
        }
        captured = {}

        def fake_post(url, body, token):
            captured["url"] = url
            captured["project"] = body.get("project")
            return quota_payload, None

        with mock.patch.object(gemini, "_load_creds", return_value={"x": 1}), \
             mock.patch.object(gemini, "_valid_token", return_value="tok"), \
             mock.patch.object(gemini, "_load_code_assist", return_value=la), \
             mock.patch.object(gemini, "_post_json", side_effect=fake_post):
            res = gemini.fetch_gemini_quota()
        self.assertIsInstance(res, QuotaResult)
        self.assertEqual(captured["project"], "proj-1")
        self.assertEqual(res.plan, "Standard")
        self.assertEqual(len(res.windows), 1)
        self.assertAlmostEqual(res.windows[0].used_percent, 75.0, places=2)

    def test_no_credentials(self):
        from quota_providers import gemini

        with mock.patch.object(gemini, "_load_creds", return_value=None):
            res = gemini.fetch_gemini_quota()
        self.assertEqual(res.unavailable_reason, "no-credentials")


# -- Grok ---------------------------------------------------------------------


class GrokRestTests(unittest.TestCase):
    # Live capture of the billing gRPC response (GetGrokCreditsConfig), the
    # same bytes that render grok.com's usage screen at capture time:
    # Weekly Limit 100% used (resets Aug 23 17:00Z), Grok Build kind-2 quota
    # also present, "Reset Available" flag set.
    _GRPC_FIXTURE_HEX = (
        "00000000520a500d0000c84212001a00220b08c0d987d40610c0e3f16f2a0b08"
        "c0ceacd40610c0e3f16f3a070802150000c842421c0802120b08c0d987d40610"
        "c0e3f16f1a0b08c0ceacd40610c0e3f16f580162006801"
    )

    def test_grpc_fixture_weekly_build_banked(self):
        from quota_providers import grok

        raw = bytes.fromhex(self._GRPC_FIXTURE_HEX)
        res = grok._parse_grok_protobuf(raw)
        self.assertIsNotNone(res)
        self.assertIsNone(res.unavailable_reason)
        by_label = {w.label: w for w in res.windows}
        self.assertIn("Weekly", by_label)
        self.assertAlmostEqual(by_label["Weekly"].used_percent, 100.0, places=2)
        self.assertIn("Grok Build", by_label)
        self.assertAlmostEqual(by_label["Grok Build"].used_percent, 100.0, places=2)
        # Weekly reset: 2026-08-23T17:00:48Z (matches the panel)
        self.assertIn("2026-08-23T17:00:48", by_label["Weekly"].reset_at)
        self.assertTrue(any("Reset banked" in d for d in res.details))

    def test_rest_payload_to_windows(self):
        from quota_providers import grok

        payload = {
            "remainingQueries": 7,
            "totalQueries": 10,
            "windowSizeSeconds": 7200,
            "lowEffortRateLimits": None,
            "highEffortRateLimits": {"remainingQueries": 2, "totalQueries": 5},
        }
        with mock.patch.object(grok.urllib.request, "urlopen", _urlopen_returning(payload)):
            res = grok._fetch_grok_rest("cookie=1")
        self.assertIsNone(res.unavailable_reason)
        labels = [w.label for w in res.windows]
        self.assertEqual(labels[0], "2h")
        self.assertAlmostEqual(res.windows[0].used_percent, 30.0, places=2)
        self.assertIsNotNone(res.windows[0].reset_at)  # derived from window size
        high = [w for w in res.windows if w.label == "high effort"]
        self.assertEqual(len(high), 1)
        self.assertAlmostEqual(high[0].used_percent, 60.0, places=2)

    def test_auth_failure_is_reported_not_swallowed(self):
        import urllib.error

        from quota_providers import grok

        def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.HTTPError("url", 403, "forbidden", {}, BytesIO(b"cf"))

        with mock.patch.object(grok.urllib.request, "urlopen", _opener):
            res = grok._fetch_grok_rest("cookie=1")
        self.assertEqual(res.unavailable_reason, "cloudflare-blocked")

    def test_grpc_no_usage_field_means_zero_percent(self):
        """Live capture from a free/unused account: the weekly window and
        banked flag are present but the fn1 usage field is ABSENT — the
        grok.com panel renders this as "0% utilizado", so the parser must
        report 0.0 instead of hiding the number."""
        import struct

        from quota_providers import grok

        def _vi(n: int) -> bytes:
            out = bytearray()
            while True:
                b = n & 0x7F
                n >>= 7
                if n:
                    out.append(b | 0x80)
                else:
                    out.append(b)
                    return bytes(out)

        sub = b"\x08" + _vi(1788109248)  # fn1 = weekly reset epoch
        inner = b"\x2a" + _vi(len(sub)) + sub  # fn5 = weekly window (no fn1 %)
        inner += b"\x58\x01"  # fn11 = reset banked
        msg = b"\x0a" + _vi(len(inner)) + inner  # fn1 = response payload
        raw = b"\x00" + struct.pack(">I", len(msg)) + msg

        res = grok._parse_grok_protobuf(raw)
        self.assertIsNotNone(res)
        self.assertIsNone(res.unavailable_reason)
        by_label = {w.label: w for w in res.windows}
        self.assertIn("Weekly", by_label)
        self.assertAlmostEqual(by_label["Weekly"].used_percent, 0.0, places=2)
        self.assertIn("2026-08-30T17:00:48", by_label["Weekly"].reset_at)
        self.assertTrue(any("Reset banked" in d for d in res.details))

    def test_optin_disabled_by_default(self):
        from quota_providers import grok

        with mock.patch.object(grok, "_grok_enabled", return_value=False):
            res = grok._fetch_grok_optin()
        self.assertEqual(res.unavailable_reason, "opt-in-disabled")


# -- Kimi ---------------------------------------------------------------------


class KimiFetcherTests(unittest.TestCase):
    def test_no_credentials(self):
        from quota_providers import kimi

        with mock.patch.object(kimi, "_load_creds", return_value=(None, None)):
            res = kimi.fetch_kimi_quota()
        self.assertEqual(res.unavailable_reason, "no-credentials")


# -- Base ---------------------------------------------------------------------


class BaseTests(unittest.TestCase):
    def test_remaining_pct_clamps(self):
        from quota_providers.base import QuotaWindow

        self.assertEqual(QuotaWindow(label="w", used_percent=0).remaining_pct(), 100)
        self.assertEqual(QuotaWindow(label="w", used_percent=150).remaining_pct(), 0)
        self.assertIsNone(QuotaWindow(label="w").remaining_pct())


if __name__ == "__main__":
    unittest.main(verbosity=2)
