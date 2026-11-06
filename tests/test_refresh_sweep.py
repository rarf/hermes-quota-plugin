"""Offline unit tests for the refresh sweep (stdlib only, no network).

Contract under test: ``refresh_quota_cache`` must never let one provider
dictate how long a refresh takes. The desktop widget polls the cache on a fixed
cadence, so a sweep that ran providers sequentially (or waited forever on a hung
one) is what stretched a configured 60s refresh past 100s.

Run from the repo root:  python tests/test_refresh_sweep.py
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ``quota_cache`` resolves the cache path through hermes_constants and uses
# relative imports, so load it as a package member with the boundary stubbed —
# the tests must never touch the user's real HERMES_HOME.
_stub = types.ModuleType("hermes_constants")
_stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME") or tempfile.gettempdir())
sys.modules.setdefault("hermes_constants", _stub)

_pkg = types.ModuleType("quota_plugin_under_test")
_pkg.__path__ = [str(ROOT)]
sys.modules["quota_plugin_under_test"] = _pkg

qc = importlib.import_module("quota_plugin_under_test.quota_cache")
from quota_plugin_under_test.quota_providers.base import QuotaResult, QuotaWindow  # noqa: E402


def _ok(label: str) -> QuotaResult:
    return QuotaResult(label=label, windows=[QuotaWindow(label="weekly", used_percent=10.0)])


class _SweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _cache(self) -> dict:
        path = Path(self._tmp.name) / "quota_cache.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _run(self, fetchers: dict, **kwargs) -> tuple[dict, float]:
        original = qc.PROVIDER_FETCHERS
        qc.PROVIDER_FETCHERS = fetchers
        try:
            started = time.monotonic()
            cache = qc.refresh_quota_cache(**kwargs)
            return cache, time.monotonic() - started
        finally:
            qc.PROVIDER_FETCHERS = original

    def test_account_balance_survives_cache_serialization(self) -> None:
        from quota_plugin_under_test.quota_providers.base import AccountBalance
        result = QuotaResult(label='deepseek', account_balances=[
            AccountBalance('USD', '12.50', '0', '12.50', label='Total usable')],
            api_calls_available=True)
        self.assertTrue(result.has_data())
        cache, _ = self._run({'deepseek': lambda: result})
        record = self._cache()['providers']['deepseek']
        self.assertEqual(record['account_balances'][0]['total_balance'], '12.50')
        self.assertEqual(record['account_balances'][0]['label'], 'Total usable')
        self.assertIs(record['api_calls_available'], True)
        self.assertEqual(record['windows'], [])
        self.assertEqual(record, cache['providers']['deepseek'])

    def test_detail_facts_survive_cache_serialization(self) -> None:
        result = QuotaResult(
            label='nous', plan='Free',
            details=['Rate limits: 50 RPM'],
            detail_facts={
                'rate_limits': 'Rate limits: 50 RPM',
                'tool_pool': 'Tool pool: browser-use',
                'too_long': 'x' * 401,
            },
        )
        cache, _ = self._run({'nous': lambda: result})
        facts = cache['providers']['nous']['detail_facts']
        self.assertEqual(facts['rate_limits'], 'Rate limits: 50 RPM')
        self.assertEqual(facts['tool_pool'], 'Tool pool: browser-use')
        self.assertNotIn('too_long', facts)

    def test_account_org_key_survives_cache_serialization_without_raw_slug(self) -> None:
        result = QuotaResult(
            label='nous', plan='Free', details=['Free tier - free models only'],
            account_org_key='1523bbad0010557d',
        )
        cache, _ = self._run({'nous': lambda: result})
        record = cache['providers']['nous']
        self.assertEqual(record['account_org_key'], '1523bbad0010557d')
        self.assertNotIn('synthetic-org', record['account_org_key'])
        for malformed in ('x' * 201, 'synthetic-org', '1523BBAD0010557D',
                          '1523bbad0010557', '1523bbad0010557dd'):
            with self.subTest(value=malformed):
                bad = QuotaResult(
                    label='nous', plan='Free', details=['Free tier - free models only'],
                    account_org_key=malformed,
                )
                cache, _ = self._run({'nous': lambda: bad})
                self.assertNotIn('account_org_key', cache['providers']['nous'])

    def test_muted_and_unavailable_availability_survive_cache_serialization(self) -> None:
        """Free/unknown stays ``None``; only evidence-backed depletion says False."""
        free = QuotaResult(
            label='nous', plan='Free', details=['Free tier - free models only'],
            api_calls_available=None, access_state='free')
        depleted = QuotaResult(
            label='nous', plan='Tier 2',
            details=['Status: access depleted - top up to restore'],
            api_calls_available=False, access_state='depleted')
        self.assertTrue(free.has_data())
        self.assertTrue(depleted.has_data())
        cache, _ = self._run({'nous': lambda: free, 'nous-depleted': lambda: depleted})
        self.assertIsNone(cache['providers']['nous']['api_calls_available'])
        self.assertEqual(cache['providers']['nous']['plan'], 'Free')
        self.assertEqual(cache['providers']['nous']['access_state'], 'free')
        self.assertIs(cache['providers']['nous-depleted']['api_calls_available'], False)
        self.assertEqual(cache['providers']['nous-depleted']['plan'], 'Tier 2')
        self.assertEqual(cache['providers']['nous-depleted']['access_state'], 'depleted')

    def test_malformed_access_state_never_reaches_the_cache(self) -> None:
        """Only the documented enum is tone evidence; junk becomes unknown."""
        bad = QuotaResult(label='nous', plan='Free', details=['x'], access_state='healthy')
        cache, _ = self._run({'nous': lambda: bad})
        self.assertIsNone(cache['providers']['nous']['access_state'])

    def test_carried_provider_drops_its_access_state(self) -> None:
        """A provider not refreshed this sweep must not keep a green claim."""
        path = Path(self._tmp.name) / "quota_cache.json"
        path.write_text(json.dumps({
            'fetched_at': '2030-01-01T00:00:00+00:00',
            'providers': {
                'nous': {
                    'label': 'nous', 'plan': 'Free', 'unavailable_reason': None,
                    'details': ['Free tier - free models only'], 'windows': [],
                    'account_balances': [], 'api_calls_available': None,
                    'access_state': 'free',
                },
            },
        }), encoding='utf-8')
        cache, _ = self._run({'other': lambda: _ok('other')})
        self.assertIsNone(cache['providers']['nous']['access_state'])
        self.assertEqual(cache['providers']['nous']['unavailable_reason'], 'not-refreshed')

    def test_providers_run_concurrently(self) -> None:
        """Four 0.4s fetchers finish together, not one after the other."""
        fetchers = {
            f"p{i}": (lambda label=f"p{i}": (time.sleep(0.4), _ok(label))[1])
            for i in range(4)
        }

        cache, elapsed = self._run(fetchers)

        self.assertLess(elapsed, 1.0, "sweep looks sequential (sum of provider times)")
        self.assertEqual(sorted(cache["providers"]), ["p0", "p1", "p2", "p3"])
        self.assertEqual(cache["providers"]["p0"]["windows"][0]["label"], "weekly")

    def test_hung_provider_is_capped_by_budget(self) -> None:
        """A provider still running at the deadline is recorded, not awaited."""
        release = threading.Event()
        try:
            fetchers = {
                "fast": lambda: _ok("fast"),
                "hung": lambda: (release.wait(5.0), _ok("hung"))[1],
            }

            cache, elapsed = self._run(fetchers, budget=0.3)

            self.assertLess(elapsed, 1.2, "budget did not cap the sweep")
            self.assertEqual(cache["providers"]["fast"]["label"], "fast")
            self.assertEqual(cache["providers"]["hung"]["unavailable_reason"], "timeout")
            self.assertEqual(cache["providers"]["hung"]["windows"], [])
        finally:
            release.set()

    def test_fail_open_per_provider(self) -> None:
        """A crashing or empty fetcher leaves a record; the sweep still completes."""
        def boom() -> QuotaResult:
            raise RuntimeError("provider exploded")

        cache, _elapsed = self._run({"boom": boom, "empty": lambda: None})

        self.assertEqual(cache["providers"]["boom"]["unavailable_reason"], "fetch-error")
        self.assertEqual(cache["providers"]["empty"]["unavailable_reason"], "no-data")
        self.assertIsNotNone(cache["fetched_at"])

    def test_cache_file_is_written_for_the_widget(self) -> None:
        cache, _elapsed = self._run({"p0": lambda: _ok("p0")}, budget=1.0)

        on_disk = self._cache()
        self.assertEqual(on_disk["providers"]["p0"]["windows"][0]["used_percent"], 10.0)
        self.assertEqual(on_disk["fetched_at"], cache["fetched_at"])

    def test_incomplete_registry_keeps_previous_providers(self) -> None:
        """A sweep that never tried a provider must not delete it."""
        path = Path(self._tmp.name) / "quota_cache.json"
        path.write_text(json.dumps({"providers": {
            "kept": {
                "label": "kept", "plan": "Plus",
                "windows": [{"label": "Weekly", "used_percent": 40}],
                "details": [
                    "Not refreshed in this sweep: stale note from an older sweep",
                    "Subscription credits: $14.00",
                    "Spend this period: $6.00",
                    "Renews: Dec 3, 2026",
                ],
                "account_balances": [
                    {"currency": "USD", "total_balance": "26.00",
                     "label": "Total usable"},
                ],
                "detail_facts": {
                    "spend": "Spend this period: $6.00",
                    "renews": "Renews: Dec 3, 2026",
                },
            },
            "swept": {"label": "swept", "windows": []},
        }}), encoding="utf-8")

        cache, _elapsed = self._run({"swept": lambda: _ok("swept")}, budget=1.0)

        self.assertIn("kept", cache["providers"])
        kept = cache["providers"]["kept"]
        self.assertEqual(kept["unavailable_reason"], "not-refreshed")
        self.assertEqual(kept["windows"], [], "unattempted providers must not present stale numbers")
        self.assertEqual(kept["account_balances"], [])
        self.assertEqual(kept["plan"], "Plus", "plan identity is not a financial fact")
        # Every cached money/limit line is stale history; only the honest
        # not-refreshed note survives so the CLI cannot print a balance.
        self.assertEqual(kept["details"],
                         ["Not refreshed in this sweep: the provider registry was incomplete"])
        self.assertEqual(kept["detail_facts"], {})
        self.assertNotIn("Subscription credits", json.dumps(kept))
        self.assertNotIn("Spend this period", json.dumps(kept))
        self.assertNotIn("Renews", json.dumps(kept))
        self.assertNotIn("Total usable", json.dumps(kept))

        # A second incomplete sweep must not accumulate the note.
        cache, _elapsed = self._run({"swept": lambda: _ok("swept")}, budget=1.0)
        notes = [d for d in cache["providers"]["kept"]["details"] if d.startswith("Not refreshed in this sweep")]
        self.assertEqual(len(notes), 1)
        self.assertNotEqual(cache["providers"]["swept"].get("unavailable_reason"),
                            "not-refreshed")

    def test_incomplete_registry_scrubs_nested_account_details(self) -> None:
        """Codex saved accounts are rendered individually; scrub each one."""
        path = Path(self._tmp.name) / "quota_cache.json"
        path.write_text(json.dumps({"providers": {
            "openai-codex": {
                "label": "openai-codex",
                "windows": [{"label": "Weekly", "used_percent": 80}],
                "accounts": [{
                    "label": "work@example.test",
                    "windows": [{"label": "Weekly", "used_percent": 80}],
                    "details": ["Credits balance: $12.50"],
                    "account_balances": [
                        {"currency": "USD", "total_balance": "12.50"},
                    ],
                    "detail_facts": {"spend": "Spend this period: $4.00"},
                }],
            },
            "swept": {"label": "swept", "windows": []},
        }}), encoding="utf-8")

        cache, _elapsed = self._run({"swept": lambda: _ok("swept")}, budget=1.0)

        kept = cache["providers"]["openai-codex"]
        self.assertEqual(kept["unavailable_reason"], "not-refreshed")
        self.assertEqual(len(kept["accounts"]), 1)
        child = kept["accounts"][0]
        self.assertEqual(child["label"], "work@example.test")
        self.assertEqual(child["unavailable_reason"], "not-refreshed")
        self.assertEqual(child["windows"], [])
        self.assertEqual(child["account_balances"], [])
        self.assertEqual(child["detail_facts"], {})
        self.assertEqual(child["details"],
                         ["Not refreshed in this sweep: the provider registry was incomplete"])
        self.assertNotIn("Credits balance", json.dumps(kept))
        self.assertNotIn("Spend this period", json.dumps(kept))


if __name__ == "__main__":
    unittest.main(verbosity=2)
