"""Offline OpenRouter contracts. All keys, accounts and amounts are synthetic."""
from __future__ import annotations

import dataclasses
from email.message import Message
import importlib
import json
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
from typing import Optional
import unittest
from unittest import mock
from urllib.error import HTTPError

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
                    windows, details = mod._key_result("Key 1", "FAKE_A")
                self.assertEqual([w.used_percent for w in windows], expected)
                self.assertIn("Key 1: usage total USD 0.0000", details)
                if left == 0:
                    self.assertIn("key cap exhausted; account wallet is separate", repr(details))
                if limit is None:
                    self.assertIn("no key-specific cap (account wallet still applies)", repr(details))

    def test_malformed_payloads_are_safe(self):
        mod = self.module()
        for payload in (None, [], {}, {"data": []}, {"data": {"limit": True, "usage": "NaN"}}, {"data": {"label": "PRIVATE_SECRET"}}):
            with self.subTest(payload=payload), mock.patch.object(mod, "_get_json", return_value=(payload, None)):
                windows, details = mod._key_result("Key 1", "FAKE_A")
            self.assertEqual(windows, [])
            self.assertEqual(details, ["Key 1: unavailable (parse-pending)"])

    def test_http_errors_are_safe_and_redirects_are_blocked(self):
        mod = self.module()
        for exc, reason in [(HTTPError("https://test", 401, "PRIVATE_SECRET", Message(), None), "auth-failed"),
                            (HTTPError("https://test", 429, "PRIVATE_SECRET", Message(), None), "http-429"),
                            (TimeoutError("PRIVATE_SECRET"), "timeout"), (RuntimeError("PRIVATE_SECRET"), "fetch-error")]:
            with self.subTest(reason=reason), mock.patch.object(mod, "_urlopen", side_effect=exc):
                self.assertEqual(mod._get_json("https://openrouter.ai/api/v1/key", "FAKE_A"), (None, reason))
        self.assertIsNone(mod._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

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


class OpenRouterWidgetTests(unittest.TestCase):
    def test_details_visible_in_clean_and_dense_only_for_openrouter(self):
        # Execute the actual ProviderRow with minimal element/hook shims, not a
        # copied renderer. No React install, browser or live widget required.
        runner = r'''
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("function ProviderRow(");
const end = src.indexOf("\nfunction ResetFormatControl(", start);
if (start < 0 || end < 0) throw Error("ProviderRow not found");
let mode = "clean";
const ID = "quota", resetFormatAtom = {}, paneDetailAtom = {};
const usePluginI18n = () => (key) => key;
const useValue = (a) => a === paneDetailAtom ? mode : "relative";
const providerMeta = id => ({name: id});
const ProviderBadge = () => null, StatusDot = () => null, QuotaBar = () => null;
const worstWindow = () => null, toneForRemaining = () => "muted", toneColor = () => "gray";
const remainingPct = () => 50, formatReset = () => "later";
const jsx = (type, props) => ({type: typeof type === "string" ? type : "component", ...props});
const jsxs = jsx;
// Only trusted, checked-in widget code is evaluated, never fixture/user data.
const render = eval(`(${src.slice(start, end)})`);
const detail = "Account wallet via Key 1; other account membership unverified";
const results = [];
for (mode of ["clean", "dense"]) {
  for (const id of ["openrouter", "nous"]) {
    for (const windows of [[], [{label: "Key quota", used_percent: 50}]]) {
      results.push({mode, id, visible: JSON.stringify(render({id, provider: {windows, details: [detail]}})).includes(detail)});
    }
  }
}
process.stdout.write(JSON.stringify(results));
'''
        run = subprocess.run(["node", "-e", runner, str(ROOT / "desktop/plugin.js")], capture_output=True, text=True, check=True)
        for row in json.loads(run.stdout):
            self.assertEqual(row["visible"], row["id"] == "openrouter" or row["mode"] == "dense", row)


if __name__ == "__main__":
    unittest.main()
