"""Independent-review provenance findings, pinned as tests (offline).

Findings this file locks down:

  1. Codex: a free/plan-less account still returns an allowance window, but the
     backend did not report a paid plan, so the record must not be labelled
     ``subscription`` (the meter may still say it is an allowance).
  2. Provenance is a *closed vocabulary*: an out-of-vocabulary
     ``plan_source`` / ``billing_mode`` / ``meter`` is normalized to ``unknown``
     at serialization AND at read/consumer time, so a hand-edited or stale cache
     cannot smuggle a claim as status (e.g. ``verified-by-email`` shown as a
     verified plan).
  3. The serializer enforces the no-email invariant for ``account`` too, not
     only the provider-side constructor.
  4. OpenRouter key caps / usage details are NOT evidence of pay-as-you-go
     money; only the wallet (credits endpoint) figures are.
  5. The primary Claude plan is matched against the *same* credential the usage
     request used (a single resolve), never a re-resolved token.

Offline only: no network, no real credentials.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from widget_harness import render, text  # noqa: E402

_stub = types.ModuleType("hermes_constants")
_stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME") or tempfile.gettempdir())
sys.modules.setdefault("hermes_constants", _stub)

if "quota_plugin_under_test" not in sys.modules:
    _pkg = types.ModuleType("quota_plugin_under_test")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["quota_plugin_under_test"] = _pkg

base = importlib.import_module("quota_plugin_under_test.quota_providers.base")
builtin = importlib.import_module("quota_plugin_under_test.quota_providers.builtin")
qc = importlib.import_module("quota_plugin_under_test.quota_cache")
commands = importlib.import_module("quota_plugin_under_test.commands")
openrouter = importlib.import_module("quota_plugin_under_test.quota_providers.openrouter")


class ExtraAccountCredentialMatchTests(unittest.TestCase):
    def test_changed_extra_credential_does_not_supply_another_plan(self):
        with mock.patch.object(builtin, "_read_claude_account_token", return_value=("request-token", None)), \
             mock.patch.object(builtin, "_request_anthropic_usage", return_value=({"five_hour": {"utilization": 10}}, None)), \
             mock.patch.object(builtin, "_read_claude_credentials_json", return_value={"claudeAiOauth": {"accessToken": "different-token", "subscriptionType": "enterprise"}}):
            rows = builtin._claude_account_results(
                [{"id": "work", "label": "Work", "config_dir": "/synthetic/work"}],
                primary_token=None, deadline=base.Deadline(10),
            )
        self.assertIsNone(rows[0].plan)
        self.assertEqual(rows[0].provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)


class CodexBillingReviewTests(unittest.TestCase):
    def test_planless_windows_do_not_claim_a_paid_subscription(self):
        res = builtin._parse_codex_payload({"rate_limit": {
            "primary_window": {"used_percent": 10, "limit_window_seconds": 18000}}})
        # A window exists (an allowance is measured)...
        self.assertTrue(res.windows)
        self.assertEqual(res.provenance.meter, base.METER_UNKNOWN)
        # ...but the backend reported no plan, so no paid-subscription label.
        self.assertIsNone(res.plan)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)
        self.assertEqual(res.provenance.billing_mode, base.BILLING_UNKNOWN)

    def test_free_and_unrecognized_plans_do_not_claim_paid_subscription(self):
        for plan in ("free", "unknown-future-plan"):
            with self.subTest(plan=plan):
                res = builtin._parse_codex_payload({
                    "plan_type": plan,
                    "rate_limit": {"primary_window": {"used_percent": 10}},
                })
                self.assertEqual(res.provenance.billing_mode, base.BILLING_UNKNOWN)
                self.assertEqual(res.provenance.meter, base.METER_UNKNOWN)

    def test_reported_plan_windows_are_subscription(self):
        res = builtin._parse_codex_payload({
            "plan_type": "plus",
            "rate_limit": {"primary_window": {"used_percent": 10}},
        })
        self.assertEqual(res.provenance.billing_mode, base.BILLING_SUBSCRIPTION)


class SerializerVocabularyReviewTests(unittest.TestCase):
    def test_out_of_vocabulary_plan_source_normalizes_to_unknown(self):
        d = base.provenance_to_dict(base.Provenance(plan_source="verified-by-email"))
        self.assertEqual(d["plan_source"], base.PLAN_SOURCE_UNKNOWN)

    def test_out_of_vocabulary_billing_mode_normalizes_to_unknown(self):
        d = base.provenance_to_dict(base.Provenance(billing_mode="free-tier"))
        self.assertEqual(d["billing_mode"], base.BILLING_UNKNOWN)

    def test_out_of_vocabulary_meter_normalizes_to_unknown(self):
        d = base.provenance_to_dict(base.Provenance(meter="tokens"))
        self.assertEqual(d["meter"], base.METER_UNKNOWN)

    def test_serializer_enforces_no_email_account(self):
        d = base.provenance_to_dict(base.Provenance(account="me@example.com"))
        self.assertIsNone(d["account"])

    def test_from_dict_normalizes_out_of_vocabulary_values(self):
        prov = base.provenance_from_dict({
            "plan_source": "verified-by-email",
            "billing_mode": "free-tier",
            "meter": "tokens",
        })
        self.assertEqual(prov.plan_source, base.PLAN_SOURCE_UNKNOWN)
        self.assertEqual(prov.billing_mode, base.BILLING_UNKNOWN)
        self.assertEqual(prov.meter, base.METER_UNKNOWN)

    def test_from_dict_keeps_known_values(self):
        prov = base.provenance_from_dict({
            "plan_source": base.PLAN_SOURCE_PROVIDER_RESPONSE,
            "billing_mode": base.BILLING_SUBSCRIPTION,
            "meter": base.METER_SUBSCRIPTION_ALLOWANCE,
        })
        self.assertEqual(prov.plan_source, base.PLAN_SOURCE_PROVIDER_RESPONSE)
        self.assertEqual(prov.billing_mode, base.BILLING_SUBSCRIPTION)
        self.assertEqual(prov.meter, base.METER_SUBSCRIPTION_ALLOWANCE)


class CacheReadNormalizationReviewTests(unittest.TestCase):
    def test_expand_accounts_normalizes_unsupported_plan_source(self):
        record = {
            "label": "anthropic",
            "plan": None,
            "unavailable_reason": None,
            "details": [],
            "windows": [],
            "provenance": {"plan_source": "verified-by-email",
                           "billing_mode": "subscription", "meter": "unknown"},
            "accounts": [{
                "id": "work", "label": "Work", "plan": "Team",
                "unavailable_reason": None, "details": [],
                "windows": [{"label": "Current week", "used_percent": 5.0}],
                "provenance": {"plan_source": "verified-by-email",
                               "billing_mode": "subscription"},
            }],
        }
        rows = qc._expand_accounts("anthropic", record)
        row = rows["anthropic:work"]
        self.assertEqual(row["provenance"]["plan_source"], base.PLAN_SOURCE_UNKNOWN)

    def test_read_quota_cache_normalizes_an_unsupported_claim(self):
        cache = {
            "fetched_at": "2026-09-01T00:00:00+00:00",
            "providers": {
                "openai-codex": {
                    "label": "openai-codex", "plan": "Pro 100",
                    "unavailable_reason": None, "details": [],
                    "windows": [{"label": "Session", "used_percent": 20.0}],
                    "provenance": {"plan_source": "verified-by-email",
                                   "billing_mode": "subscription",
                                   "meter": "subscription allowance",
                                   "account": "me@example.com"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "quota_cache.json")
            path.write_text(json.dumps(cache), encoding="utf-8")
            with mock.patch.object(qc, "_cache_path", return_value=str(path)):
                loaded = qc.read_quota_cache()
        prov = loaded["providers"]["openai-codex"]["provenance"]
        self.assertEqual(prov["plan_source"], base.PLAN_SOURCE_UNKNOWN)
        self.assertIsNone(prov["account"])


class CliConsumerReviewTests(unittest.TestCase):
    def _render(self, providers):
        with mock.patch.object(commands, "read_quota_cache",
                               return_value={"fetched_at": None, "providers": providers}), \
                mock.patch.object(commands, "quota_cache_age_seconds", return_value=1.0):
            return commands._render_quota(None)

    def test_cli_never_shows_an_unsupported_plan_source(self):
        out = self._render({"openai-codex": {
            "label": "openai-codex",
            "unavailable_reason": None,
            "windows": [{"label": "Session", "used_percent": 20.0}],
            "details": [],
            "plan": "Pro 100",
            "provenance": {"source": "chatgpt.com backend usage",
                           "plan_source": "verified-by-email",
                           "billing_mode": "subscription",
                           "meter": "subscription allowance"},
        }})
        self.assertNotIn("verified-by-email", out)
        # The plan must not be presented with an unverified marker either.
        self.assertNotIn("Pro 100 (", out)

    def test_cli_shows_a_known_plan_source(self):
        out = self._render({"openai-codex": {
            "label": "openai-codex",
            "unavailable_reason": None,
            "windows": [{"label": "Session", "used_percent": 20.0}],
            "details": [],
            "plan": "Pro 200",
            "provenance": {"source": "chatgpt.com backend usage",
                           "plan_source": base.PLAN_SOURCE_PROVIDER_RESPONSE,
                           "billing_mode": base.BILLING_SUBSCRIPTION,
                           "meter": base.METER_SUBSCRIPTION_ALLOWANCE},
        }})
        self.assertIn("Pro 200 (provider-response)", out)


class WidgetConsumerReviewTests(unittest.TestCase):
    def _row(self, provider, pid="openai-codex"):
        return text(render(component="row", id=pid, provider=provider))

    def test_widget_never_shows_an_unsupported_plan_source(self):
        out = self._row({
            "label": "openai-codex",
            "plan": "Pro 100",
            "windows": [{"label": "Session", "used_percent": 20}],
            "details": [],
            "provenance": {"source": "chatgpt.com backend usage endpoint",
                           "plan_source": "verified-by-email",
                           "billing_mode": "subscription",
                           "meter": "subscription allowance"},
        })
        self.assertNotIn("verified-by-email", out)
        self.assertNotIn("Pro 100 (", out)

    def test_widget_shows_a_known_plan_source(self):
        out = self._row({
            "label": "openai-codex",
            "plan": "Pro 100",
            "windows": [{"label": "Session", "used_percent": 20}],
            "details": [],
            "provenance": {"source": "chatgpt.com backend usage endpoint",
                           "plan_source": "provider-response",
                           "billing_mode": "subscription",
                           "meter": "subscription allowance"},
        })
        self.assertIn("Pro 100 (provider-response)", out)


class OpenRouterBillingReviewTests(unittest.TestCase):
    def test_key_caps_without_a_wallet_are_not_pay_as_you_go(self):
        creds = [("Key 1 (native)", "sk-secret")]
        with mock.patch.object(openrouter, "_resolve_credentials", return_value=creds), \
                mock.patch.object(
                    openrouter, "_key_result",
                    return_value=([], ["Key 1 (native): key cap USD 10.00; remaining USD 5.00"])), \
                mock.patch.object(
                    openrouter, "_wallet_result",
                    return_value=(False, "Account credits unavailable via Key 1 (native) (parse-pending)")):
            res = openrouter.fetch_openrouter_quota()
        # A key cap / usage line is not money evidence; only the wallet is.
        self.assertEqual(res.provenance.billing_mode, base.BILLING_UNKNOWN)
        self.assertEqual(res.provenance.meter, base.METER_UNKNOWN)

    def test_wallet_evidence_is_pay_as_you_go(self):
        creds = [("Key 1 (native)", "sk-secret")]
        with mock.patch.object(openrouter, "_resolve_credentials", return_value=creds), \
                mock.patch.object(
                    openrouter, "_key_result",
                    return_value=([], ["Key 1 (native): unavailable (parse-pending)"])), \
                mock.patch.object(
                    openrouter, "_wallet_result",
                    return_value=(True, "Account wallet via Key 1 (native): USD 5.00 remaining")):
            res = openrouter.fetch_openrouter_quota()
        self.assertEqual(res.provenance.billing_mode, base.BILLING_PAY_AS_YOU_GO)
        self.assertEqual(res.provenance.meter, base.METER_CREDITS_BALANCE)


class AnthropicSingleResolveReviewTests(unittest.TestCase):
    """The plan is matched against the credential the request used (one resolve)."""

    def test_primary_token_is_resolved_once_and_used_for_the_request(self):
        calls = []

        def _resolve():
            calls.append(1)
            return "tok-first"

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(builtin, "_core_anthropic_token", side_effect=_resolve), \
                    mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
                    mock.patch.object(builtin, "_request_anthropic_usage",
                                      return_value=({"five_hour": {"utilization": 5}}, None)) as request, \
                    mock.patch.object(builtin, "_effective_claude_config_dir",
                                      return_value=Path(d)), \
                    mock.patch.object(builtin, "_read_macos_claude_keychain_payload",
                                      return_value=None):
                res = builtin._fetch_anthropic_single()

        self.assertEqual(len(calls), 1, "the primary token must be resolved once")
        self.assertEqual(request.call_args.args[0], "tok-first")
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)

    def test_plan_matches_the_request_credential_not_a_second_resolve(self):
        tokens = iter(["tok-request", "tok-other"])

        def _resolve():
            return next(tokens, "tok-other")

        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(json.dumps({
                "claudeAiOauth": {"accessToken": "tok-other", "subscriptionType": "team"}
            }), encoding="utf-8")
            with mock.patch.object(builtin, "_core_anthropic_token", side_effect=_resolve), \
                    mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
                    mock.patch.object(builtin, "_request_anthropic_usage",
                                      return_value=({"five_hour": {"utilization": 5}}, None)), \
                    mock.patch.object(builtin, "_effective_claude_config_dir",
                                      return_value=Path(d)), \
                    mock.patch.object(builtin, "_read_macos_claude_keychain_payload",
                                      return_value=None):
                res = builtin._fetch_anthropic_single()
        # The plan credential (tok-other) is NOT the request credential
        # (tok-request), so no plan may be attached.
        self.assertIsNone(res.plan)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
