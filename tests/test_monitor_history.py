"""Quota history, burn rate and the refresh hook (synthetic data, no network).

Run from the repo root:  python -m unittest tests.test_monitor_history
"""

from __future__ import annotations

import sqlite3
import time
import unittest

from monitor_fixtures import Home, anthropic_record, iso, load

quotas = load("quota_monitor.quotas")
store = load("quota_monitor.store")
qm = load("quota_monitor")


class HistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Home()

    def tearDown(self) -> None:
        self.home.close()

    def _rows(self):
        c = sqlite3.connect(str(self.home.root / "quota_monitor.db"))
        try:
            return c.execute("SELECT ts, provider, window, used, reset FROM quota_history ORDER BY ts, provider, window").fetchall()
        finally:
            c.close()

    def test_cache_is_recorded_once_per_fetch(self):
        now = time.time()
        self.home.quota_cache({"anthropic": anthropic_record(20, 30, now + 3600, now + 86400)}, iso(now - 5))
        self.assertEqual(quotas.record_from_cache(), 2)
        self.assertEqual(quotas.record_from_cache(), 0, "the same cache write is not recorded twice")
        rows = self._rows()
        self.assertEqual([(r[1], r[2], r[3]) for r in rows],
                         [("anthropic", "Current session", 20.0), ("anthropic", "Current week", 30.0)])
        self.assertAlmostEqual(rows[0][4], now + 3600, places=0)

    def test_identical_readings_dedupe_but_keep_a_heartbeat(self):
        now = time.time()
        r = [("anthropic", "Current week", 30.0, now + 86400)]
        self.assertEqual(store.append_readings(r, now=now - 2000), 1)
        self.assertEqual(store.append_readings(r, now=now - 1990), 0, "unchanged within the heartbeat")
        self.assertEqual(store.append_readings(r, now=now), 1, "unchanged but older than the heartbeat")
        self.assertEqual(store.append_readings([("anthropic", "Current week", 31.0, now + 86400)], now=now + 1), 1)

    def test_old_rows_are_pruned(self):
        now = time.time()
        store.append_readings([("anthropic", "Current week", 10.0, None)], now=now - 40 * 86400)
        store.append_readings([("anthropic", "Current week", 11.0, None)], now=now)
        self.assertEqual([r[3] for r in self._rows()], [11.0])

    def test_unavailable_and_missing_windows_are_skipped_and_nested_accounts_keyed(self):
        now = time.time()
        providers = {
            "grok": {"label": "grok", "unavailable_reason": "no-credentials", "windows": []},
            "anthropic:work": dict(anthropic_record(5, 6, now + 60, now + 600), provider="anthropic",
                                   account_label="Work"),
            "openai-codex": {"label": "openai-codex", "unavailable_reason": None, "windows": [],
                             "accounts": [{"id": "home", "label": "Home", "windows": [
                                 {"label": "Session", "used_percent": 40, "reset_at": iso(now + 60)},
                                 {"label": "Weekly", "used_percent": None, "reset_at": None}]}]},
        }
        self.home.quota_cache(providers, iso(now))
        items = {i["provider"]: i for i in quotas.items_from_cache()}
        self.assertEqual(items["anthropic:work"]["base"], "anthropic")
        self.assertIn("openai-codex:home", items)
        quotas.record_from_cache()
        got = {(r[1], r[2]) for r in self._rows()}
        self.assertEqual(got, {("anthropic:work", "Current session"), ("anthropic:work", "Current week"),
                               ("openai-codex:home", "Session")})

    def test_burn_slope_eta_and_fills_before_reset(self):
        now = time.time()
        reset = now + 3 * 3600
        for i, used in enumerate((10.0, 20.0, 30.0)):  # +10 % per 30 min = 20 %/h
            store.append_readings([("anthropic", "Current session", used, reset)], now=now - 3600 + i * 1800)
        b = quotas.burn(now)
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(b[0]["per_hour"], 20.0, places=3)
        self.assertEqual(b[0]["used"], 30.0)
        self.assertAlmostEqual(b[0]["eta_full"], now + 3.5 * 3600, delta=2)
        self.assertFalse(b[0]["fills_before_reset"], "3.5 h to full, reset in 3 h")
        self.assertEqual(b[0]["reset"], reset)
        store.append_readings([("anthropic", "Current session", 60.0, reset)], now=now)
        self.assertTrue(quotas.burn(now)[0]["fills_before_reset"])

    def test_burn_ignores_readings_before_a_reset(self):
        now = time.time()
        store.append_readings([("anthropic", "Current session", 90.0, now)], now=now - 5000)
        store.append_readings([("anthropic", "Current session", 5.0, now + 18000)], now=now - 1800)
        store.append_readings([("anthropic", "Current session", 10.0, now + 18000)], now=now)
        b = quotas.burn(now)
        self.assertAlmostEqual(b[0]["per_hour"], 10.0, places=3)

    def test_history_payload_shape_and_day_clamp(self):
        now = time.time()
        store.append_readings([("anthropic", "Current week", 10.0, None)], now=now - 2 * 86400)
        store.append_readings([("anthropic", "Current week", 12.0, None)], now=now - 60)
        p = qm.history_payload(1)
        self.assertEqual(p["monitor_version"], 1)
        self.assertEqual(p["history"], [{"provider": "anthropic", "window": "Current week", "points": [[p["history"][0]["points"][0][0], 12.0]]}])
        self.assertEqual(qm.history_payload(999)["days"], 31)
        self.assertEqual(len(qm.history_payload(3)["history"][0]["points"]), 2)
        self.assertIn("burn", p)
        self.assertEqual(p["errors"], {})


class RefreshHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Home()

    def tearDown(self) -> None:
        self.home.close()

    def test_refresh_appends_history(self):
        qc = load("quota_cache")
        providers_base = load("quota_providers.base")
        fake = {"anthropic": lambda: providers_base.QuotaResult(
            label="anthropic", windows=[providers_base.QuotaWindow(label="Current week", used_percent=42.0)])}
        saved = dict(qc.PROVIDER_FETCHERS)
        qc.PROVIDER_FETCHERS.clear()
        qc.PROVIDER_FETCHERS.update(fake)
        try:
            qc.refresh_quota_cache(budget=5)
        finally:
            qc.PROVIDER_FETCHERS.clear()
            qc.PROVIDER_FETCHERS.update(saved)
        hist = quotas.history(time.time() - 60, time.time() + 60)
        self.assertEqual([(h["provider"], h["window"], h["points"][0][1]) for h in hist],
                         [("anthropic", "Current week", 42.0)])

    def test_refresh_survives_a_broken_history_store(self):
        qc = load("quota_cache")
        (self.home.root / "quota_monitor.db").mkdir()  # sqlite cannot open a directory
        saved = dict(qc.PROVIDER_FETCHERS)
        qc.PROVIDER_FETCHERS.clear()
        try:
            cache = qc.refresh_quota_cache(budget=1)
        finally:
            qc.PROVIDER_FETCHERS.update(saved)
        self.assertIn("fetched_at", cache)


if __name__ == "__main__":
    unittest.main()
