"""Account-aware usage provenance contribution.

Pins the normalized provenance vocabulary (source / scope / account / plan
source / billing mode / meter), that it flows through the provider adapters,
the cache serializer and the CLI, and the honesty rules:

  * a plan name is only *verified* when it comes from a provider response or a
    local credential field — never from a user-chosen display label;
  * an email address (EDU or otherwise) is never read as a plan or an account;
  * a subscription allowance is reported distinctly from a pay-as-you-go
    balance/spend;
  * Grok's Build/weekly meter and its fallback per-model chat meter each carry
    their own explicit source/scope, and neither implies that the Hermes
    ``xai-oauth`` provider shares this budget.

Offline only: no network, no credentials.
"""

from __future__ import annotations

import importlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# quota_cache / commands use relative imports and read HERMES_HOME through
# hermes_constants, so load them as package members with that boundary stubbed.
_stub = types.ModuleType("hermes_constants")
_stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME") or tempfile.gettempdir())
sys.modules.setdefault("hermes_constants", _stub)

if "quota_plugin_under_test" not in sys.modules:
    _pkg = types.ModuleType("quota_plugin_under_test")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["quota_plugin_under_test"] = _pkg

base = importlib.import_module("quota_plugin_under_test.quota_providers.base")
builtin = importlib.import_module("quota_plugin_under_test.quota_providers.builtin")
grok = importlib.import_module("quota_plugin_under_test.quota_providers.grok")
qc = importlib.import_module("quota_plugin_under_test.quota_cache")
commands = importlib.import_module("quota_plugin_under_test.commands")
openrouter = importlib.import_module("quota_plugin_under_test.quota_providers.openrouter")


class ProvenanceVocabularyTests(unittest.TestCase):
    """The normalized vocabulary is a protocol, not free text."""

    def test_plan_sources_are_exact(self):
        self.assertEqual(base.PLAN_SOURCE_PROVIDER_RESPONSE, "provider-response")
        self.assertEqual(base.PLAN_SOURCE_LOCAL_CREDENTIAL, "local-credential")
        self.assertEqual(base.PLAN_SOURCE_USER_LABEL, "user-label")
        self.assertEqual(base.PLAN_SOURCE_UNKNOWN, "unknown")

    def test_billing_modes_are_exact(self):
        self.assertEqual(base.BILLING_SUBSCRIPTION, "subscription")
        self.assertEqual(base.BILLING_PAY_AS_YOU_GO, "pay-as-you-go")
        self.assertEqual(base.BILLING_MIXED, "mixed")
        self.assertEqual(base.BILLING_UNKNOWN, "unknown")

    def test_meters_are_exact(self):
        self.assertEqual(base.METER_SUBSCRIPTION_ALLOWANCE, "subscription allowance")
        self.assertEqual(base.METER_CREDITS_BALANCE, "credits balance")
        self.assertEqual(base.METER_CHAT_QUERIES, "chat queries")
        self.assertEqual(base.METER_UNKNOWN, "unknown")


class ProvenanceDefaultsTests(unittest.TestCase):
    def test_quotaresult_provenance_defaults_to_unknown(self):
        res = base.QuotaResult(label="x")
        self.assertIsInstance(res.provenance, base.Provenance)
        self.assertIsNone(res.provenance.source)
        self.assertIsNone(res.provenance.scope)
        self.assertIsNone(res.provenance.account)
        self.assertIsNone(res.provenance.meter)
        self.assertIsNone(res.provenance.note)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)
        self.assertEqual(res.provenance.billing_mode, base.BILLING_UNKNOWN)

    def test_quotaaccount_provenance_defaults_to_unknown(self):
        acc = base.QuotaAccount(id="a")
        self.assertIsInstance(acc.provenance, base.Provenance)
        self.assertFalse(acc.provenance.plan_is_verified())

    def test_plan_is_verified_only_for_provider_or_credential_sources(self):
        self.assertTrue(
            base.Provenance(plan_source=base.PLAN_SOURCE_PROVIDER_RESPONSE).plan_is_verified()
        )
        self.assertTrue(
            base.Provenance(plan_source=base.PLAN_SOURCE_LOCAL_CREDENTIAL).plan_is_verified()
        )
        self.assertFalse(
            base.Provenance(plan_source=base.PLAN_SOURCE_USER_LABEL).plan_is_verified()
        )
        self.assertFalse(base.Provenance().plan_is_verified())

    def test_round_trips_through_a_plain_dict(self):
        prov = base.Provenance(
            source="s", scope="sc", account="acct", meter=base.METER_CHAT_QUERIES,
            plan_source=base.PLAN_SOURCE_PROVIDER_RESPONSE,
            billing_mode=base.BILLING_SUBSCRIPTION, note="n",
        )
        self.assertEqual(base.Provenance(**base.provenance_to_dict(prov)), prov)


class AnthropicProvenanceTests(unittest.TestCase):
    def _single(self, payload):
        # No core login is patched, so the primary-token resolver returns None
        # and no credential (file or Keychain) is consulted. Pin the Keychain
        # reader anyway so a test run never touches the real login item.
        with mock.patch.object(builtin, "_anthropic_usage_payload", return_value=(payload, None)), \
                mock.patch.object(builtin, "_read_macos_claude_keychain_payload",
                                  return_value=None):
            return builtin._fetch_anthropic_single()

    def test_single_account_reports_the_oauth_source_and_subscription_meter(self):
        res = self._single({"five_hour": {"utilization": 12, "resets_at": "2026-09-01T00:00:00Z"}})
        prov = res.provenance
        self.assertEqual(prov.source, "api.anthropic.com/api/oauth/usage")
        self.assertEqual(prov.billing_mode, base.BILLING_SUBSCRIPTION)
        self.assertEqual(prov.meter, base.METER_SUBSCRIPTION_ALLOWANCE)
        self.assertEqual(prov.plan_source, base.PLAN_SOURCE_UNKNOWN)

    def test_extra_usage_reports_mixed_billing(self):
        res = self._single({
            "five_hour": {"utilization": 12},
            "extra_usage": {"is_enabled": True, "used_credits": 3.0, "monthly_limit": 50.0},
        })
        self.assertEqual(res.provenance.billing_mode, base.BILLING_MIXED)

    def test_configured_account_plan_is_marked_local_credential(self):
        import json

        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"subscriptionType": "team", "accessToken": "tok"}}), encoding="utf-8"
            )
            entries = [{"id": "work", "label": "Work", "config_dir": d}]
            with mock.patch.object(builtin, "_read_claude_account_token", return_value=("tok", None)), \
                    mock.patch.object(builtin, "_request_anthropic_usage",
                                      return_value=({"five_hour": {"utilization": 5}}, None)):
                accounts = builtin._claude_account_results(
                    entries, primary_token="primary", deadline=builtin.Deadline(5.0))
        self.assertEqual(len(accounts), 1)
        acc = accounts[0]
        self.assertEqual(acc.plan, "Team")
        self.assertEqual(acc.provenance.plan_source, base.PLAN_SOURCE_LOCAL_CREDENTIAL)
        self.assertTrue(acc.provenance.plan_is_verified())

    def _account(self, label):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"subscriptionType": "team", "accessToken": "tok"}}), encoding="utf-8")
            entries = [{"id": "work", "label": label, "config_dir": d}]
            with mock.patch.object(builtin, "_read_claude_account_token",
                                   return_value=("tok", None)), \
                    mock.patch.object(builtin, "_request_anthropic_usage",
                                      return_value=({"five_hour": {"utilization": 5}}, None)):
                return builtin._claude_account_results(
                    entries, primary_token="primary", deadline=builtin.Deadline(5.0))[0]

    def test_configured_account_provenance_uses_its_own_display_label(self):
        self.assertEqual(self._account("Work laptop").provenance.account, "Work laptop")

    def test_email_shaped_display_label_is_never_written_to_provenance(self):
        self.assertIsNone(self._account("me@example.com").provenance.account)


class CodexProvenanceTests(unittest.TestCase):
    def _parse(self, payload):
        return builtin._parse_codex_payload(payload)

    def test_plan_comes_from_the_provider_response(self):
        res = self._parse({"plan_type": "prolite", "rate_limit": {
            "primary_window": {"used_percent": 10, "limit_window_seconds": 18000}}})
        self.assertEqual(res.plan, "Pro 100")
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_PROVIDER_RESPONSE)
        self.assertTrue(res.provenance.plan_is_verified())
        self.assertEqual(res.provenance.meter, base.METER_SUBSCRIPTION_ALLOWANCE)
        self.assertIn("codex", res.provenance.source.lower())

    def test_missing_plan_is_unknown_never_inferred(self):
        res = self._parse({"rate_limit": {
            "primary_window": {"used_percent": 10, "limit_window_seconds": 18000}}})
        self.assertIsNone(res.plan)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)

    def test_credits_balance_does_not_change_subscription_billing(self):
        res = self._parse({
            "plan_type": "plus",
            "rate_limit": {"primary_window": {"used_percent": 10}},
            "credits": {"has_credits": True, "balance": 12.5},
        })
        # A credits-balance figure is not proof of purchased/pay-as-you-go money
        # (it may be included or promotional), so the subscription windows keep
        # the record subscription, not mixed.
        self.assertEqual(res.provenance.billing_mode, base.BILLING_SUBSCRIPTION)

    def test_credits_balance_alone_does_not_imply_pay_as_you_go(self):
        res = self._parse({"credits": {"has_credits": True, "balance": 12.5}})
        self.assertEqual(res.provenance.billing_mode, base.BILLING_UNKNOWN)

    def test_provenance_never_carries_an_account_identity(self):
        res = self._parse({"plan_type": "plus",
                           "rate_limit": {"primary_window": {"used_percent": 1}}})
        self.assertIsNone(res.provenance.account)


class GrokProvenanceTests(unittest.TestCase):
    @staticmethod
    def _grok_build_frame(used_percent: float, reset_epoch: int) -> bytes:
        inner = b"\x0d" + struct.pack("<f", used_percent)
        epoch = reset_epoch
        varint = bytearray()
        while True:
            byte = epoch & 0x7F
            epoch >>= 7
            if epoch:
                varint.append(byte | 0x80)
            else:
                varint.append(byte)
                break
        sub_msg = b"\x08" + bytes(varint)
        inner += b"\x2a" + bytes([len(sub_msg)]) + sub_msg
        top = b"\x0a" + bytes([len(inner)]) + inner
        return b"\x00" + len(top).to_bytes(4, "big") + top

    def test_build_weekly_meter_names_its_source_and_scope(self):
        res = grok._parse_grok_protobuf(self._grok_build_frame(3.0, 1_900_000_000))
        self.assertIsNotNone(res)
        prov = res.provenance
        self.assertIn("GetGrokCreditsConfig", prov.source)
        # No verified subscription/plan is available from the browser session, so
        # the record must not claim a subscription billing mode or a
        # subscription-allowance meter.
        self.assertEqual(prov.meter, base.METER_UNKNOWN)
        self.assertEqual(prov.billing_mode, base.BILLING_UNKNOWN)
        self.assertIn("grok.com", prov.scope)

    def test_build_weekly_note_marks_the_xai_relationship_unverified(self):
        res = grok._parse_grok_protobuf(self._grok_build_frame(3.0, 1_900_000_000))
        note = res.provenance.note.lower()
        self.assertIn("xai-oauth", note)
        # The claim that this budget "does not share" the Hermes xai-oauth
        # budget is unverifiable from a browser session; the note must frame it
        # as unknown/may-share, never as a categorical denial.
        self.assertTrue(
            ("cannot" in note) or ("may" in note) or ("unverified" in note),
            f"note must frame the xai-oauth relationship as unverified: {note!r}",
        )
        self.assertNotIn("does not share", note)

    def test_rest_fallback_names_the_chat_meter_and_scope(self):
        payload = {"remainingQueries": 5, "totalQueries": 10,
                   "lowEffortRateLimits": {"remainingQueries": 3, "totalQueries": 10}}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                import json as _json

                return _json.dumps(payload).encode()

        with mock.patch.object(grok, "urlopen_no_redirect", return_value=_Resp()):
            res = grok._fetch_grok_rest("cookie=1")
        self.assertIsNotNone(res)
        prov = res.provenance
        self.assertIn("/rest/rate-limits", prov.source)
        self.assertEqual(prov.meter, base.METER_CHAT_QUERIES)
        self.assertIn("chat", prov.scope.lower())
        self.assertIn("xai-oauth", prov.note.lower())


class NousProvenanceTests(unittest.TestCase):
    def _fetch(self, account):
        fake_mod = types.ModuleType("hermes_cli.nous_account")
        fake_mod.get_nous_portal_account_info = lambda *a, **k: account
        with mock.patch.dict(sys.modules, {"hermes_cli.nous_account": fake_mod}):
            return builtin._fetch_nous_portal()

    def test_subscription_credits_are_a_provider_reported_subscription_plan(self):
        access = types.SimpleNamespace(subscription_credits_remaining=None,
                                       purchased_credits_remaining=None,
                                       total_usable_credits=None)
        acct = types.SimpleNamespace(
            logged_in=True, paid_service_access=True, paid_service_access_info=access,
            tool_access=None,
            subscription=types.SimpleNamespace(
                monthly_credits=110.0, credits_remaining=88.42, rollover_credits=0,
                current_period_end="2026-09-01", plan="Super"),
        )
        res = self._fetch(acct)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_PROVIDER_RESPONSE)
        self.assertEqual(res.provenance.billing_mode, base.BILLING_SUBSCRIPTION)

    def test_spend_details_alone_do_not_imply_pay_as_you_go(self):
        access = types.SimpleNamespace(subscription_credits_remaining=3.0,
                                       purchased_credits_remaining=None,
                                       total_usable_credits=3.0)
        acct = types.SimpleNamespace(
            logged_in=True, paid_service_access=True, paid_service_access_info=access,
            tool_access=None, subscription=None,
        )
        res = self._fetch(acct)
        self.assertNotEqual(res.provenance.billing_mode, base.BILLING_PAY_AS_YOU_GO)
        self.assertEqual(res.provenance.billing_mode, base.BILLING_UNKNOWN)


class AnthropicPrimaryPlanTests(unittest.TestCase):
    """Primary-login plan is discovered only from the exact matching credential."""

    def _single(self, payload, token="tok-primary"):
        with mock.patch.object(builtin, "_core_anthropic_token", return_value=token), \
                mock.patch.object(builtin, "_anthropic_usage_payload",
                                  return_value=(payload, None)), \
                mock.patch.object(builtin, "_read_macos_claude_keychain_payload",
                                  return_value=None):
            return builtin._fetch_anthropic_single()

    def _write_creds(self, d, token="tok-primary", plan="team"):
        Path(d, ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": token,
                                          "subscriptionType": plan}}),
            encoding="utf-8",
        )

    def test_primary_plan_comes_from_the_matching_local_credential(self):
        import json

        with tempfile.TemporaryDirectory() as d:
            self._write_creds(d, token="tok-primary", plan="team")
            with mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(d)):
                res = self._single({"five_hour": {"utilization": 5}})
        self.assertEqual(res.plan, "Team")
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_LOCAL_CREDENTIAL)
        self.assertTrue(res.provenance.plan_is_verified())

    def test_primary_plan_is_not_attached_across_credentials(self):
        import json

        with tempfile.TemporaryDirectory() as d:
            self._write_creds(d, token="a-different-token", plan="team")
            with mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(d)):
                res = self._single({"five_hour": {"utilization": 5}})
        # The resolved primary token does not match this credential file's
        # accessToken, so the plan must stay unknown rather than be guessed.
        self.assertIsNone(res.plan)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)

    def test_primary_plan_is_unknown_with_no_local_credential(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(d)):
                res = self._single({"five_hour": {"utilization": 5}})
        self.assertIsNone(res.plan)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)


class OpenRouterProvenanceTests(unittest.TestCase):
    def test_wallet_figures_are_scoped_pay_as_you_go(self):
        creds = [("Key 1 (native)", "sk-secret")]
        with mock.patch.object(openrouter, "_resolve_credentials", return_value=creds), \
                mock.patch.object(
                    openrouter, "_key_result",
                    return_value=([], ["Key 1 (native): key cap USD 10.00; remaining USD 5.00"])), \
                mock.patch.object(
                    openrouter, "_wallet_result",
                    return_value=(True, "Account wallet via Key 1 (native): USD 5.00 remaining")):
            res = openrouter.fetch_openrouter_quota()
        self.assertEqual(res.provenance.billing_mode, base.BILLING_PAY_AS_YOU_GO)
        self.assertEqual(res.provenance.meter, base.METER_CREDITS_BALANCE)
        self.assertIn("openrouter.ai/api/v1", res.provenance.source)
        self.assertIn("key", res.provenance.scope.lower())


class CacheProvenanceTests(unittest.TestCase):
    def test_result_to_record_serializes_provenance(self):
        res = base.QuotaResult(
            label="grok", windows=[base.QuotaWindow(label="Weekly Limit", used_percent=3.0)],
            provenance=base.Provenance(
                source="grok.com GetGrokCreditsConfig", scope="grok.com session",
                meter=base.METER_SUBSCRIPTION_ALLOWANCE,
                billing_mode=base.BILLING_SUBSCRIPTION, plan_source=base.PLAN_SOURCE_UNKNOWN,
                note="not the Hermes xai-oauth provider"),
        )
        record = qc._result_to_record(res)
        self.assertIn("provenance", record)
        self.assertEqual(record["provenance"]["source"], "grok.com GetGrokCreditsConfig")
        self.assertEqual(record["provenance"]["meter"], base.METER_SUBSCRIPTION_ALLOWANCE)

    def test_account_rows_keep_their_provenance(self):
        res = base.QuotaResult(label="anthropic", windows=[], accounts=[
            base.QuotaAccount(id="work", label="Work", plan="Team",
                              windows=[base.QuotaWindow(label="Current week", used_percent=5.0)],
                              provenance=base.Provenance(
                                  source="api.anthropic.com/api/oauth/usage",
                                  plan_source=base.PLAN_SOURCE_LOCAL_CREDENTIAL,
                                  billing_mode=base.BILLING_SUBSCRIPTION,
                                  meter=base.METER_SUBSCRIPTION_ALLOWANCE)),
        ])
        record = qc._result_to_record(res)
        rows = qc._expand_accounts("anthropic", record)
        row = rows["anthropic:work"]
        self.assertEqual(row["provenance"]["plan_source"], base.PLAN_SOURCE_LOCAL_CREDENTIAL)

    def test_serialized_provenance_has_no_email_shaped_values(self):
        res = base.QuotaResult(label="anthropic", windows=[],
                               provenance=base.Provenance(source="api.anthropic.com"))
        record = qc._result_to_record(res)
        for value in record["provenance"].values():
            if isinstance(value, str):
                self.assertNotIn("@", value)


class CliProvenanceTests(unittest.TestCase):
    def _render(self, providers):
        with mock.patch.object(commands, "read_quota_cache",
                               return_value={"fetched_at": None, "providers": providers}), \
                mock.patch.object(commands, "quota_cache_age_seconds", return_value=1.0):
            return commands._render_quota(None)

    def test_cli_shows_source_and_billing_for_a_provider(self):
        out = self._render({"grok": {
            "label": "grok",
            "unavailable_reason": None,
            "windows": [{"label": "Weekly Limit", "used_percent": 3.0}],
            "details": [],
            "provenance": {"source": "grok.com GetGrokCreditsConfig",
                           "billing_mode": base.BILLING_SUBSCRIPTION,
                           "meter": base.METER_SUBSCRIPTION_ALLOWANCE,
                           "plan_source": base.PLAN_SOURCE_UNKNOWN},
        }})
        self.assertIn("source:", out)
        self.assertIn("grok.com GetGrokCreditsConfig", out)
        self.assertIn("billing:", out)

    def test_cli_distinguishes_a_verified_plan_from_a_user_label(self):
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
        self.assertIn("Pro 200", out)
        self.assertIn(base.PLAN_SOURCE_PROVIDER_RESPONSE, out)


class AnthropicPrimaryKeychainPlanTests(unittest.TestCase):
    """macOS primary-login plan from the ``Claude Code-credentials`` Keychain item.

    On macOS the borrowed Claude login lives in that Keychain item rather than
    ``.credentials.json``, so a file-only reader leaves the primary plan unknown
    for the common enterprise/EDU install. The item's ``subscriptionType`` is
    used **only** when its ``accessToken`` is byte-identical to the resolved
    primary token; every other item, or any read failure, stays ``unknown``. No
    token is ever returned, logged, or cached.
    """

    def _single(self, payload, token="tok-primary", keychain=None, config_dir=None):
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(config_dir or empty)), \
                    mock.patch.object(builtin, "_core_anthropic_token", return_value=token), \
                    mock.patch.object(builtin, "_anthropic_usage_payload",
                                      return_value=(payload, None)), \
                    mock.patch.object(builtin, "_read_macos_claude_keychain_payload",
                                      return_value=keychain):
                return builtin._fetch_anthropic_single()

    def test_keychain_plan_used_when_the_file_has_no_credential(self):
        res = self._single(
            {"five_hour": {"utilization": 5}},
            keychain={"claudeAiOauth": {"accessToken": "tok-primary",
                                        "subscriptionType": "enterprise"}})
        self.assertEqual(res.plan, "Enterprise")
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_LOCAL_CREDENTIAL)
        self.assertTrue(res.provenance.plan_is_verified())

    def test_keychain_plan_is_not_attached_across_credentials(self):
        res = self._single(
            {"five_hour": {"utilization": 5}},
            keychain={"claudeAiOauth": {"accessToken": "a-different-token",
                                        "subscriptionType": "enterprise"}})
        self.assertIsNone(res.plan)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)

    def test_keychain_payload_without_a_subscription_type_stays_unknown(self):
        res = self._single(
            {"five_hour": {"utilization": 5}},
            keychain={"claudeAiOauth": {"accessToken": "tok-primary"}})
        self.assertIsNone(res.plan)
        self.assertEqual(res.provenance.plan_source, base.PLAN_SOURCE_UNKNOWN)

    def test_file_match_short_circuits_the_keychain_read(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "tok-primary",
                                              "subscriptionType": "team"}}),
                encoding="utf-8")
            with mock.patch.object(builtin, "_read_macos_claude_keychain_payload") as keychain:
                res = self._single({"five_hour": {"utilization": 5}}, config_dir=d)
                keychain.assert_not_called()
        self.assertEqual(res.plan, "Team")

    def test_no_token_never_reaches_the_keychain(self):
        with mock.patch.object(builtin, "_read_macos_claude_keychain_payload") as keychain:
            res = self._single({"five_hour": {"utilization": 5}}, token=None)
            keychain.assert_not_called()
        self.assertIsNone(res.plan)


class MacosClaudeKeychainReaderTests(unittest.TestCase):
    """The bounded, read-only Keychain read: exact command, bounded timeout, errors -> unknown."""

    @staticmethod
    def _completed(stdout, returncode=0):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def test_reads_the_exact_service_with_a_bounded_timeout(self):
        payload = {"claudeAiOauth": {"accessToken": "t", "subscriptionType": "enterprise"}}
        with mock.patch.object(builtin, "_macos_keychain_supported", return_value=True), \
                mock.patch.object(builtin.subprocess, "run",
                                  return_value=self._completed(json.dumps(payload))) as run:
            result = builtin._read_macos_claude_keychain_payload()
        self.assertEqual(result, payload)
        self.assertEqual(run.call_args.args[0],
                         ["security", "find-generic-password",
                          "-s", "Claude Code-credentials", "-w"])
        # Bounded: an explicit timeout, and never raise on a non-zero exit.
        self.assertEqual(run.call_args.kwargs.get("timeout"), builtin._KEYCHAIN_TIMEOUT_S)
        self.assertTrue(run.call_args.kwargs.get("timeout") > 0)
        self.assertFalse(run.call_args.kwargs.get("check", True))

    def test_non_zero_exit_is_unknown(self):
        with mock.patch.object(builtin, "_macos_keychain_supported", return_value=True), \
                mock.patch.object(builtin.subprocess, "run",
                                  return_value=self._completed("", returncode=1)):
            self.assertIsNone(builtin._read_macos_claude_keychain_payload())

    def test_timeout_is_unknown(self):
        with mock.patch.object(builtin, "_macos_keychain_supported", return_value=True), \
                mock.patch.object(builtin.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("security", 5)):
            self.assertIsNone(builtin._read_macos_claude_keychain_payload())

    def test_missing_security_binary_is_unknown(self):
        with mock.patch.object(builtin, "_macos_keychain_supported", return_value=True), \
                mock.patch.object(builtin.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(builtin._read_macos_claude_keychain_payload())

    def test_non_json_output_is_unknown(self):
        with mock.patch.object(builtin, "_macos_keychain_supported", return_value=True), \
                mock.patch.object(builtin.subprocess, "run",
                                  return_value=self._completed("not json")):
            self.assertIsNone(builtin._read_macos_claude_keychain_payload())

    def test_non_object_output_is_unknown(self):
        with mock.patch.object(builtin, "_macos_keychain_supported", return_value=True), \
                mock.patch.object(builtin.subprocess, "run",
                                  return_value=self._completed("[1, 2, 3]")):
            self.assertIsNone(builtin._read_macos_claude_keychain_payload())

    def test_is_never_attempted_off_macos(self):
        with mock.patch.object(builtin, "_macos_keychain_supported", return_value=False), \
                mock.patch.object(builtin.subprocess, "run") as run:
            self.assertIsNone(builtin._read_macos_claude_keychain_payload())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
