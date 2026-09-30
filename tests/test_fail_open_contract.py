"""The fail-open contract: a registered fetcher never raises.

add-provider.md, first rule under "Contract rules (all enforced in review)":
  "Fail-open, never raise. Wrap the whole body; every failure path returns
   build_unavailable("<provider>", "<machine-readable-reason>")."

Six registered fetchers could raise out of the callable itself. The sweep
survived because quota_cache._fetch_one catches, but the provider then recorded
the generic "fetch-error" instead of a reason of its own -- which the next rule
("unavailable_reason must be truthful") also forbids.

Offline; no network.
"""
import ast
import importlib
import inspect
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quota_providers import PROVIDER_FETCHERS  # noqa: E402
from quota_providers.base import QuotaResult  # noqa: E402


class RegisteredFetcherNeverRaisesTests(unittest.TestCase):
    """The contract, enforced at the registration seam."""

    def test_every_registered_fetcher_is_wrapped(self):
        """A fetcher added later inherits the guard without opting in."""
        for provider_id, fetcher in PROVIDER_FETCHERS.items():
            with self.subTest(provider=provider_id):
                self.assertTrue(
                    getattr(fetcher, "__wrapped__", None) is not None,
                    "%s is registered raw; register() should wrap it" % provider_id)

    def test_registry_guard_turns_an_exception_into_a_result(self):
        from quota_providers.registry import PROVIDER_FETCHERS as registry_map
        from quota_providers import registry

        @registry.register("test-raises-anything")
        def _boom():
            raise RuntimeError("provider exploded")

        try:
            result = registry_map["test-raises-anything"]()
            self.assertIsInstance(result, QuotaResult)
            self.assertEqual(result.unavailable_reason, "fetch-error")
            self.assertEqual(result.label, "test-raises-anything")
        finally:
            registry_map.pop("test-raises-anything", None)

    def test_guard_does_not_swallow_the_normal_result(self):
        from quota_providers.registry import PROVIDER_FETCHERS as registry_map
        from quota_providers import registry
        from quota_providers.base import QuotaWindow

        @registry.register("test-returns-normally")
        def _fine():
            return QuotaResult(label="test-returns-normally",
                               windows=[QuotaWindow(label="W", used_percent=10.0)])

        try:
            result = registry_map["test-returns-normally"]()
            self.assertIsNone(result.unavailable_reason)
            self.assertEqual([w.used_percent for w in result.windows], [10.0])
        finally:
            registry_map.pop("test-returns-normally", None)

    def test_every_fetcher_keeps_its_name(self):
        """functools.wraps, so logs and tracebacks still name the provider."""
        for provider_id, fetcher in PROVIDER_FETCHERS.items():
            with self.subTest(provider=provider_id):
                self.assertEqual(fetcher.__name__, fetcher.__wrapped__.__name__)


class GeminiEscapeTests(unittest.TestCase):
    """Three concrete escapes, each reproduced before the fix."""

    def setUp(self):
        self.mod = importlib.import_module("quota_providers.gemini")
        self.creds = {"access_token": "SYNTHETIC_KEY", "expiry_date": 9e15,
                      "quota_project": "synthetic-project"}

    def _registered(self, **patches):
        with mock.patch.multiple(self.mod, **patches):
            return PROVIDER_FETCHERS["gemini"]()

    def test_dns_failure_does_not_raise(self):
        result = self._registered(
            _load_creds=lambda: self.creds,
            _valid_token=lambda c: "SYNTHETIC_KEY",
            _load_code_assist=lambda t: {"currentTier": {"id": "g1-pro-tier"}},
            _post_json=mock.Mock(side_effect=OSError("Name or service not known")))
        self.assertIsInstance(result, QuotaResult)
        self.assertEqual(result.unavailable_reason, "fetch-error")

    def test_non_numeric_expiry_date_does_not_raise(self):
        """float("soon") used to escape _valid_token before any guard."""
        result = self._registered(
            _load_creds=lambda: {"access_token": "SYNTHETIC_KEY", "expiry_date": "soon"},
            _load_code_assist=lambda t: {},
            _post_json=lambda *a: ({}, None))
        self.assertIsInstance(result, QuotaResult)

    def test_tier_probe_failure_does_not_raise(self):
        result = self._registered(
            _load_creds=lambda: self.creds,
            _valid_token=lambda c: "SYNTHETIC_KEY",
            _load_code_assist=mock.Mock(side_effect=TimeoutError("timed out")),
            _post_json=lambda *a: ({}, None))
        self.assertIsInstance(result, QuotaResult)
        self.assertEqual(result.unavailable_reason, "fetch-error")

    def test_valid_token_tolerates_a_bad_expiry(self):
        for bad in ("soon", [1], {"a": 1}, object()):
            with self.subTest(bad=bad):
                token = self.mod._valid_token({"access_token": "SYNTHETIC_KEY",
                                               "expiry_date": bad})
                self.assertEqual(token, "SYNTHETIC_KEY")

    def test_post_json_reports_a_transport_error_distinctly(self):
        """A DNS failure has no HTTP status; it must not read as http-None."""
        with mock.patch.object(self.mod.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("dns")):
            data, err = self.mod._post_json("https://example.invalid", {}, "SYNTHETIC_KEY")
        self.assertIsNone(data)
        self.assertIsNotNone(err)
        self.assertIn("transport", err)
        self.assertIsNone(err["code"])


class CodexPostRequestParseTests(unittest.TestCase):
    """builtin._fetch_codex_with_models parses after its try/except closes."""

    def _fetch(self, payload):
        mod = importlib.import_module("quota_providers.builtin")
        agent = types.ModuleType("agent")
        usage = types.ModuleType("agent.account_usage")
        usage._resolve_codex_usage_credentials = lambda a, b: ("SYNTHETIC_KEY", "https://x", None)
        usage._codex_backend_urls = lambda b: ("https://x/api/usage",)
        usage._resolve_codex_usage_url = lambda *a, **k: "https://x/api/usage"
        agent.account_usage = usage

        class Response:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload

        class Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, *a, **k):
                return Response()

            def post(self, *a, **k):
                return Response()

        fake = types.ModuleType("httpx")
        fake.Client = lambda *a, **k: Ctx()
        with mock.patch.dict(sys.modules, {"agent": agent, "agent.account_usage": usage,
                                           "httpx": fake}):
            return PROVIDER_FETCHERS["openai-codex"]()

    def test_non_dict_sections_do_not_raise(self):
        for payload in ({"rate_limit": ["not", "a", "dict"]},
                        {"rate_limit_reset_credits": ["not", "a", "dict"]},
                        {"credits": ["not", "a", "dict"]},
                        {"additional_rate_limits": [{"limit_name": "gpt-x",
                                                     "rate_limit": ["nope"]}]}):
            with self.subTest(payload=payload):
                result = self._fetch(payload)
                self.assertIsInstance(result, QuotaResult)

    def test_out_of_range_timestamp_does_not_raise(self):
        result = self._fetch({"rate_limit": {"primary_window": {
            "used_percent": 5, "reset_at": 1e18}}})
        self.assertIsInstance(result, QuotaResult)
        self.assertIsNone(result.windows[0].reset_at)

    def test_bool_available_count_is_not_a_reset_count(self):
        result = self._fetch({"rate_limit_reset_credits": {"available_count": True}})
        self.assertIsInstance(result, QuotaResult)
        self.assertEqual([d for d in result.details if "reset" in d], [])

    def test_a_well_formed_payload_still_parses(self):
        result = self._fetch({"rate_limit": {"primary_window": {
            "used_percent": 42, "reset_at": 1780000000}}})
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([(w.label, w.used_percent) for w in result.windows], [("Session", 42.0)])


class FailOpenContractIsDocumentedTests(unittest.TestCase):
    """Guard against the contract being quietly dropped from the guide."""

    def test_add_provider_guide_still_states_the_rule(self):
        guide = (ROOT / "docs" / "add-provider.md").read_text(encoding="utf-8")
        self.assertIn("Fail-open, never raise", guide)


if __name__ == "__main__":
    unittest.main(verbosity=2)
