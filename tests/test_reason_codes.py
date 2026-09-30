"""unavailable_reason must be truthful.

add-provider.md: "**`unavailable_reason` must be truthful.** `no-data` (asked,
got nothing), `no-credentials` (nothing to auth with), `opt-in-disabled` (user
turned it off) mean different things to users staring at the muted card."

Two reasons pointed at the wrong remedy. Offline; no network.
"""
import importlib
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quota_providers import PROVIDER_FETCHERS  # noqa: E402


class GeminiAuthVsTierTests(unittest.TestCase):
    """A 401/403 was reported as consumer-tier-deprecated.

    The maintainer's own words on #25, about this exact failure mode in
    antigravity: "this is the one failure mode that looks like a product
    answer rather than an error." A user whose token expired was told to
    migrate to Antigravity; the fix is re-authenticating.
    """

    def setUp(self):
        self.mod = importlib.import_module("quota_providers.gemini")
        self.creds = {"access_token": "SYNTHETIC_KEY", "expiry_date": 9e15,
                      "quota_project": "synthetic-project"}

    def _reason_for(self, code, body):
        with mock.patch.multiple(
                self.mod,
                _load_creds=lambda: self.creds,
                _valid_token=lambda c: "SYNTHETIC_KEY",
                _load_code_assist=lambda t: {"currentTier": {"id": "g1-pro-tier"}},
                _post_json=lambda *a: (None, {"code": code, "body": body})):
            return PROVIDER_FETCHERS["gemini"]().unavailable_reason

    def test_expired_token_reports_auth_failed(self):
        for code in (401, 403):
            for body in ('{"error":"invalid_token"}', '{"error":"unauthorized"}', ""):
                with self.subTest(code=code, body=body):
                    self.assertEqual(self._reason_for(code, body), "auth-failed")

    def test_a_genuine_tier_verdict_still_reports_the_tier(self):
        """The real free-tier case must keep its honest card."""
        for body in ('{"error":"UNSUPPORTED_CLIENT"}', '{"error":"IneligibleTier"}'):
            with self.subTest(body=body):
                self.assertEqual(self._reason_for(403, body),
                                 "consumer-tier-deprecated")

    def test_other_http_statuses_are_unchanged(self):
        for code in (429, 500, 404):
            with self.subTest(code=code):
                self.assertEqual(self._reason_for(code, "{}"), f"http-{code}")

    def test_a_free_tier_account_still_gets_the_honest_card(self):
        """The free-tier branch is decided by loadCodeAssist, not the 401."""
        with mock.patch.multiple(
                self.mod,
                _load_creds=lambda: self.creds,
                _valid_token=lambda c: "SYNTHETIC_KEY",
                _load_code_assist=lambda t: {"currentTier": {"id": "free-tier"}},
                _post_json=lambda *a: (None, {"code": 403, "body": "{}"})):
            result = PROVIDER_FETCHERS["gemini"]()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.plan, "Free")
        self.assertTrue(any("antigravity.google" in d for d in result.details),
                        result.details)


class DeepSeekCredentialReasonTests(unittest.TestCase):
    """A missing Hermes core reported fetch-error instead of no-credentials."""

    def setUp(self):
        self.mod = importlib.import_module("quota_providers.deepseek")

    def test_missing_core_reports_no_credentials(self):
        """A standalone install has no hermes_cli; retrying cannot fix that."""
        with mock.patch.object(self.mod, "resolve_api_key",
                               side_effect=ModuleNotFoundError("No module named 'hermes_cli'")):
            result = self.mod.fetch_deepseek_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")

    def test_a_locked_credential_store_also_reports_no_credentials(self):
        with mock.patch.object(self.mod, "resolve_api_key",
                               side_effect=RuntimeError("keyring locked")):
            result = self.mod.fetch_deepseek_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")

    def test_no_key_still_reports_no_credentials(self):
        with mock.patch.object(self.mod, "resolve_api_key", return_value=None):
            self.assertEqual(self.mod.fetch_deepseek_quota().unavailable_reason,
                             "no-credentials")

    def test_a_transport_failure_still_reports_fetch_error(self):
        """The separation must not swallow genuine fetch failures."""
        with mock.patch.object(self.mod, "resolve_api_key", return_value="SYNTHETIC_KEY"), \
             mock.patch.object(self.mod, "get_json", side_effect=OSError("dns")):
            self.assertEqual(self.mod.fetch_deepseek_quota().unavailable_reason,
                             "fetch-error")

    def test_http_reasons_pass_through_unchanged(self):
        for reason in ("auth-failed", "http-429", "timeout", "bad-json"):
            with self.subTest(reason=reason):
                with mock.patch.object(self.mod, "resolve_api_key",
                                       return_value="SYNTHETIC_KEY"), \
                     mock.patch.object(self.mod, "get_json", return_value=(None, reason)):
                    self.assertEqual(self.mod.fetch_deepseek_quota().unavailable_reason,
                                     reason)

    def test_a_good_payload_is_unaffected(self):
        payload = {"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "12.50",
             "granted_balance": "2.50", "topped_up_balance": "10.00"}]}
        with mock.patch.object(self.mod, "resolve_api_key", return_value="SYNTHETIC_KEY"), \
             mock.patch.object(self.mod, "get_json", return_value=(payload, None)):
            result = self.mod.fetch_deepseek_quota()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([b.currency for b in result.account_balances], ["USD"])


class ReasonVocabularyTests(unittest.TestCase):
    """Guard the vocabulary the guide documents."""

    def test_guide_still_states_the_truthfulness_rule(self):
        guide = (ROOT / "docs" / "add-provider.md").read_text(encoding="utf-8")
        self.assertIn("must be truthful", guide)

    def test_reasons_are_kebab_case(self):
        """Every reason a fetcher can emit should read as a slug."""
        import ast
        import re
        providers = ROOT / "quota_providers"
        pattern = re.compile(r"build_unavailable\([^,]+,\s*[\"']([a-zA-Z0-9:_-]+)[\"']")
        for path in sorted(providers.glob("*.py")):
            for reason in pattern.findall(path.read_text(encoding="utf-8")):
                with self.subTest(reason=reason):
                    self.assertNotIn(" ", reason)
                    self.assertNotIn("_", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
