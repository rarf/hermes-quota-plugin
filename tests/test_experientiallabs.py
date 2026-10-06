"""Offline Experiential Labs account balance + recent usage contracts.

Synthetic data only; no network access.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from decimal import Decimal
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class ExperientialLabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Register the module via the init import
        import quota_providers  # noqa: F401

    def module(self):
        import quota_providers
        self.assertIn("experientiallabs", quota_providers.PROVIDER_FETCHERS)
        return importlib.import_module("quota_providers.experientiallabs")

    def credits_payload(self):
        return {"data": {"total_credits": "100.00", "total_usage": "25.50"}}

    def usage_payload(self):
        return {
            "data": [
                {
                    "model": "gpt-6-astra",
                    "provider": "openai",
                    "real_cost_usd": "0.0123",
                    "cost_usd": "0.0123",
                    "estimated_cost_usd": "0.0123",
                    "pricing_known": True,
                    "input_tokens": 500,
                    "output_tokens": 120,
                    "cached_input_tokens": 200,
                    "reasoning_tokens": 0,
                    "created_at": "2026-09-30T10:00:00Z",
                    "id": "gen-req_abc123",
                    "attribution_label": None,
                },
                {
                    "model": "claude-fable-5.1",
                    "provider": "anthropic",
                    "real_cost_usd": "0.0450",
                    "cost_usd": "0.0450",
                    "estimated_cost_usd": "0.0450",
                    "pricing_known": True,
                    "input_tokens": 1000,
                    "output_tokens": 300,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "created_at": "2026-09-30T09:45:00Z",
                    "id": "gen-req_def456",
                    "attribution_label": "user-email@example.com",
                },
            ]
        }

    def _run_env_fetch(self, mod, env_key, env_value):
        """Fetch with resolve_api_key patched to return a key via env scan."""
        with mock.patch.dict("os.environ", {env_key: env_value}), mock.patch.object(
            mod, "_get_json"
        ) as get:
            # Make _get_json return credits payload the first call, usage the second
            def side_effect(url, secret, timeout=None):
                if "/credits" in url:
                    return self.credits_payload(), None
                if "/usage" in url:
                    return self.usage_payload(), None
                return None, "fetch-error"

            get.side_effect = side_effect
            result = mod.fetch_experientiallabs_quota()
            self.assertNotIn(env_value, repr(result))
            self.assertNotIn("SYNTHETIC", repr(result))
            return result, get

    def test_registered_without_taking_over_another_provider(self):
        mod = self.module()
        from quota_providers import PROVIDER_FETCHERS

        self.assertEqual(
            PROVIDER_FETCHERS["experientiallabs"].__module__,
            "quota_providers.experientiallabs",
        )
        self.assertNotEqual(
            PROVIDER_FETCHERS["deepseek"].__module__,
            "quota_providers.experientiallabs",
        )
        self.assertIn("deepseek", PROVIDER_FETCHERS)

    def test_account_balance_from_credits(self):
        result, get = self._run_env_fetch(self.module(), "EXPLABS_API_KEY", "xpl_" + "a" * 40)
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.label, "experientiallabs")
        self.assertEqual(result.windows, [])
        self.assertIsNone(result.api_calls_available)
        total_balances = [(b.currency, b.total_balance) for b in result.account_balances]
        self.assertEqual(len(total_balances), 1)
        self.assertEqual(total_balances[0][0], "USD")
        # total_balance stores remaining (total_credits - total_usage = 100.00 - 25.50)
        self.assertEqual(Decimal(total_balances[0][1]), Decimal("74.50"))
        self.assertTrue(result.has_data())
        self.assertIn("Remaining USD", result.details[0])
        self.assertIn("25.50", result.details[0])  # total_usage in detail

    def test_recent_usage_in_details(self):
        result, get = self._run_env_fetch(self.module(), "EXPLABS_API_KEY", "xpl_" + "a" * 40)
        # Usage list items should appear
        usage_lines = [d for d in result.details if "gpt-6-astra" in d or "claude-fable-5.1" in d]
        self.assertEqual(len(usage_lines), 2)
        # attribution_label must NOT appear (PII avoidance)
        for line in usage_lines:
            self.assertNotIn("user-email", line)
            self.assertNotIn("email", line)
        # Sum recent should be there
        sum_lines = [d for d in result.details if "Sum recent" in d]
        self.assertGreaterEqual(len(sum_lines), 1)

    def test_credits_unavailable_does_not_block_recent_usage(self):
        mod = self.module()
        with mock.patch.dict("os.environ", {"EXPLABS_API_KEY": "xpl_" + "b" * 40}), mock.patch.object(
            mod, "_get_json"
        ) as get:

            def side_effect(url, secret, timeout=None):
                if "/credits" in url:
                    return None, "auth-failed"
                if "/usage" in url:
                    return self.usage_payload(), None
                return None, "fetch-error"

            get.side_effect = side_effect
            result = mod.fetch_experientiallabs_quota()
        # has_data because usage succeeded
        self.assertTrue(result.has_data())
        self.assertIn("Credits: unavailable (auth-failed)", result.details)
        self.assertIn("gpt-6-astra", "\n".join(result.details))

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_missing_credentials_never_fetch(self):
        mod = self.module()
        # The module's resolve_api_key scans environment; clear the env
        # and assert no-credentials without network
        with mock.patch.object(mod, "resolve_api_key", return_value=None), mock.patch.object(
            mod, "_get_json"
        ) as get:
            result = mod.fetch_experientiallabs_quota()
            self.assertEqual(result.unavailable_reason, "no-credentials")
            get.assert_not_called()

    def test_fallback_env_var_works(self):
        """EXPLABS_API_KEY should work as the primary env var."""
        result, get = self._run_env_fetch(self.module(), "EXPLABS_API_KEY", "xpl_" + "c" * 40)
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(result.has_data())

    def test_malformed_credits_fails_open_parse_pending(self):
        mod = self.module()
        for payload in (None, {}, {"data": None}, {"data": {"total_credits": None, "total_usage": None}}):
            with self.subTest(payload=payload):
                with mock.patch.dict("os.environ", {"EXPLABS_API_KEY": "xpl_d"}), mock.patch.object(
                    mod, "_get_json", return_value=(payload, None)
                ):
                    result = mod.fetch_experientiallabs_quota()
                    # If both credits and usage fail, we get no-data
                    self.assertTrue(
                        result.unavailable_reason in ("no-data", None) or "parse-pending" in str(result.details)
                    )

    def test_errors_are_sanitized(self):
        mod = self.module()
        # credential-resolution failure -> no-credentials
        with mock.patch.object(mod, "resolve_api_key", side_effect=RuntimeError("SECRET")):
            result = mod.fetch_experientiallabs_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")
        self.assertNotIn("SECRET", repr(result))

        # fetch error on both endpoints -> fetch-error
        with mock.patch.object(mod, "resolve_api_key", return_value="xpl_secret"), mock.patch.object(
            mod, "_get_json", side_effect=RuntimeError("SECRET")
        ):
            result = mod.fetch_experientiallabs_quota()
        self.assertEqual(result.unavailable_reason, "fetch-error")
        self.assertNotIn("SECRET", repr(result))

        # specific HTTP errors — both endpoints fail independently so the
        # overall result is no-data; the key contract is that secrets are gone.
        for reason in ("auth-failed", "http-429", "timeout", "bad-json"):
            with self.subTest(reason=reason):
                with mock.patch.object(mod, "resolve_api_key", return_value="xpl_secret"), mock.patch.object(
                    mod, "_get_json", return_value=(None, reason)
                ):
                    result = mod.fetch_experientiallabs_quota()
                self.assertEqual(result.unavailable_reason, "no-data")
                self.assertNotIn("SECRET", repr(result))
                self.assertNotIn("xpl_secret", repr(result))

    def test_resolve_api_key_env_and_fallback(self):
        mod = self.module()
        # When no key is in env, resolve_api_key returns None
        with mock.patch.dict("os.environ", {}, clear=True):
            key = mod.resolve_api_key()
            self.assertIsNone(key)

        # When EXPLABS_API_KEY is set, it returns that
        with mock.patch.dict("os.environ", {"EXPLABS_API_KEY": "xpl_secret"}):
            self.assertEqual(mod.resolve_api_key(), "xpl_secret")

    def test_malformed_token_does_not_kill_balance(self):
        """A non-numeric token field costs that detail line, not the wallet."""
        mod = self.module()
        usage_with_bad_tokens = {
            "data": [
                {
                    "model": "gpt-6-astra",
                    "real_cost_usd": "0.0123",
                    "input_tokens": "NaN",
                    "output_tokens": 120,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "created_at": "2026-09-30T10:00:00Z",
                },
                {
                    "model": "claude-fable-5.1",
                    "real_cost_usd": "0.0450",
                    "input_tokens": 1000,
                    "output_tokens": 300,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "created_at": "2026-09-30T09:45:00Z",
                },
            ]
        }
        with mock.patch.dict("os.environ", {"EXPLABS_API_KEY": "xpl_a" * 10}), mock.patch.object(
            mod, "_get_json"
        ) as get:

            def side_effect(url, secret, timeout=None):
                if "/credits" in url:
                    return {"data": {"total_credits": "10.00", "total_usage": "3.00"}}, None
                if "/usage" in url:
                    return usage_with_bad_tokens, None
                return None, "fetch-error"

            get.side_effect = side_effect
            result = mod.fetch_experientiallabs_quota()

        # Balance is preserved (7 USD remaining) — no fetch-error
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(result.has_data())
        # detail for the row with bad tokens omits the token count
        text = "\n".join(result.details)
        self.assertIn("Remaining USD 7.00", text)
        self.assertIn("gpt-6-astra", text)
        self.assertIn("claude-fable-5.1", text)
        self.assertNotIn("NaN", text)

    def test_timeout_propagated_to_get_json(self):
        """The deadline-sliced timeout is passed as the third arg to get_json."""
        mod = self.module()
        captured_timeouts = []

        def side_effect(url, secret, timeout=None):
            captured_timeouts.append(timeout)
            if "/credits" in url:
                return {"data": {"total_credits": "50.00", "total_usage": "10.00"}}, None
            if "/usage" in url:
                return {"data": []}, None
            return None, "fetch-error"

        with mock.patch.dict("os.environ", {"EXPLABS_API_KEY": "xpl_b" * 10}), mock.patch.object(
            mod, "_get_json", side_effect=side_effect
        ):
            mod.fetch_experientiallabs_quota()

        self.assertGreaterEqual(len(captured_timeouts), 1)
        for t in captured_timeouts:
            self.assertIsNotNone(t)
            self.assertLessEqual(t, 7.0)

    def test_no_secrets_or_ids_in_output(self):
        """attribution_label, request IDs, and raw env values must never appear."""
        mod = self.module()
        usage_payload = {
            "data": [
                {
                    "model": "gpt-4",
                    "real_cost_usd": "0.10",
                    "id": "gen-req_should_not_leak",
                    "attribution_label": "secret-user@evilcorp.com",
                    "created_at": "2026-09-30T10:00:00Z",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                }
            ]
        }
        with mock.patch.dict("os.environ", {"EXPLABS_API_KEY": "xpl_" + "e" * 40}), mock.patch.object(
            mod, "_get_json"
        ) as get:

            def side_effect(url, secret, timeout=None):
                if "/credits" in url:
                    return self.credits_payload(), None
                if "/usage" in url:
                    return usage_payload, None
                return None, "fetch-error"

            get.side_effect = side_effect
            result = mod.fetch_experientiallabs_quota()
        text = "\n".join(result.details)
        self.assertNotIn("secret-user", text)
        self.assertNotIn("evilcorp", text)
        self.assertNotIn("gen-req_should_not_leak", text)
        self.assertNotIn("xpl_", text)


if __name__ == "__main__":
    unittest.main()