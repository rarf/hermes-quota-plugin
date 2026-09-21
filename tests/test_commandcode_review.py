"""Regression tests for the reviewed CommandCode provider contract."""

from __future__ import annotations

import importlib
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest import mock


class CommandCodeReviewTests(unittest.TestCase):
    def _module(self):
        # Keep the pre-implementation RED useful: a missing provider is an
        # assertion failure, not a collection-time ImportError.
        from quota_providers.registry import get_fetcher

        self.assertIsNotNone(
            get_fetcher("commandcode"),
            "CommandCode must be registered before its fetch contract can run",
        )
        return importlib.import_module("quota_providers.commandcode")

    @staticmethod
    def _payloads(credits, sub=None, summary=None, whoami=None):
        return {
            "/alpha/whoami": {"org": {"id": "org-test"}} if whoami is None else whoami,
            "/alpha/billing/credits": credits,
            "/alpha/billing/subscriptions": {
                "data": {
                    "planId": "individual-goat",
                    "currentPeriodEnd": "2026-10-11T18:36:24.000Z",
                }
            }
            if sub is None
            else sub,
            "/alpha/usage/summary": {"totalCost": 2.0, "totalCount": 3, "totalTokens": 4000}
            if summary is None
            else summary,
        }

    def _fetch(self, credits, *, sub=None, summary=None, whoami=None, get_override=None):
        module = self._module()
        payloads = self._payloads(credits, sub=sub, summary=summary, whoami=whoami)
        calls = []

        def fake_get(path, key, timeout=None):
            calls.append((path, key, timeout))
            base = path.split("?", 1)[0]
            if get_override is not None:
                return get_override(path, key, timeout, payloads, base)
            return payloads[base]

        with mock.patch.object(module, "_load_api_key", return_value="test-key"), mock.patch.object(
            module, "_get", side_effect=fake_get
        ):
            result = module.fetch_commandcode_quota()
        return result, calls

    @staticmethod
    def _window_limits():
        return {
            "limited": True,
            "fiveHour": {"used": 3.5, "cap": 14, "resetAt": 1789700413797},
            "weekly": {"used": 7.0, "cap": 35, "resetAt": 1789756647895},
        }

    def test_accepts_window_limits_nested_under_credits(self):
        credits = {
            "credits": {
                "planId": "individual-pro-v1",
                "monthlyCredits": 40.0,
                "purchasedCredits": 0.0,
                "freeCredits": 0.0,
                "windowLimits": self._window_limits(),
            }
        }
        result, _calls = self._fetch(credits)
        by_label = {window.label: window for window in result.windows}
        self.assertEqual({"5h", "Weekly", "Cycle"}, set(by_label))
        self.assertAlmostEqual(by_label["5h"].used_percent, 25.0)
        self.assertAlmostEqual(by_label["Weekly"].used_percent, 20.0)

    def test_accepts_window_limits_at_payload_top_level(self):
        credits = {
            "credits": {"monthlyCredits": 40.0},
            "windowLimits": self._window_limits(),
        }
        result, _calls = self._fetch(credits)
        self.assertEqual({"5h", "Weekly", "Cycle"}, {w.label for w in result.windows})

    def test_known_pro_plan_ids_keep_distinct_published_allowances(self):
        for plan_id, allowance in (("individual-pro", 30.0), ("individual-pro-v1", 80.0)):
            with self.subTest(plan_id=plan_id):
                credits = {
                    "credits": {"monthlyCredits": allowance / 2},
                    "windowLimits": self._window_limits(),
                }
                sub = {"data": {"planId": plan_id, "currentPeriodEnd": "2026-10-11T18:36:24Z"}}
                result, _calls = self._fetch(credits, sub=sub, summary=None)
                self.assertEqual("Pro", result.plan)
                cycle = next(window for window in result.windows if window.label == "Cycle")
                self.assertAlmostEqual(cycle.used_percent, 50.0)

    def test_unknown_plan_has_no_invented_display_label_or_cycle_percent(self):
        credits = {
            "credits": {"monthlyCredits": 60.0},
            "windowLimits": self._window_limits(),
        }
        sub = {"data": {"planId": "individual-future", "currentPeriodEnd": "2026-10-11T18:36:24Z"}}
        result, _calls = self._fetch(credits, sub=sub)
        self.assertIsNone(result.plan)
        self.assertNotIn("Cycle", {window.label for window in result.windows})
        self.assertIn("Cycle credits left: $60.00", "\n".join(result.details))

    def test_uses_cli_proven_org_and_cycle_query_scopes(self):
        credits = {"credits": {"monthlyCredits": 5.0}, "windowLimits": self._window_limits()}
        sub = {
            "data": {
                "planId": "individual-go",
                "currentPeriodStart": "2026-09-01T00:00:00Z",
                "currentPeriodEnd": "2026-10-01T00:00:00Z",
            }
        }
        result, calls = self._fetch(credits, sub=sub, whoami={"org": {"id": "org-primary"}})
        self.assertIsNone(result.unavailable_reason)
        paths = {urlsplit(path).path: parse_qs(urlsplit(path).query) for path, _key, _timeout in calls}
        self.assertEqual({"limits": ["1"]}, paths["/alpha/whoami"])
        self.assertEqual({"orgId": ["org-primary"]}, paths["/alpha/billing/credits"])
        self.assertEqual({"orgId": ["org-primary"]}, paths["/alpha/billing/subscriptions"])
        self.assertEqual(
            {"orgId": ["org-primary"], "since": ["2026-09-01T00:00:00Z"]},
            paths["/alpha/usage/summary"],
        )

    def test_supporting_requests_run_concurrently(self):
        credits = {"credits": {"monthlyCredits": 5.0}, "windowLimits": self._window_limits()}
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active = 0
        max_active = 0

        def get_override(path, _key, _timeout, payloads, base):
            nonlocal active, max_active
            if base == "/alpha/whoami":
                return payloads[base]
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                try:
                    barrier.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass
                return payloads[base]
            finally:
                with lock:
                    active -= 1

        result, _calls = self._fetch(credits, get_override=get_override)
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(2, max_active)

    def test_provider_deadline_does_not_wait_for_hung_supporting_call(self):
        credits = {"credits": {"monthlyCredits": 5.0}, "windowLimits": self._window_limits()}
        release = threading.Event()

        def get_override(path, _key, _timeout, payloads, base):
            if base == "/alpha/whoami" or base == "/alpha/billing/credits":
                return payloads[base]
            release.wait(1.0)
            return payloads[base]

        module = self._module()
        started = time.monotonic()
        with mock.patch.object(module, "_REQUEST_DEADLINE_S", 0.05):
            result, _calls = self._fetch(credits, get_override=get_override)
        elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 0.5)
        self.assertIn("5h", {window.label for window in result.windows})


if __name__ == "__main__":
    unittest.main(verbosity=2)
