"""Offline unit tests for the quota provider fetchers (stdlib only).

Run from the repo root:  python tests/test_fetchers.py
No network access happens here — every HTTP boundary is mocked.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import types
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



# The cross-profile payload share is covered by test_anthropic_shared_payload.py.
# Here it is switched off so one test's successful read cannot satisfy the next
# test's request (every test in this module reuses the same synthetic token).
_SHARE_PATCHES = []


def setUpModule():  # noqa: N802 - unittest hook
    from quota_providers import builtin as _b

    for patch in (
        mock.patch.object(_b, "_load_shared_payload", return_value=None),
        mock.patch.object(_b, "_store_shared_payload", return_value=None),
    ):
        patch.start()
        _SHARE_PATCHES.append(patch)


def tearDownModule():  # noqa: N802 - unittest hook
    while _SHARE_PATCHES:
        _SHARE_PATCHES.pop().stop()


class _FakeResponse(BytesIO):
    """Minimal context-manager response standing in for urlopen()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _raise_closed_http_error(error):
    """Raise a mocked HTTPError without leaking its response buffer.

    An fp-less HTTPError skips ``addinfourl.__init__``, so on Python 3.9
    ``close()`` resolves through the tempfile wrapper and raises
    ``KeyError('file')`` — inside ``finally``, which would replace the error
    being raised and turn a mocked HTTP failure into ``fetch-error:KeyError``.
    Nothing is open in that case, so only close when there is a buffer.
    """
    try:
        raise error
    finally:
        if error.fp is not None:
            error.close()


def _urlopen_returning(payload: dict):
    def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return _opener


# -- OpenAI Codex -------------------------------------------------------------


class OpenAICodexFetcherTests(unittest.TestCase):
    def test_usage_url_helpers_preserve_saved_backend(self):
        from quota_providers import builtin
        from test_codex_accounts import auth_modules, token, mock_http
        for helper in ('_codex_backend_urls', '_resolve_codex_usage_url'):
            modules = auth_modules([{'access_token': token('synthetic-a'),
                                     'base_url': 'https://saved.invalid/codex'}])
            usage = modules['agent.account_usage']
            del usage._codex_backend_urls
            resolve = mock.Mock(return_value=('https://saved.invalid/api/codex/usage',)
                                if helper == '_codex_backend_urls' else 'https://saved.invalid/api/codex/usage')
            setattr(usage, helper, resolve)
            route = mock.Mock(return_value='https://configured.invalid')
            modules['hermes_cli.auth_codex']._codex_pool_route_base_url = route
            with self.subTest(helper=helper), mock.patch.dict(sys.modules, modules), mock_http(side_effect=_urlopen_returning({'rate_limit': {'primary_window': {'used_percent': 21}}})) as http:
                result = builtin._fetch_codex_with_models()
            self.assertIsNone(result.unavailable_reason)
            self.assertEqual(str(http.call_args.args[0].url), 'https://saved.invalid/api/codex/usage')
            # A saved account host must not inherit the model's proxy route.
            route.assert_not_called()
            resolve.assert_called_once_with('https://saved.invalid/codex')

    def test_legacy_saved_account_header_and_old_auth_base_helper(self):
        from quota_providers import builtin
        from test_codex_accounts import auth_modules, token, mock_http
        modules = auth_modules([], {'tokens': {'access_token': token('jwt-account'),
                                               'account_id': 'synthetic-saved-account'}})
        modules['hermes_cli.auth_codex'] = None
        auth = types.ModuleType('hermes_cli.auth')
        auth._codex_base_url = lambda: 'https://legacy-configured.invalid/codex'
        modules['hermes_cli.auth'] = auth
        with mock.patch.dict(sys.modules, modules), mock_http(side_effect=_urlopen_returning({'rate_limit': {'primary_window': {'used_percent': 21}}})) as http:
            result = builtin._fetch_codex_with_models()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(str(http.call_args.args[0].url), 'https://legacy-configured.invalid/wham/usage')
        self.assertEqual(http.call_args.args[0].headers['ChatGPT-Account-Id'], 'synthetic-saved-account')

    def test_native_httpx_keeps_redirects_disabled_and_accounts_independent(self):
        try:
            import httpx
        except ImportError:
            self.skipTest('optional httpx integration')
        from quota_providers import builtin
        from test_codex_accounts import auth_modules, token, payload
        client = httpx.Client
        requests = []
        def respond(request):
            requests.append(request)
            if request.headers['ChatGPT-Account-Id'] == 'synthetic-a':
                return httpx.Response(302, headers={'Location': 'https://redirect.invalid'},
                                      text='SYNTHETIC_PRIVATE_ERROR')
            return httpx.Response(200, json=payload(21))
        with mock.patch.dict(sys.modules, auth_modules([
                {'access_token': token('synthetic-a')}, {'access_token': token('synthetic-b')}])), mock.patch.object(httpx, 'Client', side_effect=lambda **kw: client(transport=httpx.MockTransport(respond), **kw)):
            result = builtin._fetch_codex_with_models()
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.url.host == 'chatgpt.com' for request in requests))
        self.assertEqual(result.accounts[0].unavailable_reason, 'http-302')
        self.assertEqual(result.accounts[1].windows[0].used_percent, 21)
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR', repr(result))

    def test_worker_preserves_profile_context_for_endpoint_helpers(self):
        from contextvars import ContextVar
        from quota_providers import builtin
        from test_codex_accounts import auth_modules, token, mock_http
        context = ContextVar('synthetic_endpoint', default='https://wrong.invalid')
        modules = auth_modules([{'access_token': token('synthetic-a')}])
        modules['hermes_cli.auth_codex']._codex_pool_route_base_url = lambda base: context.get()
        handle = context.set('https://profile.invalid/codex')
        try:
            with mock.patch.dict(sys.modules, modules), mock_http(side_effect=_urlopen_returning({'rate_limit': {'primary_window': {'used_percent': 21}}})) as http:
                result = builtin._fetch_codex_with_models()
            self.assertIsNone(result.unavailable_reason)
            self.assertEqual(str(http.call_args.args[0].url), 'https://profile.invalid/wham/usage')
        finally:
            context.reset(handle)

    def test_saved_single_account_request_and_spark_windows(self):
        from quota_providers import builtin
        from test_codex_accounts import auth_modules, token, mock_http
        payload = {
            "plan_type": "plus",
            "rate_limit": {"primary_window": {"used_percent": 21}, "secondary_window": {"used_percent": 42}},
            "additional_rate_limits": [{"limit_name": "GPT-5-Codex-Spark", "rate_limit": {"primary_window": {"used_percent": 7}}}],
        }
        with mock.patch.dict(sys.modules, auth_modules([{"access_token": token("synthetic-a")} ])), mock_http(side_effect=_urlopen_returning(payload)) as http:
            result = builtin._fetch_codex_with_models()
        self.assertIsNone(result.unavailable_reason)
        request = http.call_args.args[0]
        self.assertEqual(str(request.url), "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(request.headers.get("Chatgpt-account-id"), "synthetic-a")
        self.assertEqual([w.label for w in result.windows], ["Session", "Weekly", "5 Codex Spark · 5h"])


# -- Nous Portal --------------------------------------------------------------


def _nous_account(**overrides):
    """A stand-in for NousPortalAccountInfo shaped like the live free dump."""
    base = dict(
        logged_in=True,
        source="jwt",
        fresh=False,
        user_id="synthetic-user",
        org_id="synthetic-org",
        org_slug="synthetic-org",
        portal_base_url="https://portal.invalid",
        account_tier=None,
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


def _nous_access(**overrides):
    """A fresh account-API paid_service_access block with explicit facts."""
    base = dict(
        allowed=False,
        paid_access=False,
        reason="no_usable_credits",
        has_active_subscription=False,
        active_subscription_is_paid=False,
        subscription_tier=None,
        subscription_monthly_charge=None,
        subscription_credits_remaining=0.0,
        purchased_credits_remaining=0.0,
        total_usable_credits=0.0,
        member_spend_cap_exceeded=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _nous_fresh_free(**overrides):
    """A fresh account-API Free snapshot, with no tool-access field by default."""
    base = dict(
        source="account_api",
        fresh=True,
        account_tier="free",
        paid_service_access=False,
        paid_service_access_info=_nous_access(),
        tool_access=None,
    )
    base.update(overrides)
    return _nous_account(**base)


_NOUS_MISSING = object()


class NousPortalFetcherTests(unittest.TestCase):
    def _fetch_with(self, account, *, fresh=_NOUS_MISSING, cached=_NOUS_MISSING):
        from quota_providers import builtin

        calls: list[bool] = []

        def _choose(value, fallback):
            if value is _NOUS_MISSING:
                value = fallback
            if isinstance(value, BaseException):
                raise value
            return value

        def fake_get(*args, **kwargs):
            is_fresh = bool(kwargs.get("force_fresh"))
            calls.append(is_fresh)
            return _choose(fresh if is_fresh else cached, account)

        fake_mod = types.ModuleType("hermes_cli.nous_account")
        setattr(fake_mod, "get_nous_portal_account_info", fake_get)
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}):
            result = builtin._fetch_nous_portal()
        self.lookup_calls = calls
        return result

    def test_free_account_gets_honest_card(self):
        acct = _nous_account(
            account_tier="free",
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True, "browser_use": True, "krea": False},
            )
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.windows, [])
        self.assertIsNone(res.api_calls_available)
        self.assertEqual(res.access_state, "free")
        self.assertEqual(self.lookup_calls, [False, True])
        joined = "\n".join(res.details)
        self.assertIn("Free tier", joined)
        self.assertIn("browser-use, firecrawl", joined)
        self.assertNotIn("depleted", joined)

    def test_live_plus_usage_window_surfaces_windows_and_one_account_balance(self):
        """The real live Plus account shape.

        The plan is usage-window based: the subscription grant is zero, the
        fresh account snapshot carries four gauges, and the only money is the
        purchased top-up. The card must show the exact allowance names and one
        prominent Account balance (the portal's total), with no separate
        subscription or top-up component lines.
        """
        gauges = (
            types.SimpleNamespace(
                id="day", scope="all", label="Today", used_bp=0,
                resets_at_ms=1794056810060,
            ),
            types.SimpleNamespace(
                id="week", scope="all", label="This week", used_bp=0,
                resets_at_ms=1794575210060,
            ),
            types.SimpleNamespace(
                id="month", scope="all", label="This month", used_bp=0,
                resets_at_ms=1796562047000,
            ),
            types.SimpleNamespace(
                id="top_tier", scope="top_tier", label="Large models (monthly)",
                used_bp=0, resets_at_ms=1796562047000,
            ),
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            account_tier=None,
            paid_service_access=True,
            subscription=types.SimpleNamespace(
                plan="Hermes Plus", tier=110, monthly_charge=20.0,
                monthly_credits=0.0, credits_remaining=0.0,
                rollover_credits=0.0,
                current_period_end="2026-12-06T13:00:47.000Z",
            ),
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=9.993238,
                total_usable_credits=9.993238,
                member_spend_usd=0.000483,
            ),
            usage_window=types.SimpleNamespace(
                gauges=gauges,
                featured_model_ids=(
                    "deepseek/deepseek-v4-flash-0731",
                    "z-ai/glm-5.3",
                    "stepfun/step-3.7-flash:free",
                ),
                auto_top_up_enabled=False,
                paused_reason=None,
                keep_going_on_credits=False,
            ),
            tool_access=types.SimpleNamespace(
                enabled=False,
                coverage={"firecrawl": True, "browser-use": True},
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual("Hermes Plus", res.plan)
        self.assertEqual("active", res.access_state)
        self.assertEqual(
            ["Daily", "Weekly", "Monthly", "Large models (Monthly)"],
            [w.label for w in res.windows],
        )
        self.assertEqual([0.0, 0.0, 0.0, 0.0], [w.used_percent for w in res.windows])
        self.assertTrue(all(w.reset_at and w.reset_at.endswith("+00:00")
                            for w in res.windows))
        self.assertEqual(
            [("USD", "9.99", "Account balance")],
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
        )
        joined = "\n".join(res.details)
        # One Account balance row is the money; no separate component lines.
        self.assertNotIn("Subscription credits", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertIn("Cycle ends: 2026-12-06T13:00:47.000Z", joined)
        self.assertIn("Spend this period: $0.00 (no cap reported)", joined)
        self.assertNotIn("Tool pool", joined)
        self.assertNotIn("Rate limits", joined)
        # No live JWT claim is available to this snapshot, so the exact
        # "Hermes Plus" label maps to the docs' bare Plus ceiling.
        self.assertIn("Rate ceiling: 400 RPM / 4M TPM (published)", joined)
        self.assertTrue(res.api_calls_available)

    def _plus_usage_account(self, *, used_bp=0, paused_reason=None, keep_going=None,
                            purchased=0.0, total=0.0):
        """The real core usage-window shape with the fields the CLI reads."""
        gauges = (
            types.SimpleNamespace(
                id="day", scope="all", label="Today", used_bp=used_bp,
                resets_at_ms=1794056810060,
            ),
            types.SimpleNamespace(
                id="week", scope="all", label="This week", used_bp=0,
                resets_at_ms=1794575210060,
            ),
            types.SimpleNamespace(
                id="top_tier", scope="top_tier", label="Large models (monthly)",
                used_bp=0, resets_at_ms=1796562047000,
            ),
        )
        return _nous_account(
            source="account_api",
            fresh=True,
            account_tier=None,
            paid_service_access=True,
            subscription=types.SimpleNamespace(
                plan="Hermes Plus", tier=110, monthly_charge=20.0,
                monthly_credits=0.0, credits_remaining=0.0,
                rollover_credits=0.0,
                current_period_end="2026-12-06T13:00:47.000Z",
            ),
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=purchased,
                total_usable_credits=total,
            ),
            usage_window=types.SimpleNamespace(
                gauges=gauges,
                featured_model_ids=(),
                auto_top_up_enabled=False,
                paused_reason=paused_reason,
                keep_going_on_credits=keep_going,
            ),
        )

    def test_paused_usage_window_is_paused_with_no_included_windows(self):
        """A withheld usage-window plan is paused, not active or unavailable.

        Core exposes the pause in ``usage_window.paused_reason``; the CLI used
        to read only the unrelated member-spend-cap flag, so a paid paused plan
        was classified active and its stale gauges were ranked.
        """
        acct = self._plus_usage_account(
            paused_reason="payment_past_due", keep_going=False,
            purchased=9.99, total=9.99,
        )
        res = self._fetch_with(acct)
        self.assertEqual("paused", res.access_state)
        self.assertEqual("Hermes Plus", res.plan)
        self.assertEqual([], res.windows)
        self.assertIs(False, res.api_calls_available)
        self.assertIn(
            "Status: included usage paused - subscription payment failed",
            res.details,
        )

    def test_paused_usage_window_with_paid_flag_false_is_not_unavailable(self):
        """The portal can drop `paid_service_access` while the plan is paused.

        That aggregate denial — even the lapsed-subscription reason — must not
        hide the paused plan or its wallet; only the usage-window pause fact
        decides this state. A stale/failed lookup is converted to `unknown`
        separately.
        """
        for reason in ("no_usable_credits", "subscription_expired"):
            with self.subTest(reason=reason):
                acct = self._plus_usage_account(
                    paused_reason="payment_past_due", keep_going=False,
                    purchased=9.99, total=9.99,
                )
                acct.paid_service_access = False
                acct.paid_service_access_info.reason = reason
                res = self._fetch_with(acct)
                self.assertEqual("paused", res.access_state)
                self.assertEqual("Hermes Plus", res.plan)
                self.assertEqual([], res.windows)
                self.assertEqual(
                    [("USD", "9.99", "Account balance")],
                    [(b.currency, b.total_balance, b.label)
                     for b in res.account_balances],
                )

    def test_paused_usage_window_keeps_calls_on_proven_purchased_credits(self):
        acct = self._plus_usage_account(
            paused_reason="payment_past_due", keep_going=True,
            purchased=9.99, total=9.99,
        )
        res = self._fetch_with(acct)
        self.assertEqual("paused", res.access_state)
        self.assertEqual([], res.windows)
        self.assertIs(True, res.api_calls_available)

    def test_paused_usage_window_without_a_continuation_answer_stays_unknown(self):
        acct = self._plus_usage_account(
            paused_reason="payment_past_due", keep_going=None,
            purchased=9.99, total=9.99,
        )
        res = self._fetch_with(acct)
        self.assertEqual("paused", res.access_state)
        self.assertIsNone(res.api_calls_available)

    def test_paused_continuation_ignores_the_withheld_subscription_total(self):
        """The portal's total still counts the allowance a pause withholds.

        Only the purchased wallet is spendable while paused. A zero wallet is
        proof that calls stop even when a subscription component and the total
        both stay positive, and that withheld $18 is not an Account balance.
        """
        acct = self._plus_usage_account(
            paused_reason="payment_past_due", keep_going=True,
            purchased=0.0, total=18.0,
        )
        acct.paid_service_access_info.subscription_credits_remaining = 18.0
        res = self._fetch_with(acct)
        self.assertEqual("paused", res.access_state)
        self.assertIs(False, res.api_calls_available)
        self.assertEqual(
            [("USD", "0.00", "Account balance")],
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
        )

    def test_paused_balance_is_the_purchased_wallet_not_the_total(self):
        acct = self._plus_usage_account(
            paused_reason="payment_past_due", keep_going=True,
            purchased=4.0, total=22.0,
        )
        acct.paid_service_access_info.subscription_credits_remaining = 18.0
        res = self._fetch_with(acct)
        self.assertIs(True, res.api_calls_available)
        self.assertEqual(
            [("USD", "4.00", "Account balance")],
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
        )

    def test_paused_continuation_without_a_proven_credit_balance_stays_unknown(self):
        acct = _nous_account(
            source="account_api",
            fresh=True,
            account_tier=None,
            paid_service_access=True,
            subscription=types.SimpleNamespace(
                plan="Hermes Plus", tier=110, monthly_charge=20.0,
                monthly_credits=0.0, credits_remaining=0.0,
                rollover_credits=0.0,
                current_period_end="2026-12-06T13:00:47.000Z",
            ),
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                purchased_credits_remaining=None,
                total_usable_credits=None,
                subscription_credits_remaining=None,
            ),
            usage_window=types.SimpleNamespace(
                gauges=(),
                featured_model_ids=(),
                auto_top_up_enabled=False,
                paused_reason="payment_past_due",
                keep_going_on_credits=True,
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual("paused", res.access_state)
        self.assertIsNone(res.api_calls_available)

    def test_full_all_scope_gauge_with_keep_going_off_reports_calls_unavailable(self):
        """A paid flag cannot override an exhausted included allowance."""
        acct = self._plus_usage_account(used_bp=10_000, keep_going=False)
        res = self._fetch_with(acct)
        self.assertEqual("active", res.access_state)
        self.assertEqual(["Daily", "Weekly", "Large models (Monthly)"],
                         [w.label for w in res.windows])
        self.assertIs(False, res.api_calls_available)

    def test_full_gauge_with_keep_going_on_and_a_funded_wallet_stays_available(self):
        acct = self._plus_usage_account(
            used_bp=10_000, keep_going=True, purchased=2.0, total=2.0,
        )
        res = self._fetch_with(acct)
        self.assertIs(True, res.api_calls_available)

    def test_full_gauge_continuation_needs_a_positive_purchased_wallet(self):
        """A total that includes the spent included credits proves nothing.

        After the included allowance is exhausted, only the purchased wallet
        can pay. The portal total can still be positive from the included
        component, so it must not be read as continuation.
        """
        acct = self._plus_usage_account(
            used_bp=10_000, keep_going=True, purchased=0.0, total=18.0,
        )
        acct.paid_service_access_info.subscription_credits_remaining = 18.0
        res = self._fetch_with(acct)
        self.assertEqual("active", res.access_state)
        self.assertIs(False, res.api_calls_available)

    def test_full_gauge_continuation_without_a_wallet_stays_unknown(self):
        acct = self._plus_usage_account(
            used_bp=10_000, keep_going=True, purchased=1.0, total=18.0,
        )
        acct.paid_service_access_info.purchased_credits_remaining = None
        acct.paid_service_access_info.subscription_credits_remaining = 18.0
        res = self._fetch_with(acct)
        self.assertEqual("active", res.access_state)
        self.assertIsNone(res.api_calls_available)

    def test_partial_all_scope_gauge_keeps_calls_available(self):
        acct = self._plus_usage_account(used_bp=5_000, keep_going=False)
        res = self._fetch_with(acct)
        self.assertIs(True, res.api_calls_available)

    def test_top_tier_only_gauge_does_not_claim_general_availability(self):
        acct = self._plus_usage_account(keep_going=False)
        acct.usage_window.gauges = (
            types.SimpleNamespace(
                id="top_tier", scope="top_tier", label="Large models (monthly)",
                used_bp=0, resets_at_ms=1796562047000,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.api_calls_available)

    def test_real_core_dataclasses_preserve_paid_plus_and_free_cards(self):
        """Exercise the actual core classes, not only attribute stand-ins.

        The small stand-ins are needed for CI (Hermes core is not installed
        there), but this test runs wherever the plugin is loaded by Hermes and
        catches a field rename/type change in the real dataclasses.
        """
        try:
            from agent.usage_gauges import WindowGauge
            from hermes_cli.nous_account import (
                NousPaidServiceAccessInfo,
                NousPortalAccountInfo,
                NousPortalSubscriptionInfo,
                NousUsageWindowInfo,
            )
        except Exception as exc:  # pragma: no cover - core is optional in plugin CI
            self.skipTest(f"Hermes core dataclasses unavailable: {exc}")

        paid = NousPortalAccountInfo(
            logged_in=True,
            source="account_api",
            fresh=True,
            org_id="synthetic-org",
            org_slug="synthetic-org",
            account_tier=None,
            subscription=NousPortalSubscriptionInfo(
                plan="Hermes Plus",
                tier=110,
                monthly_charge=20.0,
                monthly_credits=0.0,
                credits_remaining=0.0,
                rollover_credits=0.0,
                current_period_end="2026-12-06T13:00:47.000Z",
            ),
            paid_service_access=True,
            paid_service_access_info=NousPaidServiceAccessInfo(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=9.99,
                total_usable_credits=9.99,
            ),
            usage_window=NousUsageWindowInfo(
                gauges=(
                    WindowGauge("day", "all", "Today", 0, 1794056810060),
                    WindowGauge("week", "all", "This week", 0, 1794575210060),
                    WindowGauge("top_tier", "top_tier", "Large models (monthly)",
                                0, 1796562047000),
                ),
                featured_model_ids=(),
                auto_top_up_enabled=False,
                paused_reason=None,
                keep_going_on_credits=False,
            ),
        )
        paid_result = self._fetch_with(paid)
        self.assertEqual("Hermes Plus", paid_result.plan)
        self.assertEqual("active", paid_result.access_state)
        self.assertEqual(["Daily", "Weekly", "Large models (Monthly)"],
                         [w.label for w in paid_result.windows])
        self.assertIs(True, paid_result.api_calls_available)
        self.assertIn("Cycle ends: 2026-12-06T13:00:47.000Z", paid_result.details)

        free = NousPortalAccountInfo(
            logged_in=True,
            source="account_api",
            fresh=True,
            org_id="synthetic-org",
            org_slug="synthetic-org",
            account_tier="free",
            subscription=NousPortalSubscriptionInfo(
                plan="Free",
                tier=0,
                monthly_charge=0.0,
                monthly_credits=0.0,
                credits_remaining=0.0,
                rollover_credits=0.0,
                current_period_end="2027-01-01",
            ),
            paid_service_access=False,
            paid_service_access_info=NousPaidServiceAccessInfo(
                allowed=False,
                paid_access=False,
                reason="no_usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=False,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
        )
        free_result = self._fetch_with(free)
        self.assertEqual("Free", free_result.plan)
        self.assertEqual("free", free_result.access_state)
        self.assertEqual([], free_result.windows)
        joined = "\n".join(free_result.details)
        self.assertIn("Free tier", joined)
        self.assertNotIn("Cycle ends:", joined)
        self.assertIsNone(free_result.api_calls_available)

    def test_usage_window_positive_credits_are_only_the_account_balance(self):
        """A positive included credit is part of the total, not a second line."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            subscription=types.SimpleNamespace(
                plan="Hermes Plus", tier=110, monthly_charge=20.0,
                monthly_credits=0.0, credits_remaining=0.0,
                rollover_credits=0.0,
                current_period_end="2026-12-06T13:00:47.000Z",
            ),
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_credits_remaining=5.0,
                purchased_credits_remaining=9.993238,
                total_usable_credits=14.993238,
            ),
            usage_window=types.SimpleNamespace(
                gauges=(
                    types.SimpleNamespace(
                        id="day", scope="all", label="Today", used_bp=0,
                        resets_at_ms=1794056810060,
                    ),
                ),
                featured_model_ids=(),
                auto_top_up_enabled=False,
                paused_reason=None,
                keep_going_on_credits=False,
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(["Daily"], [w.label for w in res.windows])
        joined = "\n".join(res.details)
        self.assertNotIn("Subscription credits", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertEqual(
            [("USD", "14.99", "Account balance")],
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
        )

    def test_tool_pool_requires_the_live_entitlement_switch(self):
        """Coverage entries alone must not advertise a gated-off tool pool."""
        acct = _nous_account(
            account_tier="free",
            tool_access=types.SimpleNamespace(
                enabled=False,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertNotIn("Tool pool", "\n".join(res.details))
        self.assertNotIn("tool_pool", res.detail_facts)

    def test_free_pool_survives_fresh_account_omitting_tool_access(self):
        """The account API may omit tool_access; the same-account JWT has it.

        This is the live shape that lost the line: a fresh Free snapshot with
        no tool-access field replaced a cached JWT snapshot whose enabled pool
        was the only source of the entitlement.
        """
        cached = _nous_account(
            account_tier="free",
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        fresh = _nous_fresh_free()
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIn("Tool pool: browser-use, firecrawl", res.details)
        self.assertEqual(
            res.detail_facts.get("tool_pool"), "Tool pool: browser-use, firecrawl",
        )
        self.assertEqual(self.lookup_calls, [False, True])

    def test_free_topup_jwt_claims_and_pool_survive_fresh_account(self):
        """A free-plan JWT can carry paid_access true because of top-up credit.

        Core's JWT path has no purchased-credit fields, so the old free/paid
        parity check read this as a plan mismatch and dropped both the pool and
        the plan's rate-limit claims. The explicit Free plan identity proves the
        same plan, so both are safe to merge for the same account.
        """
        cached = _nous_account(
            account_tier="free",
            paid_service_access=True,
            raw_claims={
                "paid_access": True,
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
                "member_spend_usd": 1.0,
            },
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        fresh = _nous_fresh_free(
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=False,
                active_subscription_is_paid=False,
                purchased_credits_remaining=10.0,
                total_usable_credits=10.0,
            ),
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIn("Tool pool: browser-use, firecrawl", res.details)
        self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", res.details)
        self.assertEqual(
            res.detail_facts.get("rate_limits"),
            "Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
        )

    def test_live_free_topup_jwt_without_account_tier_keeps_rate_limits(self):
        """The real JWT has no plan name or account tier, only tier 5.

        Core's JWT path also cannot populate the purchased-credit fields, so
        ``paid_access: true`` is the only free/paid signal it carries. The
        fresh account API proves Free (a Free subscription with purchased
        credits), and the matching tier 5 proves the same plan; the JWT's rate
        limits must still reach the card.
        """
        cached = _nous_account(
            account_tier=None,
            paid_service_access=True,
            raw_claims={
                "paid_access": True,
                "subscription_tier": 5,
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
            },
            tool_access=types.SimpleNamespace(
                enabled=False,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        fresh = _nous_fresh_free(
            account_tier=None,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=False,
                subscription_tier=5,
                purchased_credits_remaining=10.0,
                total_usable_credits=10.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Free", tier=5, monthly_charge=0.0, monthly_credits=0.0,
                credits_remaining=0.0, rollover_credits=0.0,
                current_period_end="2026-12-01",
            ),
            tool_access=types.SimpleNamespace(
                enabled=False,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertEqual(
            res.detail_facts.get("rate_limits"),
            "Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
        )
        self.assertNotIn("tool_pool", res.detail_facts)
        self.assertNotIn("Tool pool", "\n".join(res.details))

    def test_live_free_topup_jwt_without_account_tier_can_still_supply_the_pool(self):
        """The same real JWT shape must not lose an enabled pool either."""
        cached = _nous_account(
            account_tier=None,
            paid_service_access=True,
            raw_claims={"paid_access": True, "subscription_tier": 5},
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        fresh = _nous_fresh_free(
            account_tier=None,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=False,
                subscription_tier=5,
                purchased_credits_remaining=10.0,
                total_usable_credits=10.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Free", tier=5, monthly_charge=0.0, monthly_credits=0.0,
                credits_remaining=0.0, rollover_credits=0.0,
                current_period_end="2026-12-01",
            ),
            tool_access=None,
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.detail_facts.get("tool_pool"),
                         "Tool pool: browser-use, firecrawl")

    def test_planless_paid_tier_jwt_still_does_not_match_a_fresh_free_plan(self):
        """The ambiguous shape is accepted only when the tier also matches."""
        cached = _nous_account(
            account_tier=None,
            paid_service_access=True,
            raw_claims={
                "paid_access": True,
                "subscription_tier": 2,
                "rate_limit_rpm": 400,
                "rate_limit_tpm": 4_000_000,
            },
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True},
            ),
        )
        fresh = _nous_fresh_free(
            account_tier=None,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=False,
                subscription_tier=5,
                purchased_credits_remaining=10.0,
                total_usable_credits=10.0,
            ),
            subscription=types.SimpleNamespace(plan="Free", tier=5),
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertNotIn("rate_limits", res.detail_facts)
        self.assertNotIn("Rate limits:", "\n".join(res.details))
        self.assertNotIn("tool_pool", res.detail_facts)

    def test_fresh_explicitly_disabled_pool_is_not_revived_from_the_jwt(self):
        """An explicit fresh ``enabled: false`` outranks a cached enabled pool."""
        cached = _nous_account(
            account_tier="free",
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        fresh = _nous_fresh_free(
            tool_access=types.SimpleNamespace(
                enabled=False,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertNotIn("Tool pool", "\n".join(res.details))
        self.assertNotIn("tool_pool", res.detail_facts)

    def test_cached_free_pool_never_crosses_accounts(self):
        """A different user/org keeps the cached JWT's pool off this row."""
        cached = _nous_account(
            user_id="synthetic-other-user",
            account_tier="free",
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True, "browser_use": True},
            ),
        )
        fresh = _nous_fresh_free()
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertNotIn("Tool pool", "\n".join(res.details))
        self.assertNotIn("tool_pool", res.detail_facts)

    def test_cached_jwt_raw_tool_access_claim_is_inherited(self):
        """An older/synthetic JWT with only the raw claim still supplies it."""
        cached = _nous_account(
            account_tier="free",
            tool_access=None,
            raw_claims={
                "tool_access": {
                    "enabled": True,
                    "coverage": {"firecrawl": True, "browser_use": True},
                },
            },
        )
        fresh = _nous_fresh_free()
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertIn("Tool pool: browser-use, firecrawl", res.details)
        self.assertEqual(
            res.detail_facts.get("tool_pool"), "Tool pool: browser-use, firecrawl",
        )

    def test_free_pool_needs_only_a_proven_account_not_a_plan_label(self):
        """A Free JWT's pool and rate limits survive a missing fresh plan label.

        The fresh account snapshot can prove Free through its subscription
        block while carrying no plan name or tier. The cached JWT explicitly
        names Free and the account is the same, so its Free-plan rate limits
        and tool pool remain that account's facts. Spend is not rendered on a
        Free card, so it stays absent without being a cross-account concern.
        """
        cached = _nous_account(
            account_tier="free",
            raw_claims={
                "rate_limit_rpm": 50,
                "member_spend_usd": 1.0,
            },
            tool_access=types.SimpleNamespace(
                enabled=True,
                coverage={"firecrawl": True},
            ),
        )
        fresh = _nous_fresh_free(account_tier=None)
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIn("Tool pool: firecrawl", res.details)
        self.assertIn("Rate limits: 50 RPM", res.details)
        joined = "\n".join(res.details)
        self.assertNotIn("Spend this period", joined)
        self.assertNotIn("spend", res.detail_facts)

    def test_free_jwt_tier_claim_is_not_a_paid_plan(self):
        """The live free JWT carries subscription_tier 5; it is not "Tier 5"."""
        acct = _nous_account(
            account_tier="free",
            raw_claims={
                "paid_access": False,
                "subscription_tier": 5,
                "member_spend_usd": 0.0,
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
            },
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.windows, [])
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Free tier - free models only", joined)
        self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", joined)
        self.assertNotIn("Tier 5", joined)
        self.assertNotIn("depleted", joined)

    def test_paid_fresh_snapshot_keeps_master_jwt_facts(self):
        """The account API wins for plan/status; the JWT still supplies limits."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="paid",
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason=None,
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=14.0, rollover_credits=3.0,
                current_period_end="2026-12-03",
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            subscription=types.SimpleNamespace(plan="Plus", tier=2),
            raw_claims={
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
                "member_spend_usd": 6.0,
                "member_spend_cap_usd": 200.0,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertIsNone(res.unavailable_reason)
        # Fresh account facts own the plan and percentage window.
        self.assertEqual(res.plan, "Plus")
        self.assertEqual(res.access_state, "active")
        self.assertEqual(len(res.windows), 1)
        joined = "\n".join(res.details)
        # The old master facts survive the fresh snapshot's missing raw_claims.
        self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", joined)
        self.assertIn("Spend this period: $6.00", joined)
        self.assertEqual(
            res.detail_facts.get("rate_limits"),
            "Rate limits: 50 RPM · 500k TPM · 2.1k RPH",
        )
        self.assertEqual(res.detail_facts.get("spend"), "Spend this period: $6.00")
        self.assertEqual(self.lookup_calls, [False, True])

    def test_paid_plan_change_drops_stale_jwt_rate_and_spend(self):
        """A cached JWT from an older paid tier is not the current plan's facts.

        Rate limits and period spend are plan-specific. Both snapshots being
        paid (and even naming the same account) is not enough: without a
        provably identical plan name/tier the cached claims must be dropped.
        """
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=14.0, rollover_credits=0.0,
                current_period_end="2026-12-03",
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            subscription=types.SimpleNamespace(plan="Pro", tier=3),
            raw_claims={
                "rate_limit_rpm": 400,
                "rate_limit_tpm": 4_000_000,
                "member_spend_usd": 21.77,
                "member_spend_cap_usd": 200.0,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Plus")
        self.assertEqual(res.access_state, "active")
        joined = "\n".join(res.details)
        self.assertNotIn("Rate limits:", joined)
        self.assertNotIn("Spend this period", joined)
        self.assertNotIn("rate_limits", res.detail_facts)
        self.assertNotIn("spend", res.detail_facts)

    def test_cached_claims_require_a_provable_same_plan(self):
        """A paid JWT that cannot name its plan is not merged into a paid card."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(plan="Plus", tier=2),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            raw_claims={"rate_limit_rpm": 50, "member_spend_usd": 6.0},
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Plus")
        joined = "\n".join(res.details)
        self.assertNotIn("Rate limits:", joined)
        self.assertNotIn("Spend this period", joined)

    def test_free_fresh_snapshot_keeps_master_rate_facts(self):
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=False,
            paid_service_access_info=_nous_access(),
        )
        cached = _nous_account(
            account_tier="free",
            raw_claims={
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        joined = "\n".join(res.details)
        self.assertIn("Rate limits: 50 RPM · 500k TPM · 2.1k RPH", joined)
        self.assertNotIn("Tier 5", joined)
        self.assertEqual(res.detail_facts.get("rate_limits"),
                         "Rate limits: 50 RPM · 500k TPM · 2.1k RPH")

    def test_master_lookup_failure_keeps_billing_fresh_card_without_claims(self):
        """The account API can succeed independently of the local JWT read."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=False,
            paid_service_access_info=_nous_access(),
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=RuntimeError("jwt unavailable"))
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        joined = "\n".join(res.details)
        self.assertIn("Rate ceiling: 50 RPM / 500k TPM (published)", joined)
        self.assertNotIn("Rate limits:", joined)
        self.assertNotIn("rate_limits", res.detail_facts)
        self.assertEqual(self.lookup_calls, [False, True])

    def _fetch_paid_plan_without_claims(self, plan, *, tier=2):
        """A fresh paid plan snapshot whose JWT carries no live rate claims."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="paid",
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason=None,
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=tier,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(
                plan=plan, tier=tier, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=14.0, rollover_credits=0.0,
                current_period_end="2026-12-03",
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            subscription=types.SimpleNamespace(plan=plan, tier=tier),
        )
        return self._fetch_with(fresh, fresh=fresh, cached=cached)

    def test_published_rate_ceiling_for_every_documented_paid_tier(self):
        """Each official API-docs tier gets its own published ceiling."""
        expected = {
            "Basic": "Rate ceiling: 400 RPM / 2M TPM (published)",
            "Plus": "Rate ceiling: 400 RPM / 4M TPM (published)",
            "Hermes Plus": "Rate ceiling: 400 RPM / 4M TPM (published)",
            "Super": "Rate ceiling: 800 RPM / 8M TPM (published)",
            "Scale": "Rate ceiling: 600 RPM / 6M TPM (published)",
            "Ultra": "Rate ceiling: 1.6k RPM / 16M TPM (published)",
            "Default paid users": "Rate ceiling: 180 RPM / 720k TPM (published)",
        }
        for plan, line in expected.items():
            with self.subTest(plan=plan):
                res = self._fetch_paid_plan_without_claims(plan)
                self.assertEqual(res.plan, plan)
                self.assertEqual(res.access_state, "active")
                self.assertIn(line, res.details)
                self.assertEqual(res.detail_facts.get("rate_ceiling"), line)
                self.assertNotIn("rate_limits", res.detail_facts)

    def test_unknown_or_numeric_only_paid_tier_gets_no_published_ceiling(self):
        """No hypothetical normalization from a bare tier or an unknown label."""
        for plan in ("Pro", None):
            with self.subTest(plan=plan):
                res = self._fetch_paid_plan_without_claims(plan)
                self.assertEqual(res.access_state, "active")
                joined = "\n".join(res.details)
                self.assertNotIn("Rate ceiling", joined)
                self.assertNotIn("rate_ceiling", res.detail_facts)

    def test_live_rate_claims_win_over_published_tier_and_keep_rph(self):
        """Actual JWT claims are used as-is; RPH is never dropped for a fallback."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="paid",
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason=None,
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=14.0, rollover_credits=0.0,
                current_period_end="2026-12-03",
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            subscription=types.SimpleNamespace(plan="Plus", tier=2),
            raw_claims={
                "rate_limit_rpm": 51,
                "rate_limit_tpm": 501_000,
                "rate_limit_rph": 2_100,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Plus")
        self.assertEqual(
            res.detail_facts.get("rate_limits"),
            "Rate limits: 51 RPM · 501k TPM · 2.1k RPH",
        )
        self.assertNotIn("rate_ceiling", res.detail_facts)
        self.assertNotIn("Rate ceiling", "\n".join(res.details))

    def test_partial_or_malformed_live_claim_never_mixes_in_published_values(self):
        """A present claim is authoritative; a broken one is not replaced."""
        for claim, expected in (
            ({"rate_limit_rpm": 399}, "Rate limits: 399 RPM"),
            ({"rate_limit_rpm": "not-a-number"}, None),
        ):
            with self.subTest(claim=claim):
                fresh = _nous_account(
                    source="account_api",
                    fresh=True,
                    account_tier="paid",
                    paid_service_access=True,
                    paid_service_access_info=_nous_access(
                        allowed=True,
                        paid_access=True,
                        reason=None,
                        has_active_subscription=True,
                        active_subscription_is_paid=True,
                        subscription_tier=2,
                        subscription_monthly_charge=20.0,
                        subscription_credits_remaining=14.0,
                        purchased_credits_remaining=12.0,
                        total_usable_credits=26.0,
                    ),
                    subscription=types.SimpleNamespace(
                        plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                        credits_remaining=14.0, rollover_credits=0.0,
                        current_period_end="2026-12-03",
                    ),
                )
                cached = _nous_account(
                    account_tier="paid",
                    paid_service_access=True,
                    subscription=types.SimpleNamespace(plan="Plus", tier=2),
                    raw_claims=claim,
                )
                res = self._fetch_with(fresh, fresh=fresh, cached=cached)
                joined = "\n".join(res.details)
                self.assertNotIn("Rate ceiling", joined)
                self.assertNotIn("rate_ceiling", res.detail_facts)
                if expected is None:
                    self.assertNotIn("Rate limits", joined)
                    self.assertNotIn("rate_limits", res.detail_facts)
                else:
                    self.assertIn(expected, joined)

    def test_stale_paid_snapshot_gets_no_published_ceiling(self):
        """A stale cached plan is not a current tier identity."""
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            subscription=types.SimpleNamespace(plan="Plus", tier=2),
        )
        res = self._fetch_with(cached, fresh=RuntimeError("fresh lookup failed"), cached=cached)
        self.assertEqual(res.access_state, "unknown")
        self.assertNotIn("Rate ceiling", "\n".join(res.details))
        self.assertNotIn("rate_ceiling", res.detail_facts)

    def test_nous_result_carries_opaque_account_org_for_safe_merge(self):
        acct = _nous_account(org_slug="synthetic-org")
        res = self._fetch_with(acct)
        # Fixed FNV-1a 64 vector, so the test cannot silently agree with a
        # broken implementation that hashes the wrong bytes.
        self.assertEqual(res.account_org_key, "10afe58765c15923")
        self.assertNotIn("synthetic-org", "\n".join(res.details))
        self.assertIsNotNone(res.account_org_key)
        self.assertNotIn("synthetic-org", res.account_org_key or "")

    def test_published_rate_ceiling_allows_a_low_balance_unknown_exact_plan(self):
        """An exact paid plan keeps its documented ceiling while access is unknown."""
        from quota_providers import builtin

        sub = types.SimpleNamespace(plan="Plus", tier=2)
        acct = _nous_account(subscription=sub, raw_claims={})
        self.assertEqual(
            builtin._nous_published_rate_limits(acct, "unknown", sub),
            ("Plus", 400, 4_000_000),
        )

    def test_published_rate_ceiling_maps_hermes_plus_and_never_crosses_free(self):
        """The account API's product label is the same exact Plus tier.

        A prefixed paid label never gives a Free reading a paid ceiling, and a
        Free label on a paid snapshot never inherits the Free ceiling.
        """
        from quota_providers import builtin

        plus = types.SimpleNamespace(plan="Hermes Plus", tier=110)
        acct = _nous_account(subscription=plus, raw_claims={})
        for state in ("active", "unknown", "depleted", "paused"):
            with self.subTest(state=state):
                self.assertEqual(
                    builtin._nous_published_rate_limits(acct, state, plus),
                    ("Plus", 400, 4_000_000),
                )
        self.assertIsNone(
            builtin._nous_published_rate_limits(acct, "free", plus)
        )
        free = types.SimpleNamespace(plan="Free", tier=5)
        self.assertIsNone(
            builtin._nous_published_rate_limits(acct, "active", free)
        )

    def test_master_claims_do_not_copy_identity_or_entitlement_fields(self):
        """Only the old card's display-safe rate/spend claims cross over."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=False,
            paid_service_access_info=_nous_access(),
        )
        cached = _nous_account(
            account_tier="free",
            raw_claims={
                "rate_limit_rpm": 50,
                "member_spend_usd": 0.0,
                "email": "private@example.invalid",
                "org_id": "secret-org",
                "subscription_tier": 5,
                "paid_access": True,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        blob = "\n".join(res.details) + repr(res.detail_facts)
        self.assertNotIn("private@example.invalid", blob)
        self.assertNotIn("secret-org", blob)
        self.assertNotIn("Tier 5", blob)
        self.assertNotIn("paid_access", blob)
        self.assertIn("Rate limits: 50 RPM", blob)
        self.assertEqual(
            set(res.detail_facts),
            {"free_tier", "rate_limits"},
        )

    def test_stale_paid_jwt_claims_do_not_decorate_a_fresh_free_snapshot(self):
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=False,
            paid_service_access_info=_nous_access(),
        )
        cached = _nous_account(
            source="jwt",
            account_tier="paid",
            paid_service_access=True,
            raw_claims={
                "paid_access": True,
                "rate_limit_rpm": 400,
                "rate_limit_tpm": 4_000_000,
                "rate_limit_rph": 16_800,
                "member_spend_usd": 21.77,
                "member_spend_cap_usd": 200.0,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        joined = "\n".join(res.details)
        self.assertIn("Free tier - free models only", joined)
        self.assertIn("Rate ceiling: 50 RPM / 500k TPM (published)", joined)
        self.assertNotIn("Rate limits:", joined)
        self.assertNotIn("Spend this period", joined)
        self.assertNotIn("Tier 5", joined)

    def test_stale_paid_claims_do_not_decorate_a_free_snapshot_without_a_tier(self):
        """A free snapshot proven only by ``has_active_subscription: false``.

        Core's freest account shape has no ``account_tier`` and no subscription
        object; only the access block says there is no active subscription. A
        stale paid JWT's rate/spend claims must not cross that free/paid
        boundary.
        """
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier=None,
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                has_active_subscription=False,
                active_subscription_is_paid=False,
            ),
        )
        cached = _nous_account(
            source="jwt",
            account_tier="paid",
            paid_service_access=True,
            raw_claims={
                "paid_access": True,
                "rate_limit_rpm": 400,
                "rate_limit_tpm": 4_000_000,
                "rate_limit_rph": 16_800,
                "member_spend_usd": 21.77,
                "member_spend_cap_usd": 200.0,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        joined = "\n".join(res.details)
        self.assertIn("Free tier - free models only", joined)
        self.assertNotIn("Rate limits:", joined)
        self.assertNotIn("Spend this period", joined)
        self.assertNotIn("Tier 5", joined)

    def test_free_topup_without_plan_name_is_not_active_and_has_no_renewal(self):
        """An explicit no-subscription Free plan with top-up stays Free.

        Core's aggregate ``paid_service_access`` is true when purchased credits
        fund calls, so a plan-less Free snapshot used to be classified active
        and its padded renewal date was rendered. The account API's explicit
        ``has_active_subscription: false`` is the authoritative answer.
        """
        free_sub = types.SimpleNamespace(
            plan=None, tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end="2027-01-01",
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=False,
                active_subscription_is_paid=False,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=10.0,
                total_usable_credits=10.0,
            ),
            subscription=free_sub,
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIs(res.api_calls_available, True)
        self.assertEqual(res.windows, [])
        self.assertEqual(
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
            [("USD", "10.00", "Account balance")],
        )
        joined = "\n".join(res.details)
        self.assertIn("Free tier - paid models via purchased credits", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertNotIn("Cycle ends:", joined)
        self.assertNotIn("Subscription credits", joined)

    def test_cached_claims_require_the_same_user(self):
        """A different user in the same org must never receive cached claims."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            user_id="synthetic-other-user",
            account_tier="paid",
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason=None,
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=14.0, rollover_credits=3.0,
                current_period_end="2026-12-03",
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            raw_claims={
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "member_spend_usd": 6.0,
                "member_spend_cap_usd": 200.0,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Plus")
        self.assertEqual(res.access_state, "active")
        joined = "\n".join(res.details)
        self.assertNotIn("Rate limits:", joined)
        self.assertNotIn("Spend this period", joined)
        self.assertNotIn("rate_limits", res.detail_facts)
        self.assertNotIn("spend", res.detail_facts)

    def test_cached_claims_require_the_same_org_and_portal(self):
        """A shared user label is not enough without the same org and portal."""
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            raw_claims={"rate_limit_rpm": 50},
        )
        for changed in (
            {"org_id": "synthetic-other-org"},
            {"portal_base_url": "https://other-portal.invalid"},
        ):
            with self.subTest(changed=changed):
                fresh = _nous_account(
                    source="account_api",
                    fresh=True,
                    paid_service_access=True,
                    paid_service_access_info=_nous_access(
                        allowed=True,
                        paid_access=True,
                        reason="usable_credits",
                        has_active_subscription=True,
                        active_subscription_is_paid=True,
                    ),
                    **changed,
                )
                res = self._fetch_with(fresh, fresh=fresh, cached=cached)
                self.assertNotIn("Rate limits:", "\n".join(res.details))

    def test_cached_claims_are_refused_when_identity_is_not_proven(self):
        """Missing identity is fail-closed, not an implicit match."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            user_id=None,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            raw_claims={"rate_limit_rpm": 50},
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertNotIn("Rate limits:", "\n".join(res.details))

    def test_exhausted_paid_snapshot_keeps_its_paid_jwt_rate_claims(self):
        """A denied paid account is not a Free account; keep its rate facts."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="paid",
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                reason="no_usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=0.0, rollover_credits=0.0,
                current_period_end="2026-12-03",
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            subscription=types.SimpleNamespace(plan="Plus", tier=2),
            raw_claims={
                "rate_limit_rpm": 400,
                "rate_limit_tpm": 4_000_000,
                "rate_limit_rph": 16_800,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.plan, "Plus")
        self.assertEqual(res.access_state, "depleted")
        self.assertIn("Rate limits: 400 RPM · 4M TPM · 16.8k RPH",
                      "\n".join(res.details))

    def test_free_account_api_snapshot_is_not_depleted(self):
        """A fresh account API that explicitly has no subscription is free."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access_info=_nous_access(),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        self.assertIsNone(res.api_calls_available)
        self.assertEqual(res.access_state, "free")
        joined = "\n".join(res.details)
        self.assertIn("Free tier", joined)
        self.assertNotIn("depleted", joined)
        self.assertNotIn("unknown", joined)

    def test_free_snapshot_suppresses_zero_credits_and_renewal(self):
        """A free plan's padded zero balances are not display facts."""
        free_sub = types.SimpleNamespace(
            plan="Free", tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end="2027-01-01",
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            subscription=free_sub,
            raw_claims={"member_spend_usd": 0.0},
            paid_service_access_info=_nous_access(),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertEqual(res.windows, [])
        # Zero is a known total, distinct from an absent field: it stays on the
        # card without turning the plan into a paid subscription.
        self.assertEqual(
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
            [("USD", "0.00", "Account balance")],
        )
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Free tier - free models only", joined)
        for noise in ("Subscription credits", "Top-up credits", "Account balance",
                      "Cycle ends:", "Spend this period:", "$0.00"):
            self.assertNotIn(noise, joined)

    def test_missing_total_with_zero_subscription_uses_the_wallet_as_total(self):
        """An absent total may use the wallet only when subscription credits are zero.

        This is the supported top-up-only shape: there is no included credit
        pool, so the purchased wallet is the whole Account balance. The card
        still renders one balance row, not a component line.
        """
        access = _nous_access(
            allowed=True,
            paid_access=True,
            reason="usable_credits",
            has_active_subscription=False,
            active_subscription_is_paid=False,
            subscription_credits_remaining=0.0,
            purchased_credits_remaining=10.0,
            total_usable_credits=None,
        )
        free_sub = types.SimpleNamespace(
            plan="Free", tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end="2027-01-01",
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=access,
            subscription=free_sub,
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIs(res.api_calls_available, True)
        self.assertEqual(
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
            [("USD", "10.00", "Account balance")],
        )
        joined = "\n".join(res.details)
        self.assertIn("Free tier - paid models via purchased credits", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertNotIn("Subscription credits", joined)

    def test_missing_total_with_unknown_subscription_is_not_summed(self):
        """No fabricated total: an unknown included component blocks the fallback."""
        access = _nous_access(
            allowed=True,
            paid_access=True,
            reason="usable_credits",
            has_active_subscription=False,
            active_subscription_is_paid=False,
            subscription_credits_remaining=None,
            purchased_credits_remaining=10.0,
            total_usable_credits=None,
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=access,
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.account_balances, [])
        self.assertNotIn("Account balance", "\n".join(res.details))

    def test_empty_subscription_object_does_not_block_free(self):
        """A ``subscription: {}`` payload must behave like no subscription."""
        empty_sub = types.SimpleNamespace(
            plan=None, tier=None, monthly_charge=None, monthly_credits=None,
            credits_remaining=None, rollover_credits=None, current_period_end=None,
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            subscription=empty_sub,
            paid_service_access_info=_nous_access(
                has_active_subscription=False,
                active_subscription_is_paid=False,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIsNone(res.api_calls_available)
        self.assertNotIn("unknown", "\n".join(res.details))

    def test_zeroed_subscription_object_does_not_block_free(self):
        """Every numeric field padded with 0 is still no subscription fact."""
        zeroed_sub = types.SimpleNamespace(
            plan=None, tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end=None,
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            subscription=zeroed_sub,
            paid_service_access_info=_nous_access(
                has_active_subscription=False,
                active_subscription_is_paid=False,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIsNone(res.api_calls_available)

    def test_free_plan_with_purchased_credits_stays_free(self):
        """Purchased credits make paid_service_access true but not a paid plan.

        A Free plan whose $10 top-up wallet funds calls reports
        ``paid_service_access: true``; that must not turn it into a paid
        subscription. The zeroed subscription object also carries a renewal
        date, which is not evidence of a paid plan and must not be rendered.
        """
        free_sub = types.SimpleNamespace(
            plan="Free", tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end="2027-01-01",
        )
        access = _nous_access(
            allowed=True,
            paid_access=True,
            reason="usable_credits",
            has_active_subscription=False,
            active_subscription_is_paid=False,
            subscription_tier=5,
            subscription_monthly_charge=0.0,
            subscription_credits_remaining=0.0,
            purchased_credits_remaining=10.0,
            total_usable_credits=10.0,
            member_spend_cap_exceeded=False,
            member_spend_usd=0.0,
            member_spend_cap_usd=0.0,
        )
        for overrides in (
            {"account_tier": "free", "subscription": free_sub},
            {"account_tier": None, "subscription": free_sub},
        ):
            with self.subTest(overrides=overrides):
                acct = _nous_account(
                    source="account_api",
                    fresh=True,
                    paid_service_access=True,
                    paid_service_access_info=access,
                    **overrides,
                )
                res = self._fetch_with(acct)
                self.assertEqual(res.access_state, "free")
                self.assertEqual(res.plan, "Free")
                self.assertEqual(res.windows, [])
                # Purchased credits make calls usable, but never make the plan
                # a subscription: the plan label stays Free, the wallet is a
                # prominent, explicitly labeled balance, and no renewal date
                # (the padded one on the free subscription object) is shown.
                self.assertIs(res.api_calls_available, True)
                self.assertEqual(
                    [(b.currency, b.total_balance, b.label) for b in res.account_balances],
                    [("USD", "10.00", "Account balance")],
                )
                joined = "\n".join(res.details)
                self.assertIn("Free tier - paid models via purchased credits", joined)
                self.assertNotIn("Top-up credits", joined)
                self.assertNotIn("Free tier - free models only", joined)
                for paid_noise in (
                    "Cycle ends:",
                    "Subscription credits",
                    "Account balance",
                    "Spend this period",
                    "Tier 5",
                ):
                    self.assertNotIn(paid_noise, joined)

    def test_free_topup_with_zero_total_does_not_claim_paid_models(self):
        """A zero authoritative total contradicts a usable purchased wallet."""
        free_sub = types.SimpleNamespace(
            plan="Free", tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end="2027-01-01",
        )
        access = _nous_access(
            allowed=True,
            paid_access=True,
            reason="usable_credits",
            has_active_subscription=False,
            active_subscription_is_paid=False,
            subscription_credits_remaining=0.0,
            purchased_credits_remaining=10.0,
            total_usable_credits=0.0,
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=True,
            paid_service_access_info=access,
            subscription=free_sub,
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Free tier - free models only", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertNotIn("paid models via purchased credits", joined)
        self.assertNotIn("Cycle ends:", joined)

    def test_free_funded_total_without_a_named_component_is_neutral(self):
        """A positive total with no purchased amount is not "free models only"."""
        free_sub = types.SimpleNamespace(
            plan="Free", tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end="2027-01-01",
        )
        acct = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                has_active_subscription=False,
                active_subscription_is_paid=False,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=None,
                total_usable_credits=5.0,
            ),
            subscription=free_sub,
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIs(res.api_calls_available, True)
        joined = "\n".join(res.details)
        self.assertIn("Free tier - credits available", joined)
        self.assertNotIn("free models only", joined)
        self.assertNotIn("purchased credits", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertNotIn("Cycle ends:", joined)

    def test_true_paid_plan_with_topup_credits_is_not_free(self):
        """A real paid subscription outranks a stale free tier claim."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=10.0,
                total_usable_credits=24.0,
                member_spend_cap_exceeded=False,
                member_spend_usd=5.0,
                member_spend_cap_usd=100.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=14.0, rollover_credits=3.0,
                current_period_end="2026-12-03",
            ),
        )
        res = self._fetch_with(fresh)
        self.assertEqual(res.access_state, "active")
        self.assertEqual(res.plan, "Plus")
        self.assertEqual(len(res.windows), 1)
        self.assertIs(res.api_calls_available, True)
        joined = "\n".join(res.details)
        self.assertIn("Cycle ends: 2026-12-03", joined)
        self.assertNotIn("Subscription credits", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertNotIn("Account balance", joined)
        self.assertEqual(
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
            [("USD", "24.00", "Account balance")],
        )
        self.assertIn("Spend this period: $5.00", joined)
        self.assertNotIn("Free tier", joined)

    def test_stale_paid_jwt_claims_do_not_decorate_free_plan_with_topup(self):
        """A Free plan with $10 purchased credit is still a free tier."""
        free_sub = types.SimpleNamespace(
            plan="Free", tier=0, monthly_charge=0.0, monthly_credits=0.0,
            credits_remaining=0.0, rollover_credits=0.0,
            current_period_end="2027-01-01",
        )
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=False,
                active_subscription_is_paid=False,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=10.0,
                total_usable_credits=10.0,
            ),
            subscription=free_sub,
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            raw_claims={
                "rate_limit_rpm": 400,
                "member_spend_usd": 21.77,
                "member_spend_cap_usd": 200.0,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        self.assertEqual(res.access_state, "free")
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.windows, [])
        self.assertIs(res.api_calls_available, True)
        self.assertEqual(
            [(b.currency, b.total_balance, b.label) for b in res.account_balances],
            [("USD", "10.00", "Account balance")],
        )
        joined = "\n".join(res.details)
        self.assertIn("Free tier - paid models via purchased credits", joined)
        self.assertNotIn("Top-up credits", joined)
        self.assertNotIn("Free tier - free models only", joined)
        self.assertNotIn("Cycle ends:", joined)
        self.assertNotIn("Rate limits:", joined)
        self.assertNotIn("Spend this period", joined)
        self.assertNotIn("Tier 5", joined)

    def test_subscription_tier_number_alone_is_not_paid_evidence(self):
        """A synthesized ``Tier N`` label must never prove a paid plan."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            subscription=types.SimpleNamespace(
                plan=None, tier=5, monthly_charge=None, monthly_credits=None,
                credits_remaining=None, rollover_credits=None, current_period_end=None,
            ),
            paid_service_access_info=_nous_access(
                reason="no_usable_credits",
                has_active_subscription=None,
                active_subscription_is_paid=None,
                subscription_tier=5,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertNotEqual(res.access_state, "depleted")
        joined = "\n".join(res.details)
        self.assertNotIn("depleted", joined)
        self.assertNotIn("Free tier", joined)
        self.assertNotIn("Tier 5", joined)

    def test_account_missing_reason_is_unavailable_not_depleted(self):
        """``account_missing`` must not become credit exhaustion."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                reason="account_missing",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=110.0,
                credits_remaining=0.0, rollover_credits=0.0,
                current_period_end="2026-12-01",
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.access_state, "unavailable")
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Paid access is unavailable", joined)
        self.assertNotIn("depleted", joined)
        self.assertNotIn("Free tier", joined)

    def test_subscription_expired_reason_is_unavailable_not_depleted(self):
        """A lapsed subscription is not the same state as exhausted credits."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                reason="subscription_expired",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=110.0,
                credits_remaining=0.0, rollover_credits=0.0,
                current_period_end="2026-12-01",
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.access_state, "unavailable")
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Paid access is unavailable", joined)
        self.assertNotIn("depleted", joined)

    def test_unavailable_snapshot_hides_stale_paid_windows_and_credits(self):
        """An unresolved/lapsed account has no current paid facts to show."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            raw_claims={
                "rate_limit_rpm": 400,
                "member_spend_usd": 21.77,
                "member_spend_cap_usd": 200.0,
            },
            paid_service_access_info=_nous_access(
                reason="subscription_expired",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=110.0,
                credits_remaining=88.0, rollover_credits=3.0,
                current_period_end="2026-12-01",
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.access_state, "unavailable")
        self.assertEqual(res.windows, [])
        self.assertIsNone(res.plan)
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Paid access is unavailable", joined)
        for stale in (
            "Subscription credits",
            "Top-up credits",
            "Account balance",
            "Rollover:",
            "Cycle ends:",
            "Spend this period",
            "Rate limits:",
            "Tier 2",
            "Plus",
        ):
            self.assertNotIn(stale, joined)
        self.assertEqual(set(res.detail_facts), {"status"})

    def test_unavailable_reason_outranks_stale_paid_and_pause_flags(self):
        """A missing account cannot be active/paused just because flags say so."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="account_missing",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
                member_spend_cap_exceeded=True,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=110.0,
                credits_remaining=88.0, rollover_credits=3.0,
                current_period_end="2026-12-01",
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.access_state, "unavailable")
        self.assertEqual(res.windows, [])
        self.assertIsNone(res.plan)
        joined = "\n".join(res.details)
        self.assertIn("Paid access is unavailable", joined)
        self.assertNotIn("paused", joined.lower())
        self.assertNotIn("Subscription credits", joined)

    def test_live_jwt_fallback_is_unknown_not_free(self):
        """The live JWT shortcut carries no subscription object; stay unknown."""
        acct = _nous_account(
            raw_claims={
                "paid_access": False,
                "subscription_tier": 5,
                "member_spend_usd": 0.0,
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
            },
            tool_access=types.SimpleNamespace(enabled=True, coverage={"firecrawl": True}),
        )
        res = self._fetch_with(acct, fresh=RuntimeError("portal unavailable"), cached=acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertEqual(res.access_state, "unknown")
        self.assertIsNone(res.api_calls_available)
        self.assertEqual(self.lookup_calls, [False, True])
        joined = "\n".join(res.details)
        self.assertIn("Paid access: unknown", joined)
        self.assertNotIn("Free tier", joined)
        self.assertNotIn("Tier 5", joined)
        self.assertNotIn("depleted", joined)

    def test_failed_fresh_lookup_marks_a_paid_cached_jwt_stale(self):
        """A cached paid JWT is history, not a current active entitlement.

        When the forced account-API read fails, the local JWT fallback may
        still carry a paid plan, renewal and rate/spend claims. Presenting any
        of those as current would claim a freshness the read did not prove.
        """
        cached = _nous_account(
            source="jwt",
            fresh=False,
            account_tier="paid",
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=20.0,
                credits_remaining=14.0, rollover_credits=3.0,
                current_period_end="2026-12-03",
            ),
            raw_claims={
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "member_spend_usd": 6.0,
                "member_spend_cap_usd": 200.0,
            },
        )
        res = self._fetch_with(
            _nous_account(),
            fresh=RuntimeError("portal unavailable"),
            cached=cached,
        )
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertEqual(res.access_state, "unknown")
        self.assertIsNone(res.api_calls_available)
        self.assertEqual(res.windows, [])
        self.assertEqual(res.account_balances, [])
        self.assertEqual(self.lookup_calls, [False, True])
        joined = "\n".join(res.details)
        self.assertIn("Paid access: unknown", joined)
        self.assertIn("stale", joined)
        for stale_fact in (
            "Cycle ends:", "Subscription credits", "Top-up credits",
            "Rollover", "Spend this period", "Rate limits:", "Account balance",
        ):
            self.assertNotIn(stale_fact, joined)

    def test_failed_fresh_and_cached_lookup_is_honest_unavailable(self):
        res = self._fetch_with(
            _nous_account(),
            fresh=RuntimeError("portal unavailable"),
            cached=RuntimeError("auth store unavailable"),
        )
        self.assertEqual(res.unavailable_reason, "fetcher-unavailable")
        self.assertEqual(res.details, [])
        self.assertIsNone(res.plan)
        self.assertIsNone(res.access_state)

    def test_bounded_lookup_honors_wall_clock(self):
        """The 20s sweep budget needs a hard wall clock, not only socket timeouts."""
        from quota_providers import builtin

        started = time.monotonic()
        value = builtin._bounded_account_lookup(
            lambda **kwargs: (time.sleep(0.4), "late")[1],
            force_fresh=True,
            timeout=0.05,
        )
        self.assertIs(value, builtin._NOUS_LOOKUP_TIMEOUT)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertLess(builtin._NOUS_FETCH_BUDGET_S, 20.0)

    def test_bounded_lookup_preserves_contextvars(self):
        """The worker must see the active profile/auth context, not a blank one."""
        import contextvars

        from quota_providers import builtin

        profile = contextvars.ContextVar("quota_test_profile")
        profile.set("profile-A")

        value = builtin._bounded_account_lookup(
            lambda **kwargs: profile.get(), force_fresh=True, timeout=1.0)
        self.assertEqual(value, "profile-A")

    def test_older_core_without_force_fresh_keyword_still_works(self):
        """An older lookup signature degrades to one no-argument call."""
        from quota_providers import builtin

        acct = _nous_account(account_tier="free")
        no_arg_calls = []

        def old_get(**kwargs):
            if kwargs:
                raise TypeError("unexpected keyword argument 'force_fresh'")
            no_arg_calls.append(True)
            return acct

        fake_mod = types.ModuleType("hermes_cli.nous_account")
        setattr(fake_mod, "get_nous_portal_account_info", old_get)
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}):
            res = builtin._fetch_nous_portal()
        self.assertEqual(res.plan, "Free")
        self.assertEqual(res.access_state, "free")
        self.assertIsNone(res.unavailable_reason)
        # A forced retry after the compatibility fallback would repeat the
        # same core work; the result is reused for both reads.
        self.assertEqual(len(no_arg_calls), 1)

    def test_older_core_signature_skips_the_speculative_keyword_call(self):
        """A lookup that advertises no keyword is never probed with one."""
        from quota_providers import builtin

        acct = _nous_account(account_tier="free")
        calls = []

        def old_get():
            calls.append(True)
            return acct

        fake_mod = types.ModuleType("hermes_cli.nous_account")
        setattr(fake_mod, "get_nous_portal_account_info", old_get)
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}):
            res = builtin._fetch_nous_portal()
        self.assertEqual(res.plan, "Free")
        self.assertEqual(len(calls), 1)

    def test_lookups_pass_force_fresh_explicitly(self):
        """Never call the helper with no arguments when the keyword is known."""
        from quota_providers import builtin

        acct = _nous_account(account_tier="free")
        calls = []

        def get(*args, **kwargs):
            calls.append((args, dict(kwargs)))
            return acct

        fake_mod = types.ModuleType("hermes_cli.nous_account")
        setattr(fake_mod, "get_nous_portal_account_info", get)
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}):
            builtin._fetch_nous_portal()
        self.assertEqual(calls, [((), {"force_fresh": False}),
                                 ((), {"force_fresh": True})])

    def test_timed_out_master_lookup_does_not_start_a_second_request(self):
        """An abandoned worker may still be in flight; do not duplicate it."""
        from quota_providers import builtin

        calls = []

        def slow_get(*args, **kwargs):
            calls.append((args, dict(kwargs)))
            time.sleep(0.25)
            return _nous_account()

        fake_mod = types.ModuleType("hermes_cli.nous_account")
        setattr(fake_mod, "get_nous_portal_account_info", slow_get)
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}), \
             mock.patch.object(builtin, "_NOUS_ACCOUNT_TIMEOUT_S", 0.05):
            res = builtin._fetch_nous_portal()
        self.assertEqual(res.unavailable_reason, "fetcher-unavailable")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ())
        self.assertIs(calls[0][1].get("force_fresh"), False)

    def test_account_api_result_from_the_first_lookup_is_not_fetched_twice(self):
        """A no-arg lookup can already hit the account API; reuse that result."""
        from quota_providers import builtin

        acct = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
        )
        calls = []

        def get(*args, **kwargs):
            calls.append(dict(kwargs))
            return acct

        fake_mod = types.ModuleType("hermes_cli.nous_account")
        setattr(fake_mod, "get_nous_portal_account_info", get)
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}):
            res = builtin._fetch_nous_portal()
        self.assertEqual(res.plan, "Free")
        self.assertEqual(calls, [{"force_fresh": False}])

    def test_error_snapshot_with_stored_free_tier_is_unknown(self):
        """A failed lookup never turns a stored tier claim into Free."""
        error_acct = _nous_account(
            source="error",
            fresh=False,
            account_tier="free",
            raw_claims={},
            paid_service_access=False,
        )
        res = self._fetch_with(error_acct, fresh=error_acct, cached=error_acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertEqual(res.access_state, "unknown")
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Account lookup failed", joined)
        self.assertNotIn("Free tier", joined)

    def test_paid_plan_overrides_a_stale_free_tier_claim(self):
        """A fresh paid subscription outranks a stored/legacy free tier claim."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="free",
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                reason="no_usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Plus", tier=2, monthly_charge=20.0, monthly_credits=110.0,
                credits_remaining=0.0, rollover_credits=0.0,
                current_period_end="2026-12-01",
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.plan, "Plus")
        self.assertEqual(res.access_state, "depleted")
        self.assertIs(res.api_calls_available, False)
        self.assertNotIn("Free tier", "\n".join(res.details))

    def test_depleted_paid_subscription_is_not_free(self):
        """paid=False + an active paid plan + zero credits is depletion, not free."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                allowed=False,
                reason="no_usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
            subscription=types.SimpleNamespace(
                plan="Tier 2",
                tier=2,
                monthly_charge=20.0,
                monthly_credits=110.0,
                credits_remaining=0.0,
                rollover_credits=0.0,
                current_period_end="2026-09-01",
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Tier 2")
        self.assertEqual(len(res.windows), 1)
        self.assertAlmostEqual(res.windows[0].used_percent, 100.0)
        self.assertIs(res.api_calls_available, False)
        joined = "\n".join(res.details)
        self.assertIn("Status: access depleted - top up to restore", joined)
        self.assertNotIn("Free tier", joined)
        self.assertNotIn("unknown", joined)

    def test_depleted_paid_without_balance_fields_is_still_depleted(self):
        """An active paid subscription + denied access mirrors core's exhausted copy."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            paid_service_access_info=types.SimpleNamespace(
                allowed=False,
                paid_access=False,
                reason="no_usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=None,
                purchased_credits_remaining=None,
                total_usable_credits=None,
                member_spend_cap_exceeded=False,
            ),
            subscription=types.SimpleNamespace(
                plan="Tier 2",
                tier=2,
                monthly_charge=20.0,
                monthly_credits=None,
                credits_remaining=None,
                rollover_credits=None,
                current_period_end=None,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Tier 2")
        self.assertIs(res.api_calls_available, False)
        joined = "\n".join(res.details)
        self.assertIn("Status: access depleted", joined)
        self.assertNotIn("Free tier", joined)

    def test_explicitly_unpaid_subscription_is_unknown_not_free_or_depleted(self):
        """An active but explicitly non-paid plan is neither free nor depleted."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            account_tier="standard",
            paid_service_access_info=_nous_access(
                allowed=False,
                reason="no_usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=False,
                subscription_tier=5,
                subscription_credits_remaining=0.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=0.0,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Paid access: unknown", joined)
        self.assertNotIn("Free tier", joined)
        self.assertNotIn("Tier 5", joined)
        self.assertNotIn("depleted", joined)

    def test_denied_paid_access_with_positive_credits_is_not_depleted(self):
        """A denial plus a positive balance is unavailable, not exhaustion."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                allowed=False,
                reason="no_usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=7.5,
                purchased_credits_remaining=2.5,
                total_usable_credits=10.0,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Paid access is unavailable", joined)
        self.assertNotIn("depleted", joined)
        self.assertNotIn("Free tier", joined)

    def test_member_spend_cap_pause_is_not_depletion_or_free(self):
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=False,
            paid_service_access_info=_nous_access(
                allowed=False,
                reason="member_spend_cap_exceeded",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=120.0,
                purchased_credits_remaining=10.0,
                total_usable_credits=130.0,
                member_spend_cap_exceeded=True,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        # A paused included plan is not proof that calls stopped: purchased
        # credits may still continue, and this snapshot has no explicit
        # credits_pay-style answer, so availability stays unproven.
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Status: access paused - member spend cap exceeded", joined)
        self.assertNotIn("depleted", joined)
        self.assertNotIn("Free tier", joined)

    def test_paid_plan_without_credit_evidence_is_unknown_not_depleted(self):
        """A paid-tier claim alone cannot prove depletion when balances are absent."""
        acct = _nous_account(
            account_tier="paid",
            raw_claims={
                "paid_access": False,
                "subscription_tier": 5,
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
            },
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Paid access: unknown", joined)
        self.assertNotIn("Free tier", joined)
        self.assertNotIn("Tier 5", joined)
        self.assertNotIn("depleted", joined)

    def test_unknown_jwt_snapshot_does_not_fabricate_free_or_a_plan(self):
        """Without account_tier or subscription facts, do not infer free/depleted."""
        acct = _nous_account(
            raw_claims={
                "paid_access": False,
                "subscription_tier": 5,
                "member_spend_usd": 0.0,
                "rate_limit_rpm": 50,
                "rate_limit_tpm": 500_000,
                "rate_limit_rph": 2_100,
            },
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertEqual(res.windows, [])
        self.assertIsNone(res.api_calls_available)
        joined = "\n".join(res.details)
        self.assertIn("Paid access: unknown", joined)
        self.assertNotIn("Free tier", joined)
        self.assertNotIn("Tier 5", joined)
        self.assertNotIn("depleted", joined)

    def test_active_paid_snapshot_is_available(self):
        acct = _nous_account(
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_credits_remaining=12.0,
                purchased_credits_remaining=0.0,
                total_usable_credits=12.0,
            ),
        )
        res = self._fetch_with(acct)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Tier 2")
        self.assertIs(res.api_calls_available, True)
        joined = "\n".join(res.details)
        self.assertNotIn("depleted", joined)
        self.assertNotIn("Free tier", joined)

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

    def test_fresh_member_spend_is_read_without_a_jwt(self):
        """The account API's own spend/cap fields are facts, not JWT leftovers."""
        acct = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            raw_claims=None,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
                member_spend_usd=7.5,
                member_spend_cap_usd=100.0,
            ),
        )
        res = self._fetch_with(acct)
        self.assertEqual(res.access_state, "active")
        joined = "\n".join(res.details)
        self.assertIn("Spend this period: $7.50", joined)
        self.assertNotIn("no cap reported", joined)
        self.assertEqual(res.detail_facts.get("spend"), "Spend this period: $7.50")

    def test_fresh_member_spend_wins_over_a_stale_jwt_claim(self):
        """The fresh access values are current; the cached JWT spend is not."""
        fresh = _nous_account(
            source="account_api",
            fresh=True,
            paid_service_access=True,
            paid_service_access_info=_nous_access(
                allowed=True,
                paid_access=True,
                reason="usable_credits",
                has_active_subscription=True,
                active_subscription_is_paid=True,
                subscription_tier=2,
                subscription_monthly_charge=20.0,
                subscription_credits_remaining=14.0,
                purchased_credits_remaining=12.0,
                total_usable_credits=26.0,
                member_spend_usd=7.5,
                member_spend_cap_usd=100.0,
            ),
        )
        cached = _nous_account(
            account_tier="paid",
            paid_service_access=True,
            subscription=types.SimpleNamespace(plan="Plus", tier=2),
            raw_claims={
                "member_spend_usd": 6.0,
                "member_spend_cap_usd": 200.0,
                "rate_limit_rpm": 50,
            },
        )
        res = self._fetch_with(fresh, fresh=fresh, cached=cached)
        joined = "\n".join(res.details)
        self.assertIn("Spend this period: $7.50", joined)
        self.assertNotIn("Spend this period: $6.00", joined)
        self.assertIn("Rate limits: 50 RPM", joined)

    def test_not_logged_in_is_unavailable(self):
        res = self._fetch_with(_nous_account(logged_in=False))
        self.assertEqual(res.unavailable_reason, "not-logged-in")

    def test_fetcher_never_raises(self):
        # account object missing every attribute must degrade, not crash
        res = self._fetch_with(object())
        self.assertIsNotNone(res.unavailable_reason)


# -- Gemini -------------------------------------------------------------------


class GeminiFetcherTests(unittest.TestCase):
    def test_module_reload_does_not_read_local_oauth_store(self):
        from quota_providers import gemini

        with mock.patch("builtins.open", side_effect=AssertionError("unexpected OAuth store read")):
            importlib.reload(gemini)

    def test_public_installed_app_client_matches_gemini_cli_constants(self):
        from quota_providers import gemini
        from quota_providers.gemini import (
            _GEMINI_PUBLIC_CLIENT_ID,
            _GEMINI_PUBLIC_CLIENT_CREDENTIAL,
        )

        # Public installed-app OAuth credentials shipped with Gemini CLI, not secrets.
        expected_id = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
        expected_secret = "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"
        self.assertEqual(_GEMINI_PUBLIC_CLIENT_ID, expected_id)
        self.assertEqual(_GEMINI_PUBLIC_CLIENT_CREDENTIAL, expected_secret)
        source = Path(gemini.__file__).read_text(encoding="utf-8")
        self.assertIn(f'_GEMINI_PUBLIC_CLIENT_ID = "{expected_id}"', source)
        self.assertIn(
            f'_GEMINI_PUBLIC_CLIENT_CREDENTIAL = "{expected_secret}"', source)
        self.assertIn("public installed-app oauth client credentials", source.lower())

    def test_no_gemini_constant_name_reads_as_an_embedded_secret(self):
        """See the matching antigravity test: a secret/token/key/password name
        beside a 20+ char literal is what Hermes' scanner calls
        credential_exposure, and that verdict blocks plugin install outright."""
        from quota_providers import gemini

        source = Path(gemini.__file__).read_text(encoding="utf-8")
        for name in ("_GEMINI_PUBLIC_CLIENT_ID", "_GEMINI_PUBLIC_CLIENT_CREDENTIAL"):
            line = next(ln for ln in source.splitlines() if ln.startswith(name))
            varname = line.split("=", 1)[0].strip()
            for trigger in ("secret", "token", "password", "key"):
                self.assertNotIn(
                    trigger, varname.lower(),
                    f"{varname} would trip the credential_exposure scan")

    def test_renamed_public_client_reaches_the_token_request(self):
        """Same gap as the antigravity test: with no CLI credential file and
        no env override, the bundled public client is what gets posted. Assert
        the renamed constants -- not a synthetic stand-in -- reach the body."""
        from quota_providers import gemini

        seen = {}

        def opener(req, timeout=None):  # noqa: ANN001, ARG001
            seen["body"] = req.data.decode("utf-8")
            return _FakeResponse(b'{"access_token":"fresh"}')

        with mock.patch.dict(gemini.os.environ, {}, clear=True), \
             mock.patch.object(gemini, "urlopen_no_redirect", opener), \
             mock.patch.object(gemini, "_load_creds", return_value=None):
            self.assertEqual(gemini._refresh({"refresh_token": "synthetic-refresh-token"}),
                             "fresh")
        body = seen["body"]
        self.assertIn(f"client_id={gemini._GEMINI_PUBLIC_CLIENT_ID}", body)
        self.assertIn(
            f"client_secret={gemini._GEMINI_PUBLIC_CLIENT_CREDENTIAL}", body)
        self.assertIn("grant_type=refresh_token", body)

    def test_refresh_preserves_cli_public_client_from_credential_file(self):
        from quota_providers import gemini

        seen = {}

        def opener(req, timeout=None):  # noqa: ANN001, ARG001
            seen["body"] = req.data.decode("utf-8")
            return _FakeResponse(b'{"access_token":"fresh"}')

        creds = {
            "client_id": "synthetic-client-id",
            "client_secret": "synthetic-client-secret",
            "refresh_token": "synthetic-refresh-token",
        }
        with mock.patch.dict(gemini.os.environ, {}, clear=True), \
                mock.patch.object(gemini, "urlopen_no_redirect", opener):
            self.assertEqual(gemini._refresh(creds), "fresh")
        self.assertIn("client_id=synthetic-client-id", seen["body"])
        self.assertIn("client_secret=synthetic-client-secret", seen["body"])
        self.assertIn("refresh_token=synthetic-refresh-token", seen["body"])

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
    # Weekly Limit 100% used (resets Aug 23 17:00Z) with the kind-2 entry
    # ("Grok Build") that the panel draws as the LEGEND under the single bar,
    # plus an fn11 flag the panel never renders.
    _GRPC_FIXTURE_HEX = (
        "00000000520a500d0000c84212001a00220b08c0d987d40610c0e3f16f2a0b08"
        "c0ceacd40610c0e3f16f3a070802150000c842421c0802120b08c0d987d40610"
        "c0e3f16f1a0b08c0ceacd40610c0e3f16f580162006801"
    )

    def test_grpc_fixture_reports_the_single_panel_meter(self):
        """The usage panel renders ONE bar for this capture.

        The class comment records what the live capture showed: "Weekly Limit
        100% used (resets Aug 23 17:00Z)" with "Grok Build 3%" as its legend.
        This docstring previously said the bar read 3%, copied from the kind-2
        test below -- which is the legend, not the bar. The assertion below
        expects the 100% the parser actually returns.
        """
        from quota_providers import grok

        raw = bytes.fromhex(self._GRPC_FIXTURE_HEX)
        res = grok._parse_grok_protobuf(raw)
        self.assertIsNotNone(res)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual([w.label for w in res.windows], ["Weekly Limit"])
        self.assertAlmostEqual(res.windows[0].used_percent, 100.0, places=2)
        # Weekly reset: 2026-08-23T17:00:48Z (matches the panel)
        self.assertIn("2026-08-23T17:00:48", res.windows[0].reset_at)
        # The kind breakdown is a legend segment, not a meter of its own.
        self.assertEqual(res.details, [])

    def test_rest_payload_to_windows(self):
        from quota_providers import grok

        payload = {
            "remainingQueries": 7,
            "totalQueries": 10,
            "windowSizeSeconds": 7200,
            "lowEffortRateLimits": None,
            "highEffortRateLimits": {"remainingQueries": 2, "totalQueries": 5},
        }
        with mock.patch.object(grok, "urlopen_no_redirect", _urlopen_returning(payload)):
            res = grok._fetch_grok_rest("cookie=1")
        self.assertIsNone(res.unavailable_reason)
        labels = [w.label for w in res.windows]
        self.assertEqual(labels[0], "2h")
        self.assertAlmostEqual(res.windows[0].used_percent, 30.0, places=2)
        # windowSizeSeconds is a rolling-window LENGTH, not an anchor: the
        # payload carries no reset instant, so none may be reported.
        self.assertIsNone(res.windows[0].reset_at)
        high = [w for w in res.windows if w.label == "high effort"]
        self.assertEqual(len(high), 1)
        self.assertAlmostEqual(high[0].used_percent, 60.0, places=2)

    def test_rest_window_does_not_invent_a_reset_instant(self):
        """Regression: the REST fallback used to report ``now + windowSizeSeconds``
        as the reset time, which produced a reset ("2h 100% (reset today 19:41)")
        that the account never had — the grok.com Usage panel showed a different
        meter entirely (Weekly Limit / Grok Build). A rolling window exposes only
        its length, so the reset instant must stay unset."""
        from quota_providers import grok

        payload = {
            "remainingQueries": 0,
            "totalQueries": 10,
            "windowSizeSeconds": 7200,
            "lowEffortRateLimits": None,
            "highEffortRateLimits": None,
        }
        with mock.patch.object(grok, "urlopen_no_redirect", _urlopen_returning(payload)):
            res = grok._fetch_grok_rest("cookie=1")
        self.assertEqual(len(res.windows), 1)
        self.assertAlmostEqual(res.windows[0].used_percent, 100.0, places=2)
        self.assertIsNone(res.windows[0].reset_at)

    def test_auth_failure_is_reported_not_swallowed(self):
        import urllib.error

        from quota_providers import grok

        def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
            _raise_closed_http_error(urllib.error.HTTPError("url", 403, "forbidden", {}, None))

        with mock.patch.object(grok, "urlopen_no_redirect", _opener):
            res = grok._fetch_grok_rest("cookie=1")
        self.assertEqual(res.unavailable_reason, "cloudflare-blocked")

    def test_grpc_no_usage_field_means_zero_percent(self):
        """Live capture from a free/unused account: the weekly window is present
        but the fn1 usage field is ABSENT — the grok.com panel renders this as
        "0% utilizado", so the parser must report 0.0 instead of hiding the
        number."""
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
        msg = b"\x0a" + _vi(len(inner)) + inner  # fn1 = response payload
        raw = b"\x00" + struct.pack(">I", len(msg)) + msg

        res = grok._parse_grok_protobuf(raw)
        self.assertIsNotNone(res)
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual([w.label for w in res.windows], ["Weekly Limit"])
        self.assertAlmostEqual(res.windows[0].used_percent, 0.0, places=2)
        self.assertIn("2026-08-30T17:00:48", res.windows[0].reset_at)

    def test_grpc_kind_entry_is_not_a_second_quota(self):
        """Regression: the kind-2 entry ("Grok Build") is the legend segment of
        the same weekly bar, so it must not be published as a second window.

        The plugin showed two quotas with the same percentage and the same reset
        date under different names ("Weekly" + "Grok Build"); the vendor panel
        renders a single bar titled "Weekly Limit" with "Grok Build 3%" as its
        legend line.
        """
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

        def _sub(field: int, payload: bytes) -> bytes:
            return bytes([field << 3 | 2]) + _vi(len(payload)) + payload

        reset = b"\x08" + _vi(1790528448)  # fn1 = reset epoch (2026-09-27T17:00:48Z)
        inner = b"\x0d" + struct.pack("<f", 3.0)  # fn1 = Weekly % used
        inner += _sub(5, reset)  # fn5 = weekly window reset
        kind = b"\x08\x02" + b"\x15" + struct.pack("<f", 3.0)  # fn1 = kind 2, fn2 = %
        inner += _sub(7, kind)  # fn7 = kind entry (legend segment)
        inner += _sub(8, b"\x08\x02" + _sub(3, reset))  # fn8 = kind window
        msg = b"\x0a" + _vi(len(inner)) + inner
        raw = b"\x00" + struct.pack(">I", len(msg)) + msg

        res = grok._parse_grok_protobuf(raw)
        self.assertIsNotNone(res)
        self.assertEqual([w.label for w in res.windows], ["Weekly Limit"])
        self.assertAlmostEqual(res.windows[0].used_percent, 3.0, places=2)
        self.assertIn("2026-09-27T17:00:48", res.windows[0].reset_at)
        self.assertEqual(res.details, [])

    def test_grpc_unknown_flag_fields_do_not_become_detail_claims(self):
        """Regression: fn11/fn13 carry flags the vendor UI never renders.

        The grok.com Usage panel for the same payload showed only
        "Weekly Limit 3% used / Resets …" and "Grok Build 3%" — no banked-reset
        or extra-limit indicator. The parser used to turn fn11=1 into
        "Reset banked: available (activate at grok.com)", i.e. it stated a reset
        the account did not have. Unknown flag fields must stay unreported.
        """
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

        inner = b"\x0d" + struct.pack("<f", 3.0)  # fn1 = Weekly % used
        inner += b"\x58\x01"  # fn11 = 1 (unknown flag)
        inner += b"\x68\x01"  # fn13 = 1 (unknown flag)
        msg = b"\x0a" + _vi(len(inner)) + inner
        raw = b"\x00" + struct.pack(">I", len(msg)) + msg

        res = grok._parse_grok_protobuf(raw)
        self.assertIsNotNone(res)
        self.assertEqual([w.used_percent for w in res.windows], [3.0])
        self.assertEqual(res.details, [])

    def test_optin_disabled_by_default(self):
        from quota_providers import grok

        with mock.patch.object(grok, "_grok_enabled", return_value=False):
            res = grok._fetch_grok_optin()
        self.assertEqual(res.unavailable_reason, "opt-in-disabled")


# -- Kimi ---------------------------------------------------------------------

# Live-captured response shape of GET https://api.kimi.com/coding/v1/usages
# (2026-09-23, Hermes-resolved sk-kimi-* key): an RPM-style `limits` list whose
# numeric fields are STRINGS, plus a `usages` map of ratio-based plan windows.
_KIMI_LIVE_PAYLOAD = {
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {
                "limit": "100",
                "used": "57",
                "remaining": "43",
                "resetTime": "2026-09-23T04:08:05.435444Z",
            },
        }
    ],
    "usages": {
        "limit_5h": {"used_ratio": 0, "reset_time": "2026-09-23T04:08:05Z"},
        "limit_month_total": {"used_ratio": 0.1559, "reset_time": "2026-10-19T00:00:00Z"},
        "limit_month_code": {"used_ratio": 0, "reset_time": "2026-10-19T00:00:00Z"},
    },
}


class KimiFetcherTests(unittest.TestCase):
    def test_no_credentials_anywhere(self):
        """Neither Hermes auth nor the legacy session file -> no-credentials."""
        from quota_providers import kimi

        with mock.patch.object(kimi, "_load_hermes_creds", return_value=(None, None)), \
             mock.patch.object(kimi, "_load_creds", return_value=(None, None)):
            res = kimi.fetch_kimi_quota()
        self.assertEqual(res.unavailable_reason, "no-credentials")

    def test_hermes_auth_used_when_session_file_missing(self):
        """Hermes-managed kimi-coding credential must work without ~/kimi_session.json."""
        from quota_providers import kimi

        captured = {}

        def _opener(req, timeout=None):  # noqa: ANN001, ARG001
            captured["req"] = req
            return _FakeResponse(json.dumps(_KIMI_LIVE_PAYLOAD).encode("utf-8"))

        with mock.patch.object(kimi, "_load_hermes_creds",
                               return_value=("sk-kimi-test", "https://api.kimi.com/coding")), \
             mock.patch.object(kimi, "_load_creds", return_value=(None, None)), \
             mock.patch.object(kimi, "urlopen_no_redirect", _opener):
            res = kimi.fetch_kimi_quota()
        self.assertIsNone(res.unavailable_reason)
        self.assertTrue(res.has_data())
        # The Hermes-resolved base URL drives the usages endpoint.
        req = captured["req"]
        self.assertEqual(req.full_url, "https://api.kimi.com/coding/v1/usages")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-kimi-test")

    def test_hermes_dotenv_base_url_override_is_honored(self):
        """Use the core dotenv-aware resolver, rather than process env only."""
        from quota_providers import kimi

        pconfig = types.SimpleNamespace(
            auth_type="api_key", inference_base_url="https://api.moonshot.ai/v1",
            base_url_env_var="KIMI_BASE_URL")
        base_url = mock.Mock(return_value="https://proxy.example/v1")
        auth = types.ModuleType("hermes_cli.auth")
        config = types.ModuleType("hermes_cli.config")
        setattr(auth, "PROVIDER_REGISTRY", {"kimi-coding": pconfig})
        setattr(auth, "_resolve_api_key_provider_secret", mock.Mock(
            return_value=("sk-kimi-test", "dotenv")))
        setattr(config, "get_env_value_prefer_dotenv", base_url)
        setattr(auth, "_resolve_kimi_base_url",
                lambda _key, default, override: override or default)
        with mock.patch.dict(sys.modules, {"hermes_cli.auth": auth,
                                           "hermes_cli.config": config}):
            key, resolved_url = kimi._load_hermes_creds()
        self.assertEqual(key, "sk-kimi-test")
        self.assertEqual(resolved_url, "https://proxy.example/v1")
        base_url.assert_called_once_with("KIMI_BASE_URL")

    def test_live_payload_shape_parsed(self):
        """Real 2026-09 payload: usages ratios + string-valued RPM limits."""
        from quota_providers import kimi

        with mock.patch.object(kimi, "_load_hermes_creds",
                               return_value=("sk-kimi-test", "https://api.kimi.com/coding")), \
             mock.patch.object(kimi, "_load_creds", return_value=(None, None)), \
             mock.patch.object(kimi, "urlopen_no_redirect",
                               _urlopen_returning(_KIMI_LIVE_PAYLOAD)):
            res = kimi.fetch_kimi_quota()
        by_label = {w.label: w for w in res.windows}
        session = by_label["Session (5h)"]
        self.assertEqual(session.used_percent, 0.0)
        self.assertEqual(session.reset_at, "2026-09-23T04:08:05Z")
        monthly = by_label["Monthly total"]
        self.assertAlmostEqual(monthly.used_percent, 15.59, places=2)
        self.assertEqual(monthly.reset_at, "2026-10-19T00:00:00Z")
        self.assertIn("Monthly code", by_label)
        rate = by_label["Rate (5h)"]
        self.assertEqual(rate.used_percent, 57.0)
        self.assertEqual(rate.reset_at, "2026-09-23T04:08:05.435444Z")

    def test_session_file_fallback_when_hermes_auth_absent(self):
        """Standalone installs keep the legacy ~/kimi_session.json path."""
        from quota_providers import kimi

        captured = {}

        def _opener(req, timeout=None):  # noqa: ANN001, ARG001
            captured["req"] = req
            return _FakeResponse(json.dumps(_KIMI_LIVE_PAYLOAD).encode("utf-8"))

        with mock.patch.object(kimi, "_load_hermes_creds", return_value=(None, None)), \
             mock.patch.object(kimi, "_load_creds", return_value=("legacy-key", None)), \
             mock.patch.object(kimi, "urlopen_no_redirect", _opener):
            res = kimi.fetch_kimi_quota()
        self.assertIsNone(res.unavailable_reason)
        req = captured["req"]
        self.assertEqual(req.get_header("Authorization"), "Bearer legacy-key")

    def test_auth_failed_maps_401(self):
        from quota_providers import kimi

        def _opener(_req, timeout=None):
            _raise_closed_http_error(
                urllib.error.HTTPError("url", 401, "unauthorized", {}, None))

        with mock.patch.object(kimi, "_load_hermes_creds",
                               return_value=("sk-kimi-test", "https://api.kimi.com/coding")), \
             mock.patch.object(kimi, "_load_creds", return_value=(None, None)), \
             mock.patch.object(kimi, "urlopen_no_redirect", _opener):
            res = kimi.fetch_kimi_quota()
        self.assertEqual(res.unavailable_reason, "auth-failed")

    def test_empty_payload_is_no_data(self):
        from quota_providers import kimi

        with mock.patch.object(kimi, "_load_hermes_creds",
                               return_value=("sk-kimi-test", "https://api.kimi.com/coding")), \
             mock.patch.object(kimi, "_load_creds", return_value=(None, None)), \
             mock.patch.object(kimi, "urlopen_no_redirect", _urlopen_returning({})):
            res = kimi.fetch_kimi_quota()
        self.assertEqual(res.unavailable_reason, "no-data")


# -- Anthropic (builtin adapter over core fetch_account_usage) -----------------


class AnthropicBuiltinFetcherTests(unittest.TestCase):
    """When the core usage fetch returns no snapshot, the reason must be useful."""

    def _fetch(self):
        from quota_providers.builtin import _fetch_anthropic

        return _fetch_anthropic()

    def test_no_snapshot_without_token_is_no_credentials(self):
        import quota_providers.builtin as builtin

        with mock.patch.object(builtin, "_core_anthropic_token", return_value=None):
            res = self._fetch()
        self.assertEqual(res.unavailable_reason, "no-credentials")

    def test_no_snapshot_with_token_is_fetch_error(self):
        """A resolvable token + failed request is reported as a fetch error."""
        import quota_providers.builtin as builtin

        with mock.patch.object(builtin, "_anthropic_usage_payload",
                               return_value=(None, "fetch-error:OSError")):
            res = self._fetch()
        self.assertEqual(res.unavailable_reason, "fetch-error:OSError")

    def test_payload_windows_and_details_are_mapped(self):
        """The direct adapter maps legacy windows and extra usage in one read."""
        import quota_providers.builtin as builtin

        payload = {
            "five_hour": {
                "utilization": 0.42,
                "resets_at": "2026-09-23T09:00:00Z",
            },
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 1.0,
                "monthly_limit": 5.0,
                "currency": "USD",
            },
        }
        with mock.patch.object(builtin, "_anthropic_usage_payload",
                               return_value=(payload, None)):
            res = self._fetch()
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)
        self.assertEqual(res.windows[0].label, "Current session")
        self.assertEqual(res.windows[0].used_percent, 42.0)
        self.assertEqual(res.windows[0].reset_at, "2026-09-23T09:00:00+00:00")
        self.assertEqual(res.details, ["Extra usage: 1.00 / 5.00 USD"])

    def test_api_key_is_rejected_before_network(self):
        import quota_providers.builtin as builtin

        with mock.patch.object(builtin, "_core_anthropic_token", return_value="api-key"), \
             mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=False), \
             mock.patch.object(builtin, "urlopen_no_redirect") as urlopen:
            res = self._fetch()
        self.assertEqual(res.unavailable_reason, builtin._ANTHROPIC_OAUTH_REQUIRED_REASON)
        urlopen.assert_not_called()

# Live-captured /api/oauth/usage ``limits`` list (2026-09-23, Team plan).
_ANTHROPIC_LIMITS_PAYLOAD = {
    "five_hour": {"utilization": 65.0, "resets_at": "2026-09-23T23:20:00+00:00"},
    "nimbus_quill": {"utilization": 0.0, "resets_at": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 66,
         "resets_at": "2026-09-23T23:19:59+00:00", "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 11,
         "resets_at": "2026-09-30T07:59:59+00:00", "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 7,
         "resets_at": "2026-09-30T08:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}


class AnthropicScopedLimitTests(unittest.TestCase):
    """Model-scoped weekly limits from the single usage payload."""

    def test_scoped_limit_uses_display_name_not_codename(self):
        from quota_providers.builtin import parse_anthropic_scoped_limits

        windows = parse_anthropic_scoped_limits(_ANTHROPIC_LIMITS_PAYLOAD)
        self.assertEqual([(w.label, w.used_percent, w.reset_at) for w in windows],
                         [("Current session", 66.0, "2026-09-23T23:19:59+00:00"),
                          ("Current week", 11.0, "2026-09-30T07:59:59+00:00"),
                          ("Fable week", 7.0, "2026-09-30T08:00:00+00:00")])

    def test_scoped_limit_falls_back_to_identifiers(self):
        from quota_providers.builtin import parse_anthropic_scoped_limits

        payload = {"limits": [
            {"kind": "session", "percent": 4,
             "resets_at": "2026-09-23T23:19:59Z"},
            {"kind": "weekly_scoped", "percent": 6,
             "scope": {"model": {"id": "model-id"}}},
            {"kind": "weekly_scoped", "percent": 8,
             "scope": {"model": {}, "surface": "surface-id"}},
        ]}
        windows = parse_anthropic_scoped_limits(payload)
        self.assertEqual([w.label for w in windows],
                         ["Current session", "model-id week", "surface-id week"])
        self.assertEqual(windows[0].reset_at, "2026-09-23T23:19:59+00:00")

    def test_unscoped_or_malformed_entries_are_ignored(self):
        from quota_providers.builtin import parse_anthropic_scoped_limits

        payload = {"limits": [
            {"kind": "weekly_scoped", "percent": 5, "scope": None},
            {"kind": "weekly_scoped", "percent": "5",
             "scope": {"model": {"display_name": "X"}}},
            {"kind": "weekly_scoped", "percent": True,
             "scope": {"model": {"display_name": "X"}}},
            "garbage",
        ]}
        self.assertEqual(parse_anthropic_scoped_limits(payload), [])
        self.assertEqual(parse_anthropic_scoped_limits(["not", "a", "dict"]), [])

    def test_scoped_windows_append_to_usage_payload(self):
        import quota_providers.builtin as builtin

        # _core_anthropic_is_oauth must be stubbed too: builtin rejects a
        # non-OAuth token before parsing, so without this these tests only pass
        # when Hermes core is unimportable, and they exercise the parser rather
        # than the path that ships.
        with mock.patch.object(builtin, "_core_anthropic_token", return_value="tok"), \
             mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
             mock.patch.object(builtin, "urlopen_no_redirect",
                               _urlopen_returning(_ANTHROPIC_LIMITS_PAYLOAD)):
            res = builtin._fetch_anthropic()
        self.assertEqual(
            [w.label for w in res.windows],
            ["Current session", "Current week", "Fable week"],
        )


    def test_weekly_all_limit_fills_core_snapshot_gap(self):
        import quota_providers.builtin as builtin

        # _core_anthropic_is_oauth must be stubbed too: builtin rejects a
        # non-OAuth token before parsing, so without this these tests only pass
        # when Hermes core is unimportable, and they exercise the parser rather
        # than the path that ships.
        with mock.patch.object(builtin, "_core_anthropic_token", return_value="tok"), \
             mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
             mock.patch.object(builtin, "urlopen_no_redirect",
                               _urlopen_returning(_ANTHROPIC_LIMITS_PAYLOAD)):
            res = builtin._fetch_anthropic()
        self.assertEqual(
            [w.label for w in res.windows],
            ["Current session", "Current week", "Fable week"],
        )

    def test_scoped_limits_are_used_without_core_windows(self):
        import quota_providers.builtin as builtin

        payload = {
            "limits": [
                {"kind": "weekly_all", "percent": 11,
                 "resets_at": "2026-09-30T07:59:59+00:00", "scope": None},
                {"kind": "weekly_scoped", "percent": 7,
                 "resets_at": "2026-09-30T08:00:00+00:00",
                 "scope": {"model": {"display_name": "Fable"}}},
            ]
        }
        # The OAuth classifier must be stubbed alongside the token: builtin
        # drops a token the core reports as non-OAuth (builtin.py:165), so on
        # a machine where Hermes core is importable this test would otherwise
        # get windows == [] and pass for the wrong reason.
        with mock.patch.object(builtin, "_core_anthropic_token", return_value="tok"), \
             mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
             mock.patch.object(builtin, "urlopen_no_redirect",
                               _urlopen_returning(payload)):
            res = builtin._fetch_anthropic()
        self.assertEqual([w.label for w in res.windows], ["Current week", "Fable week"])


    def test_scoped_reset_z_is_normalized_for_python_39(self):
        import quota_providers.builtin as builtin

        payload = {
            "limits": [{
                "kind": "weekly_scoped",
                "percent": 5,
                "resets_at": "2026-09-30T08:00:00Z",
                "scope": {"model": {"display_name": "Fable"}},
            }]
        }
        windows = builtin.parse_anthropic_scoped_limits(payload)
        self.assertEqual(windows[0].reset_at, "2026-09-30T08:00:00+00:00")

    def test_usage_read_failure_is_fail_open(self):
        import quota_providers.builtin as builtin

        def _boom(_req, timeout=None):  # noqa: ANN001, ARG001
            raise OSError("down")

        with mock.patch.object(builtin, "_core_anthropic_token", return_value="tok"), \
             mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
             mock.patch.object(builtin, "urlopen_no_redirect", _boom):
            res = builtin._fetch_anthropic()
        self.assertEqual(res.unavailable_reason, "fetch-error:OSError")

    def test_usage_read_is_single_request_with_bounded_timeout(self):
        import quota_providers.builtin as builtin

        seen = []

        def _opener(_req, timeout=None):  # noqa: ANN001
            seen.append(timeout)
            return _FakeResponse(json.dumps(_ANTHROPIC_LIMITS_PAYLOAD).encode())

        with mock.patch.object(builtin, "_core_anthropic_token", return_value="tok"), \
             mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
             mock.patch.object(builtin, "urlopen_no_redirect", _opener):
            res = builtin._fetch_anthropic()
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)
        self.assertLessEqual(seen[0], builtin._ANTHROPIC_TIMEOUT_S)



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
             mock.patch.object(zai, "urlopen_no_redirect", opener), \
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
