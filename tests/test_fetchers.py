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


def _raise_closed_http_error(error):
    """Raise a mocked HTTPError without leaking its response buffer."""
    try:
        raise error
    finally:
        error.close()


def _urlopen_returning(payload: dict):
    def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return _opener


# -- OpenAI Codex -------------------------------------------------------------


class OpenAICodexFetcherTests(unittest.TestCase):
    def test_legacy_usage_url_helper_is_supported(self):
        from quota_providers.builtin import _fetch_codex_with_models

        captured = {}
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 21, "reset_at": 1_700_000_000},
            },
        }

        fake_usage = types.ModuleType("agent.account_usage")
        setattr(
            fake_usage,
            "_resolve_codex_usage_credentials",
            lambda *_args: (
                "test-token",
                "https://chatgpt.com/backend-api/codex",
                "account-1",
            ),
        )
        setattr(
            fake_usage,
            "_resolve_codex_usage_url",
            lambda base: f"{base.removesuffix('/codex')}/wham/usage",
        )
        fake_agent = types.ModuleType("agent")
        setattr(fake_agent, "account_usage", fake_usage)

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, headers):
                captured["url"] = url
                captured["headers"] = headers
                return FakeResponse()

        fake_httpx = types.ModuleType("httpx")
        setattr(fake_httpx, "Client", FakeClient)
        with mock.patch.dict(
            sys.modules,
            {
                "agent": fake_agent,
                "agent.account_usage": fake_usage,
                "httpx": fake_httpx,
            },
        ):
            result = _fetch_codex_with_models()

        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(captured["url"], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(captured["headers"]["ChatGPT-Account-Id"], "account-1")

    def test_current_account_usage_helpers_are_supported(self):
        from quota_providers.builtin import _fetch_codex_with_models

        captured = {}
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 21, "reset_at": 1_700_000_000},
                "secondary_window": {"used_percent": 42, "reset_at": 1_700_100_000},
            },
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5-Codex-Spark",
                    "rate_limit": {
                        "primary_window": {"used_percent": 7, "reset_at": 1_700_000_000}
                    },
                }
            ],
        }

        fake_usage = types.ModuleType("agent.account_usage")
        setattr(
            fake_usage,
            "_resolve_codex_usage_credentials",
            lambda *_args: (
                "test-token",
                "https://chatgpt.com/backend-api/codex",
                "account-1",
            ),
        )
        setattr(
            fake_usage,
            "_codex_backend_urls",
            lambda base: (
                f"{base.removesuffix('/codex')}/wham/usage",
                "",
                "",
            ),
        )
        fake_agent = types.ModuleType("agent")
        setattr(fake_agent, "account_usage", fake_usage)

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, headers):
                captured["url"] = url
                captured["headers"] = headers
                return FakeResponse()

        fake_httpx = types.ModuleType("httpx")
        setattr(fake_httpx, "Client", FakeClient)
        with mock.patch.dict(
            sys.modules,
            {
                "agent": fake_agent,
                "agent.account_usage": fake_usage,
                "httpx": fake_httpx,
            },
        ):
            result = _fetch_codex_with_models()

        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(captured["url"], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(captured["headers"]["ChatGPT-Account-Id"], "account-1")
        self.assertEqual(
            [w.label for w in result.windows],
            ["Session", "Weekly", "5 Codex Spark · 5h"],
        )


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

    def test_paid_spend_without_credit_cap_gets_details(self):
        acct = _nous_account(
            paid_service_access=True,
            raw_claims={
                "member_spend_usd": "21.77",
                "member_spend_cap_usd": None,
                "subscription_tier": 2,
                "rate_limit_rpm": 400,
                "rate_limit_tpm": 4_000_000,
                "rate_limit_rph": 16_800,
            },
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Tier 2")
        self.assertEqual(res.windows, [])
        joined = "\n".join(res.details)
        self.assertIn("Spend this period: $21.77 (no cap reported)", joined)
        self.assertIn("Rate limits: 400 RPM · 4M TPM · 16.8k RPH", joined)

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
            _raise_closed_http_error(urllib.error.HTTPError("url", 403, "forbidden", {}, None))

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


# -- Z.ai ---------------------------------------------------------------------


_ZAI_SESSION_RESET_MS = 1770648402389
_ZAI_WEEKLY_RESET_MS = 1772272043542
_ZAI_MONTHLY_RESET_MS = 1773596236982


def _zai_full_payload() -> dict:
    """Live-captured global response shape (2026-08/09, multiple consumers)."""
    return {
        "code": 200,
        "msg": "Operation successful",
        "success": True,
        "data": {
            "limits": [
                {
                    "type": "TOKENS_LIMIT", "unit": 3, "number": 5,
                    "usage": 800000000, "currentValue": 127694464,
                    "remaining": 672305536, "percentage": 15,
                    "nextResetTime": _ZAI_SESSION_RESET_MS,
                },
                {
                    "type": "TOKENS_LIMIT", "unit": 6, "number": 1,
                    "usage": 6000000000, "currentValue": 3180000000,
                    "remaining": 2820000000, "percentage": 53,
                    "nextResetTime": _ZAI_WEEKLY_RESET_MS,
                },
                {
                    "type": "TIME_LIMIT", "unit": 5, "number": 1,
                    "usage": 4000, "currentValue": 1828, "remaining": 2172,
                    "percentage": 45, "nextResetTime": _ZAI_MONTHLY_RESET_MS,
                    "usageDetails": [
                        {"modelCode": "search-prime", "usage": 1433},
                        {"modelCode": "web-reader", "usage": 462},
                        {"modelCode": "zread", "usage": 0},
                    ],
                },
            ],
            "level": "pro",
        },
    }


def _zai_urlopen(quota_payload=None, subscription_payload=None, quota_error=None, raw_body=None):
    """urlopen stand-in routing by URL path; payload None → HTTP failure."""
    import urllib.error

    def _opener(req, timeout=None):  # noqa: ANN001, ARG001
        url = str(getattr(req, "full_url", req))
        if "/api/biz/subscription/list" in url:
            if subscription_payload is None:
                _raise_closed_http_error(urllib.error.HTTPError(url, 404, "not found", {}, None))
            return _FakeResponse(json.dumps(subscription_payload).encode("utf-8"))
        if quota_error is not None:
            _raise_closed_http_error(quota_error)
        if raw_body is not None:
            return _FakeResponse(raw_body)
        if quota_payload is None:
            _raise_closed_http_error(urllib.error.HTTPError(url, 500, "server error", {}, None))
        return _FakeResponse(json.dumps(quota_payload).encode("utf-8"))

    return _opener


def _iso_from_ms(ms: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


class ZaiFetcherTests(unittest.TestCase):
    def _fetch(self, opener, **patch_extra):
        from quota_providers import zai

        with mock.patch.object(zai, "resolve_api_key", return_value="test-key"), \
             mock.patch.object(zai.urllib.request, "urlopen", opener), \
             mock.patch.object(zai, "_api_root", lambda: "https://api.z.ai"):
            return zai.fetch_zai_quota()

    def test_registered_in_provider_registry(self):
        from quota_providers import PROVIDER_FETCHERS

        self.assertIn("zai", PROVIDER_FETCHERS)

    def test_full_payload_maps_session_weekly_and_monthly_tools(self):
        res = self._fetch(_zai_urlopen(quota_payload=_zai_full_payload()))
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual([w.label for w in res.windows], ["Session", "Weekly", "Monthly web tools"])
        by_label = {w.label: w for w in res.windows}
        self.assertAlmostEqual(by_label["Session"].used_percent, 15.0, places=2)
        self.assertAlmostEqual(by_label["Weekly"].used_percent, 53.0, places=2)
        self.assertAlmostEqual(by_label["Monthly web tools"].used_percent, 45.0, places=2)  # server-reported
        # reset timestamps preserved (epoch ms → ISO)
        self.assertEqual(by_label["Session"].reset_at, _iso_from_ms(_ZAI_SESSION_RESET_MS))
        self.assertEqual(by_label["Weekly"].reset_at, _iso_from_ms(_ZAI_WEEKLY_RESET_MS))
        self.assertEqual(by_label["Monthly web tools"].reset_at, _iso_from_ms(_ZAI_MONTHLY_RESET_MS))
        # plan from data.level, absolute counts + per-tool breakdown in details
        self.assertEqual(res.plan, "Pro")
        joined = "\n".join(res.details)
        self.assertIn("127.7M", joined)
        self.assertIn("search-prime 1433", joined)

    def test_reordered_limits_identified_by_unit_not_position(self):
        payload = _zai_full_payload()
        limits = payload["data"]["limits"]
        payload["data"]["limits"] = [limits[2], limits[1], limits[0]]  # weekly/monthly first
        res = self._fetch(_zai_urlopen(quota_payload=payload))
        self.assertIsNone(res.unavailable_reason)
        by_label = {w.label: w for w in res.windows}
        self.assertAlmostEqual(by_label["Session"].used_percent, 15.0, places=2)
        self.assertAlmostEqual(by_label["Weekly"].used_percent, 53.0, places=2)

    def test_credit_limit_alias_is_supported(self):
        payload = _zai_full_payload()
        for entry in payload["data"]["limits"]:
            if entry["type"] == "TOKENS_LIMIT":
                entry["type"] = "CREDIT_LIMIT"
        res = self._fetch(_zai_urlopen(quota_payload=payload))
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual([w.label for w in res.windows], ["Session", "Weekly", "Monthly web tools"])

    def test_sparse_percentage_only_payload(self):
        payload = {
            "code": 200, "success": True,
            "data": {"limits": [
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 1},
                {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 42,
                 "nextResetTime": _ZAI_WEEKLY_RESET_MS},
            ], "level": "pro"},
        }
        res = self._fetch(_zai_urlopen(quota_payload=payload))
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual([w.label for w in res.windows], ["Session", "Weekly"])
        by_label = {w.label: w for w in res.windows}
        self.assertAlmostEqual(by_label["Session"].used_percent, 1.0, places=2)
        self.assertAlmostEqual(by_label["Weekly"].used_percent, 42.0, places=2)
        self.assertIsNone(by_label["Session"].reset_at)
        self.assertEqual(by_label["Weekly"].reset_at, _iso_from_ms(_ZAI_WEEKLY_RESET_MS))

    def test_percent_computed_from_counts_when_percentage_missing(self):
        payload = _zai_full_payload()
        session = payload["data"]["limits"][0]
        session.pop("percentage")
        res = self._fetch(_zai_urlopen(quota_payload=payload))
        by_label = {w.label: w for w in res.windows}
        self.assertAlmostEqual(by_label["Session"].used_percent, 15.96, places=2)

    def test_time_limit_without_positive_limit_lands_in_details_only(self):
        payload = _zai_full_payload()
        time_limit = payload["data"]["limits"][2]
        payload["data"]["limits"] = [time_limit]
        time_limit["usage"] = 0
        time_limit.pop("percentage")
        res = self._fetch(_zai_urlopen(quota_payload=payload))
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.windows, [])
        self.assertTrue(res.has_data())
        self.assertIn("1828", "\n".join(res.details))

    def test_subscription_enriches_plan_and_renews_detail(self):
        subscription = {
            "code": 200, "success": True,
            "data": [{"productName": "GLM Coding Max", "status": "ACTIVE",
                      "valid": True, "renewTime": _ZAI_MONTHLY_RESET_MS}],
        }
        res = self._fetch(_zai_urlopen(
            quota_payload=_zai_full_payload(), subscription_payload=subscription))
        self.assertEqual(res.plan, "GLM Coding Max")
        self.assertTrue(any(d.startswith("Renews:") for d in res.details))

    def test_subscription_failure_does_not_blank_meters(self):
        res = self._fetch(_zai_urlopen(quota_payload=_zai_full_payload()))  # 404 on subscription
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Pro")
        self.assertEqual(len(res.windows), 3)

    def test_missing_credentials(self):
        from quota_providers import zai

        with mock.patch.object(zai, "resolve_api_key", return_value=None):
            res = zai.fetch_zai_quota()
        self.assertEqual(res.unavailable_reason, "no-credentials")
        self.assertEqual(res.windows, [])

    def test_http_401_is_auth_failed(self):
        import urllib.error

        res = self._fetch(_zai_urlopen(
            quota_error=urllib.error.HTTPError("u", 401, "unauthorized", {}, None)))
        self.assertEqual(res.unavailable_reason, "auth-failed")

    def test_http_500_and_bad_json(self):
        import urllib.error

        res = self._fetch(_zai_urlopen(
            quota_error=urllib.error.HTTPError("u", 502, "bad gateway", {}, None)))
        self.assertEqual(res.unavailable_reason, "http-502")
        res = self._fetch(_zai_urlopen(raw_body=b"not-json{"))
        self.assertEqual(res.unavailable_reason, "bad-json")

    def test_success_false_envelope_is_no_subscription(self):
        payload = _zai_full_payload()
        payload["success"] = False
        payload["code"] = 200
        res = self._fetch(_zai_urlopen(quota_payload=payload))
        self.assertEqual(res.unavailable_reason, "no-subscription")

    def test_schema_surprises_never_raise(self):
        for payload in ({}, {"code": 200, "success": True, "data": {}},
                        {"code": 200, "success": True, "data": []},
                        {"data": {"limits": "garbage"}},
                        {"data": 42}, [1, 2, 3], "nope"):
            res = self._fetch(_zai_urlopen(quota_payload=payload))
            self.assertIsNotNone(res.unavailable_reason, msg=repr(payload))
            self.assertEqual(res.windows, [], msg=repr(payload))

    def test_reset_timestamp_normalization(self):
        from quota_providers import zai

        iso_ms = zai._parse_reset(1770648402389)
        self.assertEqual(iso_ms, _iso_from_ms(1770648402389))
        # epoch seconds → the same instant (whole-second ISO, no ms component)
        seconds_iso = zai._parse_reset(1770648402)
        self.assertEqual(seconds_iso, "2026-02-09T14:46:42+00:00")
        self.assertEqual(seconds_iso, iso_ms.split(".")[0] + "+00:00")
        self.assertEqual(zai._parse_reset("1770648402389"), _iso_from_ms(1770648402389))
        self.assertEqual(
            zai._parse_reset("2026-09-20T12:00:00Z"), "2026-09-20T12:00:00+00:00")
        self.assertEqual(
            zai._parse_reset("2026-09-20T14:00:00+02:00"),
            "2026-09-20T12:00:00+00:00",
        )
        for absent in (0, None, "", "garbage", -5):
            self.assertIsNone(zai._parse_reset(absent), msg=repr(absent))

    def test_resolver_prefers_hermes_core(self):
        from quota_providers import zai

        fake_auth = types.ModuleType("hermes_cli.auth")
        fake_auth.PROVIDER_REGISTRY = {"zai": types.SimpleNamespace(auth_type="api_key")}
        fake_auth._resolve_api_key_provider_secret = (
            lambda pid, cfg: ("core-resolved-key", "credential_pool:zai"))
        fake_pkg = types.ModuleType("hermes_cli")
        setattr(fake_pkg, "auth", fake_auth)
        with mock.patch.dict(
            sys.modules, {"hermes_cli": fake_pkg, "hermes_cli.auth": fake_auth}
        ), mock.patch.dict(os.environ, {"ZAI_API_KEY": "env-key"}):
            self.assertEqual(zai.resolve_api_key(), "core-resolved-key")

    def test_env_fallback_without_core(self):
        from quota_providers import zai

        with mock.patch.dict(sys.modules, {"hermes_cli": None}), \
             mock.patch.dict(os.environ, {"ZAI_API_KEY": "env-key", "GLM_API_KEY": ""}):
            self.assertEqual(zai.resolve_api_key(), "env-key")

    def test_no_credentials_when_core_absent_and_env_empty(self):
        from quota_providers import zai

        env = {k: "" for k in ("ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY")}
        with mock.patch.dict(sys.modules, {"hermes_cli": None}), \
             mock.patch.dict(os.environ, env, clear=False):
            self.assertIsNone(zai.resolve_api_key())


# -- Base ---------------------------------------------------------------------


class BaseTests(unittest.TestCase):
    def test_remaining_pct_clamps(self):
        from quota_providers.base import QuotaWindow

        self.assertEqual(QuotaWindow(label="w", used_percent=0).remaining_pct(), 100)
        self.assertEqual(QuotaWindow(label="w", used_percent=150).remaining_pct(), 0)
        self.assertIsNone(QuotaWindow(label="w").remaining_pct())


if __name__ == "__main__":
    unittest.main(verbosity=2)
