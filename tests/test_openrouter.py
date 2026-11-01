"""Offline OpenRouter contracts. All keys, accounts and amounts are synthetic."""
from __future__ import annotations

import dataclasses
from email.message import Message
import importlib
import json
from io import BytesIO
from pathlib import Path
import shutil
import sys
import threading
import time
import types
from typing import Optional
import unittest
from unittest import mock
from urllib.error import HTTPError

from widget_harness import nodes, render, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class OpenRouterTests(unittest.TestCase):
    def module(self):
        import quota_providers
        self.assertEqual(
            quota_providers.PROVIDER_FETCHERS["openrouter"].__module__,
            "quota_providers.openrouter",
        )
        return importlib.import_module("quota_providers.openrouter")

    def native_modules(self, primary: Optional[str] = "FAKE_A", pool=()):
        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        auth = types.ModuleType("hermes_cli.auth")
        setattr(auth, "read_credential_pool", mock.Mock(return_value=list(pool)))
        runtime = types.ModuleType("hermes_cli.runtime_provider")
        setattr(runtime, "resolve_runtime_provider", mock.Mock(return_value={"api_key": primary}))
        config = types.ModuleType("hermes_cli.config")
        setattr(config, "get_env_value_prefer_dotenv", mock.Mock(return_value=None))
        setattr(package, "auth", auth)
        return {"hermes_cli": package, "hermes_cli.auth": auth,
                "hermes_cli.runtime_provider": runtime, "hermes_cli.config": config}

    def fetch(self, get, credentials=None):
        mod = self.module()
        credentials = credentials or [("Key 1 (native)", "FAKE_A"), ("Key 2 (saved)", "FAKE_B")]
        with mock.patch.object(mod, "_resolve_credentials", return_value=credentials), \
             mock.patch.object(mod, "_get_json", side_effect=get) as calls:
            result = mod.fetch_openrouter_quota()
        return result, calls

    def test_native_resolution_and_private_deduplication(self):
        mod = self.module()
        modules = self.native_modules(pool=[
            {"access_token": "FAKE_A", "label": "PRIVATE_LABEL", "id": "PRIVATE_ID"},
            {"access_token": "FAKE_B", "label": "PRIVATE_LABEL"},
            {"access_token": " FAKE_B "}, {"access_token": ""}, None,
        ])
        with mock.patch.dict(sys.modules, modules):
            rows = mod._resolve_credentials()
        self.assertEqual(rows, [("Key 1 (native)", "FAKE_A"), ("Key 2 (saved)", "FAKE_B")])
        modules["hermes_cli.auth"].read_credential_pool.assert_called_once_with("openrouter")
        # Explicit canonical endpoint bypasses runtime pool load/select (which writes).
        modules["hermes_cli.runtime_provider"].resolve_runtime_provider.assert_called_once_with(
            requested="openrouter", explicit_base_url="https://openrouter.ai/api/v1",
            explicit_api_key=None,
        )

    def test_native_dotenv_key_and_saved_environment_references(self):
        mod = self.module()
        modules = self.native_modules(pool=[
            {"source": "env:OPENROUTER_API_KEY", "access_token": "STALE_SECRET"},
            {"source": "env:OPENROUTER_API_KEY_2", "label": "PRIVATE_LABEL"},
            {"source": "env:MISSING", "access_token": "STALE_SECRET"},
        ])
        config = modules["hermes_cli.config"]
        config.get_env_value_prefer_dotenv.side_effect = lambda name: {
            "OPENROUTER_API_KEY": "FAKE_A", "OPENROUTER_API_KEY_2": "FAKE_B",
        }.get(name)
        with mock.patch.dict(sys.modules, modules):
            rows = mod._resolve_credentials()
        self.assertEqual(rows, [("Key 1 (native)", "FAKE_A"), ("Key 2 (environment)", "FAKE_B")])
        self.assertEqual(modules["hermes_cli.runtime_provider"].resolve_runtime_provider.call_args.kwargs["explicit_api_key"], "FAKE_A")

    def test_native_failure_does_not_hide_saved_keys(self):
        mod = self.module()
        modules = self.native_modules(pool=[{"access_token": "FAKE_B"}])
        modules["hermes_cli.runtime_provider"].resolve_runtime_provider.side_effect = RuntimeError("PRIVATE_SECRET")
        with mock.patch.dict(sys.modules, modules):
            self.assertEqual(mod._resolve_credentials(), [("Key 1 (saved)", "FAKE_B")])

    def test_pool_failure_does_not_hide_native_key(self):
        mod = self.module()
        modules = self.native_modules()
        modules["hermes_cli.auth"].read_credential_pool.side_effect = RuntimeError("PRIVATE_SECRET")
        with mock.patch.dict(sys.modules, modules):
            self.assertEqual(mod._resolve_credentials(), [("Key 1 (native)", "FAKE_A")])

    def test_no_credentials_and_resolution_failure(self):
        mod = self.module()
        with mock.patch.dict(sys.modules, self.native_modules(primary=None)):
            self.assertEqual(mod._resolve_credentials(), [])
        with mock.patch.object(mod, "_resolve_credentials", return_value=[]):
            self.assertEqual(mod.fetch_openrouter_quota().unavailable_reason, "no-credentials")
        with mock.patch.object(mod, "_resolve_credentials", side_effect=RuntimeError("PRIVATE_SECRET")):
            result = mod.fetch_openrouter_quota()
        self.assertEqual(result.unavailable_reason, "fetch-error")
        self.assertNotIn("PRIVATE_SECRET", repr(result))

    def test_each_key_cap_and_usage_one_scoped_wallet(self):
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": 20, "total_usage": 12}}, None
            return {"data": {"label": key, "hash": "PRIVATE_HASH", "email": "private@example.invalid",
                             "limit": 5 if key == "FAKE_A" else 10, "limit_remaining": 2,
                             "usage": 6, "usage_daily": 1, "usage_weekly": 2,
                             "usage_monthly": 3, "limit_reset": "monthly"}}, None
        result, calls = self.fetch(get)
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.account_balances, [])
        self.assertEqual([w.used_percent for w in result.windows], [60, 80])
        self.assertEqual([w.label for w in result.windows], ["Key 1 (native) quota", "Key 2 (saved) quota"])
        wallets = [c for c in calls.call_args_list if c.args[0].endswith("/credits")]
        self.assertEqual(len(wallets), 1)
        self.assertEqual(wallets[0].args[1], "FAKE_A")
        self.assertEqual(len(calls.call_args_list), 3)
        text = json.dumps(dataclasses.asdict(result))
        for expected in ("USD 8.00 remaining", "Locally configured credentials: 2", "not an inventory of all account keys",
                         "Account wallet via Key 1 (native)", "unverified", "not a total across accounts",
                         "usage today UTC USD 1.0000", "usage this week UTC USD 2.0000",
                         "usage this month UTC USD 3.0000", "resets monthly"):
            self.assertIn(expected, text)
        for secret in ("FAKE_A", "FAKE_B", "PRIVATE_HASH", "private@example.invalid"):
            self.assertNotIn(secret, text)

    def test_single_capped_key_keeps_cap_reset_and_usage_details(self):
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": 20, "total_usage": 12}}, None
            return {"data": {"limit": 5, "limit_remaining": 2, "usage": 6,
                             "usage_daily": 1, "usage_weekly": 2,
                             "usage_monthly": 3, "limit_reset": "monthly"}}, None
        result, calls = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(result.has_data())
        self.assertEqual([(w.label, w.used_percent) for w in result.windows],
                         [("Key quota", 60.0)])
        # Every fact survives; only the generated "Key 1 (native)" prefix goes.
        self.assertEqual(result.details, [
            "Key cap USD 5.00; remaining USD 2.00; resets monthly",
            "Usage total USD 6.0000",
            "Usage today UTC USD 1.0000",
            "Usage this week UTC USD 2.0000",
            "Usage this month UTC USD 3.0000",
        ])
        self.assertEqual([(b.currency, b.total_balance) for b in result.account_balances],
                         [("USD", "8.00")])
        self.assertEqual((result.credential_scope, result.key_limit_state),
                         ("single", "capped"))
        self.assertEqual(len(calls.call_args_list), 2)
        rendered = json.dumps(dataclasses.asdict(result))
        for hidden in ("Locally configured credentials", "Management API key",
                       "Key 1 (native)", "Account membership", "unverified",
                       "not a total across accounts", "Account wallet"):
            self.assertNotIn(hidden, rendered)
        self.assertNotIn("FAKE_A", rendered)

    def test_single_uncapped_key_hides_the_cap_but_keeps_usage(self):
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": "10.5", "total_usage": "2.25"}}, None
            return {"data": {"limit": None, "usage": 2}}, None
        result, _ = self.fetch(get, credentials=[("Key 1 (saved)", "FAKE_B")])
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(result.has_data())
        self.assertEqual(result.windows, [])
        # "No key-specific cap" is not a cap fact: nothing to show for the cap.
        self.assertEqual(result.details, ["Usage total USD 2.0000"])
        self.assertNotIn("no key-specific cap", repr(result))
        self.assertEqual([(b.currency, b.total_balance) for b in result.account_balances],
                         [("USD", "8.25")])
        self.assertEqual((result.credential_scope, result.key_limit_state),
                         ("single", "uncapped"))
        self.assertNotIn("Key 1", repr(result))
        # A wallet amount is not a provider-reported API-availability flag:
        # the Desktop tones this funded, uncapped key from the balance without
        # claiming "API calls available: yes" or adding another detail line.
        self.assertIsNone(result.api_calls_available)
        self.assertNotIn("API calls available", repr(result))

    def test_single_zero_cap_stays_visible_without_a_percentage(self):
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": 20, "total_usage": 20}}, None
            # A zero cap has no denominator: a percentage would be fabricated.
            return {"data": {"limit": 0, "limit_remaining": 0, "usage": 4,
                             "limit_reset": "weekly"}}, None
        result, _ = self.fetch(get, credentials=[("Key 1 (saved)", "FAKE_B")])
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.windows, [])
        self.assertEqual(result.details, [
            "Key cap USD 0.00; remaining USD 0.00 (key cap exhausted; account wallet is separate); resets weekly",
            "Usage total USD 4.0000",
        ])
        self.assertEqual([(b.currency, b.total_balance) for b in result.account_balances],
                         [("USD", "0.00")])
        self.assertTrue(result.has_data())

    def test_single_uncapped_key_without_usage_is_not_a_false_success(self):
        # With no cap and no usage fields, the omitted placeholder must not
        # leave an "available" record that renders nothing at all.
        def get(url, key):
            if url.endswith("/credits"):
                return None, "http-429"
            return {"data": {"limit": None}}, None
        result, _ = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        self.assertEqual(result.unavailable_reason, "no-data")
        self.assertFalse(result.has_data())
        self.assertEqual(result.windows, [])
        self.assertEqual(result.details, ["Account credits unavailable (http-429)"])
        self.assertEqual(result.account_balances, [])

    def test_multi_key_uncapped_placeholders_stay_scoped(self):
        # Scoped cards keep their per-key no-cap lines and wallet diagnostics;
        # the single-key display rules must not leak into this mode.
        def get(url, key):
            if url.endswith("/credits"):
                return None, "http-429"
            return {"data": {"limit": None}}, None
        result, _ = self.fetch(get)
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(result.has_data())
        self.assertEqual(result.account_balances, [])
        self.assertEqual((result.credential_scope, result.key_limit_state),
                         ("scoped", None))
        for expected in (
            "Locally configured credentials: 2",
            "Account credits unavailable via Key 1 (native) (http-429)",
            "Account membership of other keys is unverified",
            "Key 1 (native): no key-specific cap (account wallet still applies)",
            "Key 2 (saved): no key-specific cap (account wallet still applies)",
        ):
            self.assertIn(expected, repr(result))

    def test_single_key_failures_keep_the_explanations(self):
        def get(url, key):
            if url.endswith("/credits"):
                return None, "http-403"
            return None, "auth-failed"
        result, _ = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        self.assertEqual(result.unavailable_reason, "no-data")
        self.assertFalse(result.has_data())
        self.assertEqual(result.account_balances, [])
        self.assertEqual((result.credential_scope, result.key_limit_state),
                         ("single", "unknown"))
        self.assertEqual(result.details, [
            "Account credits unavailable (http-403)",
            "Unavailable (auth-failed)",
        ])
        self.assertNotIn("Locally configured credentials", repr(result))
        self.assertNotIn("Account membership", repr(result))
        self.assertNotIn("Key 1 (native)", repr(result))

    def test_single_key_wallet_failure_keeps_key_usage(self):
        def get(url, key):
            if url.endswith("/credits"):
                return None, "http-429"
            return {"data": {"limit": None, "usage": 2}}, None
        result, _ = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.account_balances, [])
        self.assertEqual(result.details, [
            "Account credits unavailable (http-429)",
            "Usage total USD 2.0000",
        ])
        self.assertTrue(result.has_data())

    def test_single_key_failure_stays_visible_beside_the_wallet(self):
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": 20, "total_usage": 12}}, None
            return None, "timeout"
        result, _ = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([(b.currency, b.total_balance) for b in result.account_balances],
                         [("USD", "8.00")])
        self.assertEqual(result.details, ["Unavailable (timeout)"])
        self.assertTrue(result.has_data())

    def test_single_key_tiny_nonzero_wallet_is_not_zero(self):
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": "0.004", "total_usage": 0}}, None
            return {"data": {"limit": None, "usage": 0}}, None
        result, _ = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        self.assertEqual(result.account_balances[0].total_balance, "0.0040")

    def commands_module(self):
        """Load the real CLI renderer against a stubbed Hermes home."""
        name = "openrouter_cli_contract"
        cached = sys.modules.get(name + ".commands")
        if cached is not None:
            return cached
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(ROOT)]
        constants = types.ModuleType("hermes_constants")
        setattr(constants, "get_hermes_home", lambda: ROOT)
        with mock.patch.dict(sys.modules, {name: pkg, "hermes_constants": constants}):
            return importlib.import_module(name + ".commands")

    def render_cli(self, result):
        """Render the real CLI dashboard from the fetcher's own record."""
        record = {
            "label": result.label,
            "windows": [dataclasses.asdict(w) for w in result.windows],
            "details": list(result.details),
            "account_balances": [dataclasses.asdict(b) for b in result.account_balances],
            "unavailable_reason": result.unavailable_reason,
        }
        cmd = self.commands_module()
        with mock.patch.object(cmd, "read_quota_cache",
                               return_value={"providers": {"openrouter": record}}), \
             mock.patch.object(cmd, "_age_label", return_value="fresh"):
            return cmd._render_quota("openrouter")

    def test_single_capped_key_reaches_the_cli_dashboard(self):
        # The CLI renders both `details` and the single-key wallet; dropping
        # either once left single-key usage or the account balance invisible.
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": 20, "total_usage": 12}}, None
            return {"data": {"limit": 5, "limit_remaining": 2, "usage": 6,
                             "limit_reset": "monthly"}}, None
        result, _ = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        rendered = self.render_cli(result)
        self.assertIn("Account balance: $8.00 USD", rendered)
        self.assertIn("Key quota", rendered)
        self.assertIn("40% left", rendered)
        for line in result.details:
            self.assertIn(line, rendered)
        self.assertEqual(rendered.count("$8.00"), 1)
        self.assertNotIn("Key 1 (native)", rendered)
        self.assertNotIn("no usage data reported", rendered)

    def test_wallet_only_single_key_reaches_the_cli_dashboard(self):
        # /key reports no cap and no usage, so the wallet is the whole record.
        # A nonempty account_balances list must suppress "no usage data".
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": 20, "total_usage": 12}}, None
            return {"data": {"limit": None}}, None
        result, _ = self.fetch(get, credentials=[("Key 1 (native)", "FAKE_A")])
        self.assertEqual(result.windows, [])
        self.assertEqual(result.details, [])
        self.assertEqual([(b.currency, b.total_balance) for b in result.account_balances],
                         [("USD", "8.00")])
        self.assertIsNone(result.unavailable_reason)
        rendered = self.render_cli(result)
        self.assertIn("Account balance: $8.00 USD", rendered)
        self.assertNotIn("no usage data reported", rendered)
        self.assertNotIn("Account wallet", rendered)
        self.assertNotIn("Key 1 (native)", rendered)

    def test_duplicates_are_not_queried_twice(self):
        mod = self.module()
        modules = self.native_modules(pool=[{"access_token": "FAKE_A"}, {"access_token": "FAKE_B"}, {"access_token": "FAKE_B"}])
        with mock.patch.dict(sys.modules, modules), mock.patch.object(mod, "_get_json", return_value=({"data": {"limit": None, "usage": 1}}, None)) as get:
            result = mod.fetch_openrouter_quota()
        self.assertEqual(get.call_count, 3)
        self.assertIn("Locally configured credentials: 2", repr(result))

    def test_key_failure_and_wallet_failure_are_independent(self):
        def get(url, key):
            if url.endswith("/credits"):
                return None, "http-403"
            if key == "FAKE_A":
                return None, "auth-failed"
            return {"data": {"limit": None, "usage": 2}}, None
        result, _ = self.fetch(get)
        self.assertIsNone(result.unavailable_reason)
        self.assertIn("Key 1 (native): unavailable (auth-failed)", result.details)
        self.assertIn("Key 2 (saved): usage total USD 2.0000", result.details)
        self.assertIn("Account credits unavailable via Key 1 (native) (http-403)", result.details)
        self.assertEqual(result.windows, [])

    def test_unexpected_wallet_exception_does_not_hide_keys(self):
        def get(url, key):
            if url.endswith("/credits"):
                raise RuntimeError("PRIVATE_SECRET")
            return {"data": {"limit": None, "usage": 0}}, None
        result, _ = self.fetch(get)
        self.assertIsNone(result.unavailable_reason)
        self.assertIn("Account credits unavailable via Key 1 (native) (fetch-error)", result.details)
        self.assertNotIn("PRIVATE_SECRET", repr(result))

    def test_all_failures_are_not_reported_as_success(self):
        result, _ = self.fetch(lambda *_: (None, "http-429"))
        self.assertEqual(result.unavailable_reason, "no-data")
        self.assertFalse(result.has_data())
        self.assertIn("Key 2 (saved): unavailable (http-429)", result.details)

    def test_wallet_survives_when_all_key_reads_fail(self):
        def get(url, key):
            if url.endswith("/credits"):
                return {"data": {"total_credits": 10, "total_usage": 10}}, None
            return None, "http-429"
        result, _ = self.fetch(get)
        self.assertTrue(result.has_data())
        self.assertEqual(result.windows, [])
        self.assertIn("USD 0.00 remaining", repr(result))
        self.assertIn("Key 2 (saved): unavailable (http-429)", result.details)

    def test_hung_requests_have_one_shared_deadline(self):
        mod = self.module()
        release = threading.Event()
        def get(url, key):
            if url.endswith("/credits") or key == "FAKE_A":
                release.wait(2)
            return {"data": {"limit": None, "usage": 1}}, None
        try:
            with mock.patch.object(mod, "_FETCH_BUDGET_S", 0.05):
                start = time.monotonic()
                result, _ = self.fetch(get)
                elapsed = time.monotonic() - start
            self.assertLess(elapsed, 0.5)
            before = dataclasses.asdict(result)
            self.assertIn("Key 1 (native): unavailable (timeout)", result.details)
            self.assertIn("Key 2 (saved): usage total USD 1.0000", result.details)
            self.assertIn("Account credits unavailable via Key 1 (native) (timeout)", result.details)
        finally:
            release.set()
        self.assertEqual(dataclasses.asdict(result), before)

    def test_zero_exhausted_and_uncapped_keys_do_not_invent_percentages(self):
        mod = self.module()
        for limit, left, expected in [(0, 0, []), (None, None, []), (5, 0, [100]), (5, -1, []), (5, 6, [])]:
            with self.subTest(limit=limit, left=left):
                with mock.patch.object(mod, "_get_json", return_value=({"data": {"limit": limit, "limit_remaining": left, "usage": 0}}, None)):
                    windows, details, limit_state = mod._key_result("Key 1", "FAKE_A")
                self.assertEqual([w.used_percent for w in windows], expected)
                self.assertEqual(limit_state, "uncapped" if limit is None else "capped")
                self.assertIn("Key 1: usage total USD 0.0000", details)
                if left == 0:
                    self.assertIn("key cap exhausted; account wallet is separate", repr(details))
                if limit is None:
                    self.assertIn("no key-specific cap (account wallet still applies)", repr(details))

    def test_key_limit_state_only_calls_an_explicit_null_uncapped(self):
        # "No cap line shown" is not proof of an uncapped key: absent, boolean,
        # negative and unparseable limits must stay unknown so the Desktop
        # cannot tint a funded wallet green from a malformed /key payload.
        mod = self.module()
        cases = [
            ({"limit": None}, "uncapped"),
            ({"limit": 0}, "capped"),
            ({"limit": 5}, "capped"),
            ({}, "unknown"),
            ({"limit": True}, "unknown"),
            ({"limit": False}, "unknown"),
            ({"limit": -1}, "unknown"),
            ({"limit": "not-a-number"}, "unknown"),
            ({"limit": [5]}, "unknown"),
        ]
        for data, expected in cases:
            with self.subTest(data=data), mock.patch.object(
                    mod, "_get_json", return_value=({"data": data}, None)):
                _, _, limit_state = mod._key_result("Key 1", "FAKE_A")
            self.assertEqual(limit_state, expected)

    def test_malformed_payloads_are_safe(self):
        mod = self.module()
        for payload in (None, [], {}, {"data": []}, {"data": {"limit": True, "usage": "NaN"}}, {"data": {"label": "PRIVATE_SECRET"}}):
            with self.subTest(payload=payload), mock.patch.object(mod, "_get_json", return_value=(payload, None)):
                windows, details, limit_state = mod._key_result("Key 1", "FAKE_A")
            self.assertEqual(windows, [])
            self.assertEqual(details, ["Key 1: unavailable (parse-pending)"])
            self.assertEqual(limit_state, "unknown")

    def test_http_errors_are_safe_and_redirects_are_blocked(self):
        mod = self.module()
        for exc, reason in [(HTTPError("https://test", 401, "PRIVATE_SECRET", Message(), None), "auth-failed"),
                            (HTTPError("https://test", 429, "PRIVATE_SECRET", Message(), None), "http-429"),
                            (TimeoutError("PRIVATE_SECRET"), "timeout"), (RuntimeError("PRIVATE_SECRET"), "fetch-error")]:
            with self.subTest(reason=reason), mock.patch.object(mod, "_urlopen", side_effect=exc):
                self.assertEqual(mod._get_json("https://openrouter.ai/api/v1/key", "FAKE_A"), (None, reason))
        from quota_providers.base import NoRedirectHandler
        self.assertIsNone(NoRedirectHandler().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_http_request_and_body_limits(self):
        mod = self.module()
        for body, expected in [(b'{"data": {}}', ({"data": {}}, None)), (b'PRIVATE_SECRET', (None, "bad-json")), (b'x' * (1024 * 1024 + 1), (None, "response-too-large"))]:
            with mock.patch.object(mod, "_urlopen", return_value=BytesIO(body)) as get:
                self.assertEqual(mod._get_json("https://openrouter.ai/api/v1/key", "FAKE_A"), expected)
            request = get.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer FAKE_A")
            self.assertLessEqual(get.call_args.kwargs["timeout"], 15)

    def test_amount_validation(self):
        mod = self.module()
        for value in (True, "NaN", "Infinity", [], "1e999999", "1e999999999", "1e-999999", "x" * 100):
            self.assertIsNone(mod._amount(value))


@unittest.skipUnless(shutil.which("node"), "Node.js is required for widget render tests")
class OpenRouterWidgetTests(unittest.TestCase):
    def test_details_visible_in_clean_and_dense_only_for_openrouter(self):
        # Render the real ProviderRow through the shared offline harness, so the
        # helpers it calls are shipped code rather than per-test shims.
        detail = "Account wallet via Key 1; other account membership unverified"
        for mode in ("clean", "dense"):
            for provider_id in ("openrouter", "nous"):
                for windows in ([], [{"label": "Key quota", "used_percent": 50}]):
                    with self.subTest(mode=mode, provider=provider_id, windows=bool(windows)):
                        tree = render(component="row", mode=mode, id=provider_id,
                                      provider={"windows": windows, "details": [detail]})
                        self.assertEqual(detail in text(tree),
                                         provider_id == "openrouter" or mode == "dense")

    def test_all_keys_failed_keeps_failure_lines_visible(self):
        # A total failure must not collapse into a bare "unavailable (no-data)":
        # the per-key and wallet errors are what explain the state.
        details = [
            "Locally configured credentials: 2 (deduplicated); not an inventory of all account keys",
            "Key 1 (native): unavailable (http-429)",
            "Key 2 (saved): unavailable (timeout)",
            "Account credits unavailable via Key 1 (native) (timeout)",
        ]
        provider = {"windows": [], "details": details, "unavailable_reason": "no-data"}
        for mode in ("clean", "dense"):
            with self.subTest(mode=mode):
                rendered = text(render(component="row", mode=mode, id="openrouter",
                                       provider=provider))
                self.assertIn("unavailable (no-data)", rendered)
                for line in details:
                    self.assertIn(line, rendered)

    def test_single_capped_key_clean_keeps_cap_and_hides_usage(self):
        # Mirrors the fetcher's single-key result: account_balances carries the
        # wallet, details carry label-free cap/reset/usage lines.
        provider = {
            "windows": [{"label": "Key quota", "used_percent": 60}],
            "details": [
                "Key cap USD 5.00; remaining USD 2.00; resets monthly",
                "Usage total USD 6.0000",
                "Usage today UTC USD 1.0000",
            ],
            "account_balances": [{"currency": "USD", "total_balance": "8.00"}],
            "credential_scope": "single",
            "key_limit_state": "capped",
        }
        clean_tree = render(component="row", mode="clean", id="openrouter",
                            provider=provider)
        clean = text(clean_tree)
        self.assertIn("Account balance", clean)
        self.assertIn("$8.00 USD", clean)
        self.assertIn("Key quota", clean)
        self.assertIn("Key cap USD 5.00; remaining USD 2.00; resets monthly", clean)
        self.assertNotIn("Usage total", clean)
        self.assertNotIn("Usage today", clean)
        for hidden in ("Locally configured credentials", "Management API key",
                       "Key 1 (native)", "Account membership"):
            self.assertNotIn(hidden, clean)
        headline = [
            node for node in nodes(clean_tree)
            if node.get("props", {}).get("style", {}).get("fontSize") == "1.25rem"
        ]
        self.assertEqual(len(headline), 1)

        dense = text(render(component="row", mode="dense", id="openrouter",
                            provider=provider))
        for line in provider["details"]:
            self.assertIn(line, dense)
        self.assertNotIn("Key 1 (native)", dense)

    def test_single_uncapped_key_hides_usage_in_clean_but_keeps_it_dense(self):
        provider = {
            "windows": [],
            "details": ["Usage total USD 2.0000"],
            "account_balances": [{"currency": "USD", "total_balance": "8.25"}],
            "credential_scope": "single",
            "key_limit_state": "uncapped",
        }
        clean = text(render(component="row", mode="clean", id="openrouter",
                            provider=provider))
        self.assertIn("$8.25 USD", clean)
        self.assertNotIn("Usage total", clean)
        self.assertNotIn("Key cap", clean)
        dense = text(render(component="row", mode="dense", id="openrouter",
                            provider=provider))
        self.assertIn("Usage total USD 2.0000", dense)

    def test_single_zero_cap_stays_visible_in_clean_without_usage(self):
        provider = {
            "windows": [],
            "details": [
                "Key cap USD 0.00; remaining USD 0.00 (key cap exhausted; account wallet is separate)",
                "Usage total USD 4.0000",
            ],
            "account_balances": [{"currency": "USD", "total_balance": "0.00"}],
            "credential_scope": "single",
            "key_limit_state": "capped",
        }
        clean = text(render(component="row", mode="clean", id="openrouter",
                            provider=provider))
        self.assertIn("Key cap USD 0.00", clean)
        self.assertIn("key cap exhausted", clean)
        self.assertIn("$0.00 USD", clean)
        self.assertNotIn("Usage total", clean)
        dense = text(render(component="row", mode="dense", id="openrouter",
                            provider=provider))
        self.assertIn("Usage total USD 4.0000", dense)

    def test_scoped_key_usage_stays_visible_in_clean(self):
        # Multi-key lines keep their generated prefix, so the clean-mode
        # single-key usage rule must not hide them.
        provider = {
            "windows": [{"label": "Key 1 (native) quota", "used_percent": 60}],
            "details": [
                "Locally configured credentials: 2 (deduplicated); not an inventory of all account keys",
                "Key 1 (native): key cap USD 5.00; remaining USD 2.00; resets monthly",
                "Key 1 (native): usage total USD 6.0000",
            ],
            "credential_scope": "scoped",
        }
        clean = text(render(component="row", mode="clean", id="openrouter",
                            provider=provider))
        self.assertIn("Locally configured credentials: 2", clean)
        self.assertIn("Key 1 (native): key cap USD 5.00", clean)
        self.assertIn("Key 1 (native): usage total USD 6.0000", clean)

    @staticmethod
    def row_tones(provider, provider_id="openrouter", mode="clean"):
        tree = render(component="row", mode=mode, id=provider_id, provider=provider)
        return [node["props"].get("data-tone") for node in nodes(tree)
                if "data-tone" in node.get("props", {})]

    @staticmethod
    def chip_dot_color(provider, provider_id="openrouter"):
        tree = render(component="chip", id=provider_id, provider=provider)
        dots = [node for node in nodes(tree)
                if "rounded-full" in (node.get("props", {}).get("className") or "")]
        return dots[0]["props"]["style"]["background"] if dots else None

    def test_single_uncapped_positive_wallet_is_good_without_an_availability_claim(self):
        # Regression: the card had no window and no api_calls_available, so a
        # funded, uncapped single key rendered a muted dot and read as unknown.
        provider = {
            "windows": [],
            "details": ["Usage total USD 2.0000"],
            "account_balances": [{"currency": "USD", "total_balance": "8.25"}],
            "credential_scope": "single",
            "key_limit_state": "uncapped",
        }
        for mode in ("clean", "dense"):
            with self.subTest(mode=mode):
                self.assertEqual(self.row_tones(provider, mode=mode), ["good"])
        self.assertEqual(self.chip_dot_color(provider), "var(--ui-green)")
        rendered = text(render(component="row", mode="clean", id="openrouter",
                               provider=provider))
        self.assertIn("$8.25 USD", rendered)
        self.assertNotIn("API calls available", rendered)

    def test_wallet_only_uncapped_key_is_good_too(self):
        # /key can return no cap and no usage; the wallet is the whole record.
        provider = {
            "windows": [],
            "details": [],
            "account_balances": [{"currency": "USD", "total_balance": "0.0040"}],
            "credential_scope": "single",
            "key_limit_state": "uncapped",
        }
        self.assertEqual(self.row_tones(provider), ["good"])

    def test_zero_and_negative_wallets_do_not_become_good(self):
        for value in ("0", "0.00", "-1.25"):
            with self.subTest(balance=value):
                provider = {
                    "windows": [],
                    "details": ["Usage total USD 2.0000"],
                    "account_balances": [{"currency": "USD", "total_balance": value}],
                    "credential_scope": "single",
                    "key_limit_state": "uncapped",
                }
                self.assertEqual(self.row_tones(provider), ["muted"])
                self.assertEqual(self.chip_dot_color(provider), "var(--ui-stroke-tertiary)")

    def test_exhausted_zero_cap_stays_muted_despite_a_funded_wallet(self):
        # A zero key cap blocks this key even though the account wallet has
        # money: the cap line keeps its existing muted status and stays visible.
        provider = {
            "windows": [],
            "details": [
                "Key cap USD 0.00; remaining USD 0.00 (key cap exhausted; account wallet is separate)",
                "Usage total USD 4.0000",
            ],
            "account_balances": [{"currency": "USD", "total_balance": "8.00"}],
            "credential_scope": "single",
            "key_limit_state": "capped",
        }
        self.assertEqual(self.row_tones(provider), ["muted"])
        self.assertEqual(self.chip_dot_color(provider), "var(--ui-stroke-tertiary)")
        self.assertIn("Key cap USD 0.00", text(render(
            component="row", mode="clean", id="openrouter", provider=provider)))

    def test_capped_key_without_a_known_remaining_stays_muted(self):
        provider = {
            "windows": [],
            "details": ["Key cap USD 5.00"],
            "account_balances": [{"currency": "USD", "total_balance": "8.00"}],
            "credential_scope": "single",
            "key_limit_state": "capped",
        }
        self.assertEqual(self.row_tones(provider), ["muted"])

    def test_key_failure_beside_a_funded_wallet_stays_muted(self):
        provider = {
            "windows": [],
            "details": ["Unavailable (timeout)"],
            "account_balances": [{"currency": "USD", "total_balance": "8.00"}],
            "credential_scope": "single",
            "key_limit_state": "unknown",
        }
        self.assertEqual(self.row_tones(provider), ["muted"])

    def test_legacy_record_without_structured_evidence_stays_muted(self):
        # An older cache has details and a balance but neither structured
        # field. It must not be mistaken for a confirmed uncapped key.
        provider = {
            "windows": [],
            "details": ["Usage total USD 2.0000"],
            "account_balances": [{"currency": "USD", "total_balance": "8.25"}],
        }
        self.assertEqual(self.row_tones(provider), ["muted"])
        self.assertEqual(self.chip_dot_color(provider), "var(--ui-stroke-tertiary)")

    def test_scoped_record_without_its_scope_detail_stays_muted(self):
        # The old fallback inferred "single-key" from the absence of the
        # "Locally configured credentials" line. A scoped error/cap record that
        # lost that line (old cache, truncation, hand edit) fell through to a
        # green funded wallet. Structured scope must keep it muted.
        provider = {
            "windows": [],
            "details": [
                "Key 1 (native): key cap USD 5.00; remaining USD 2.00",
                "Key 2 (saved): unavailable (timeout)",
            ],
            "account_balances": [{"currency": "USD", "total_balance": "8.00"}],
            "credential_scope": "scoped",
        }
        self.assertEqual(self.row_tones(provider), ["muted"])
        self.assertEqual(self.chip_dot_color(provider), "var(--ui-stroke-tertiary)")

    def test_error_and_multi_key_states_keep_their_muted_tone(self):
        errored = {
            "windows": [],
            "details": ["Account credits unavailable (http-429)"],
            "unavailable_reason": "no-data",
            "account_balances": [],
        }
        self.assertEqual(self.row_tones(errored), [])
        self.assertEqual(self.chip_dot_color(errored), "var(--ui-stroke-tertiary)")
        multi = {
            "windows": [{"label": "Key 1 (native) quota", "used_percent": 50}],
            "details": [
                "Locally configured credentials: 2 (deduplicated); not an inventory of all account keys",
                "Account wallet via Key 1 (native): USD 8.00 remaining",
                "Key 1 (native): key cap USD 5.00; remaining USD 2.00",
                "Key 2 (saved): no key-specific cap (account wallet still applies)",
            ],
            "account_balances": [],
            "credential_scope": "scoped",
        }
        # The one window still drives its normal percentage tone.
        self.assertEqual(self.row_tones(multi), ["good"])
        no_window_multi = dict(multi, windows=[])
        self.assertEqual(self.row_tones(no_window_multi), ["muted"])
        # Even a malformed cached record that pairs scoped details with a
        # balance must not fall into the single-key funded-wallet tone.
        mixed = dict(no_window_multi, account_balances=[
            {"currency": "USD", "total_balance": "8.00"}])
        self.assertEqual(self.row_tones(mixed), ["muted"])

    def test_reported_api_unavailability_wins_over_a_funded_wallet(self):
        # A provider-reported false is a stronger fact than any balance.
        provider = {
            "windows": [],
            "details": [],
            "account_balances": [{"currency": "USD", "total_balance": "8.00"}],
            "credential_scope": "single",
            "key_limit_state": "uncapped",
            "api_calls_available": False,
        }
        self.assertEqual(self.row_tones(provider), ["bad"])
        self.assertEqual(self.chip_dot_color(provider), "var(--ui-red)")

    def test_generic_balance_only_provider_stays_muted(self):
        # The wallet fallback is OpenRouter-specific: a plain balance-only
        # provider keeps the existing "unknown availability" muted dot.
        provider = {
            "windows": [],
            "details": [],
            "account_balances": [{"currency": "USD", "total_balance": "12.00"}],
            "credential_scope": "single",
            "key_limit_state": "uncapped",
        }
        self.assertEqual(self.row_tones(provider, provider_id="deepseek"), ["muted"])


class OpenRouterCacheToWidgetToneTests(unittest.TestCase):
    """Real fetcher -> real cache serializer -> real widget renderer.

    The Desktop reads the cache record, not the in-memory ``QuotaResult``, so
    the structured tone evidence must survive serialization. These tests patch
    only the OpenRouter HTTP boundary and import the plugin as one package
    instance, so the fetcher's ``QuotaResult`` is the same class the cache
    serializes.
    """

    PACKAGE = "openrouter_cache_integration"

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("Node.js is required for widget render tests")
        stub = types.ModuleType("hermes_constants")
        setattr(stub, "get_hermes_home", lambda: ROOT)
        pkg = types.ModuleType(cls.PACKAGE)
        pkg.__path__ = [str(ROOT)]
        with mock.patch.dict(sys.modules, {cls.PACKAGE: pkg, "hermes_constants": stub}):
            cls.fetcher = importlib.import_module(cls.PACKAGE + ".quota_providers.openrouter")
            cls.cache = importlib.import_module(cls.PACKAGE + ".quota_cache")

    def fetch_and_cache(self, get, credentials):
        with mock.patch.object(self.fetcher, "_resolve_credentials", return_value=credentials), \
                mock.patch.object(self.fetcher, "_get_json", side_effect=get):
            result = self.fetcher.fetch_openrouter_quota()
        # The real fail-open cache boundary, not a test-local copy of the
        # schema. The widget reads quota_cache.json, so round-trip the record
        # through JSON too: no in-memory-only Python value can pass this.
        record = self.cache._fetch_one("openrouter", lambda: result)
        return result, json.loads(json.dumps(record))

    @staticmethod
    def record_tones(record, provider_id="openrouter"):
        tree = render(component="row", mode="clean", id=provider_id, provider=record)
        return [node["props"].get("data-tone") for node in nodes(tree)
                if "data-tone" in node.get("props", {})]

    @staticmethod
    def record_chip_color(record, provider_id="openrouter"):
        tree = render(component="chip", id=provider_id, provider=record)
        dots = [node for node in nodes(tree)
                if "rounded-full" in (node.get("props", {}).get("className") or "")]
        return dots[0]["props"]["style"]["background"] if dots else None

    @staticmethod
    def wallet(total_credits, total_usage):
        return {"data": {"total_credits": total_credits, "total_usage": total_usage}}

    def test_valid_null_limit_reaches_the_widget_as_good(self):
        def get(url, key):
            if url.endswith("/credits"):
                return self.wallet(20, 12), None
            return {"data": {"limit": None, "usage": 2}}, None
        _, record = self.fetch_and_cache(get, [("Key 1 (native)", "FAKE_A")])
        self.assertEqual((record["credential_scope"], record["key_limit_state"]),
                         ("single", "uncapped"))
        self.assertEqual([(b["currency"], b["total_balance"])
                          for b in record["account_balances"]], [("USD", "8.00")])
        self.assertEqual(self.record_tones(record), ["good"])
        self.assertEqual(self.record_chip_color(record), "var(--ui-green)")
        rendered = text(render(component="row", mode="clean", id="openrouter",
                               provider=record))
        self.assertIn("$8.00 USD", rendered)
        self.assertNotIn("API calls available", rendered)

    def test_missing_or_malformed_limit_never_greens(self):
        # The regression: any /key payload without a displayed cap line used to
        # read as "uncapped". Only an explicit null limit may do that.
        cases = (
            ("absent", {"data": {"usage": 2}}),
            ("boolean", {"data": {"limit": True, "usage": 2}}),
            ("negative", {"data": {"limit": -5, "usage": 2}}),
            ("unparseable", {"data": {"limit": "many", "usage": 2}}),
        )
        for what, key_payload in cases:
            with self.subTest(case=what):
                def get(url, key, payload=key_payload):
                    if url.endswith("/credits"):
                        return self.wallet(20, 12), None
                    return payload, None
                result, record = self.fetch_and_cache(
                    get, [("Key 1 (native)", "FAKE_A")])
                self.assertEqual(result.key_limit_state, "unknown")
                self.assertEqual(record["key_limit_state"], "unknown")
                self.assertEqual(record["credential_scope"], "single")
                self.assertEqual(self.record_tones(record), ["muted"])
                self.assertEqual(self.record_chip_color(record),
                                 "var(--ui-stroke-tertiary)")

    def test_zero_cap_reaches_the_widget_as_muted_with_the_cap_visible(self):
        def get(url, key):
            if url.endswith("/credits"):
                return self.wallet(20, 12), None
            return {"data": {"limit": 0, "limit_remaining": 0, "usage": 4}}, None
        _, record = self.fetch_and_cache(get, [("Key 1 (saved)", "FAKE_B")])
        self.assertEqual(record["key_limit_state"], "capped")
        self.assertEqual(self.record_tones(record), ["muted"])
        rendered = text(render(component="row", mode="clean", id="openrouter",
                               provider=record))
        self.assertIn("Key cap USD 0.00", rendered)
        self.assertIn("$8.00 USD", rendered)
        self.assertNotIn("Key quota", rendered)

    def test_key_failure_with_a_funded_wallet_reaches_the_widget_as_muted(self):
        def get(url, key):
            if url.endswith("/credits"):
                return self.wallet(20, 12), None
            return None, "timeout"
        _, record = self.fetch_and_cache(get, [("Key 1 (native)", "FAKE_A")])
        self.assertEqual(record["key_limit_state"], "unknown")
        self.assertEqual(record["details"], ["Unavailable (timeout)"])
        self.assertEqual(self.record_tones(record), ["muted"])
        self.assertEqual(self.record_chip_color(record),
                         "var(--ui-stroke-tertiary)")

    def test_wallet_failure_leaves_no_funded_tone(self):
        # Confirmed uncapped key but no wallet amount: there is no balance to
        # tint, and the failure line must keep the card muted.
        def get(url, key):
            if url.endswith("/credits"):
                return None, "http-429"
            return {"data": {"limit": None, "usage": 2}}, None
        _, record = self.fetch_and_cache(get, [("Key 1 (native)", "FAKE_A")])
        self.assertEqual((record["credential_scope"], record["key_limit_state"]),
                         ("single", "uncapped"))
        self.assertEqual(record["account_balances"], [])
        self.assertEqual(self.record_tones(record), ["muted"])
        self.assertIn("Account credits unavailable (http-429)", record["details"])

    def test_scoped_malformed_key_stays_muted_without_the_scope_detail(self):
        def get(url, key):
            if url.endswith("/credits"):
                return self.wallet(20, 12), None
            if key == "FAKE_A":
                # A scoped zero cap: a real cap with no percentage window.
                return {"data": {"limit": 0, "limit_remaining": 0, "usage": 3}}, None
            return {"data": {"limit": True, "usage": 3}}, None
        _, record = self.fetch_and_cache(
            get, [("Key 1 (native)", "FAKE_A"), ("Key 2 (saved)", "FAKE_B")])
        self.assertEqual((record["credential_scope"], record["key_limit_state"]),
                         ("scoped", None))
        # A scoped cache record that lost its scope and wallet details (older
        # cache, a hand edit, truncation) and was handed a balance must still
        # stay muted.
        mutated = dict(record)
        mutated["details"] = [
            d for d in record["details"]
            if not d.startswith(("Locally configured credentials", "Account wallet"))
        ]
        mutated["account_balances"] = [
            {"currency": "USD", "total_balance": "8.00"}]
        self.assertEqual(self.record_tones(mutated), ["muted"])
        # A pre-field cache record has neither structured key at all.
        legacy = {k: v for k, v in mutated.items()
                  if k not in ("credential_scope", "key_limit_state")}
        self.assertEqual(self.record_tones(legacy), ["muted"])
        self.assertEqual(self.record_chip_color(legacy),
                         "var(--ui-stroke-tertiary)")


if __name__ == "__main__":
    unittest.main()
