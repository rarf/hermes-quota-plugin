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
            AccountBalance('USD', '12.50', '0', '12.50')], api_calls_available=True)
        self.assertTrue(result.has_data())
        cache, _ = self._run({'deepseek': lambda: result})
        record = self._cache()['providers']['deepseek']
        self.assertEqual(record['account_balances'][0]['total_balance'], '12.50')
        self.assertIs(record['api_calls_available'], True)
        self.assertEqual(record['windows'], [])
        self.assertEqual(record, cache['providers']['deepseek'])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
