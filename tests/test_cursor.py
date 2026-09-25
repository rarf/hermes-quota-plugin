"""Offline tests for the Cursor quota fetcher (stdlib only, no network)."""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
from io import BytesIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota_providers import PROVIDER_FETCHERS, cursor  # noqa: E402

_CYCLE_END_MS = "1791566640000"

_USAGE = {
    "billingCycleStart": "1788974640000",
    "billingCycleEnd": _CYCLE_END_MS,
    "planUsage": {
        "totalSpend": 21710,
        "includedSpend": 2000,
        "limit": 2000,
        "autoPercentUsed": 89.34761904761905,
        "apiPercentUsed": 73.675,
        "totalPercentUsed": 86.84,
    },
    "spendLimitUsage": {
        "totalSpend": 208899,
        "pooledLimit": "750000",
        "pooledUsed": 208899,
        "pooledRemaining": "541101",
        "limitType": "team",
    },
}
_PLAN = {"planInfo": {"planName": "Team", "includedAmountCents": 2000}}


class _Resp(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(routes):
    def _urlopen(req, timeout=None):  # noqa: ANN001, ARG001
        method = req.full_url.rsplit("/", 1)[-1]
        value = routes[method]
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return _Resp(body)

    return _urlopen


def _fetch(routes, token="tok"):
    with mock.patch.object(cursor, "resolve_access_token", return_value=token), \
         mock.patch.object(cursor.urllib.request, "urlopen", _opener(routes)):
        return cursor.fetch_cursor_quota()


class CursorFetcherTests(unittest.TestCase):
    def test_registered(self):
        self.assertIn("cursor", PROVIDER_FETCHERS)

    def test_no_credentials(self):
        res = _fetch({}, token=None)
        self.assertEqual(res.unavailable_reason, "no-credentials")

    def test_team_payload_maps_windows_plan_and_pool(self):
        res = _fetch({"GetCurrentPeriodUsage": _USAGE, "GetPlanInfo": _PLAN})
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Team")
        by_label = {w.label: w for w in res.windows}
        # Auto is not surfaced; the team pool is a detail, not a quota window.
        self.assertEqual(list(by_label), ["Included", "API"])
        self.assertEqual(by_label["Included"].used_percent, 86.84)
        self.assertEqual(by_label["API"].used_percent, 73.67)
        self.assertEqual(by_label["Included"].reset_at, "2026-10-09T17:24:00+00:00")
        self.assertEqual(res.details, ["Team on-demand: $2,088.99 of $7,500.00"])

    def test_individual_limit_preferred_over_pool(self):
        usage = dict(_USAGE, spendLimitUsage={
            "individualUsed": 500, "individualLimit": "2000",
            "pooledUsed": 1, "pooledLimit": "10",
        })
        res = _fetch({"GetCurrentPeriodUsage": usage, "GetPlanInfo": _PLAN})
        self.assertEqual(res.windows[-1].label, "On-demand")
        self.assertEqual(res.windows[-1].used_percent, 25.0)
        self.assertIn("On-demand: $5.00 of $20.00", res.details)
        self.assertIn("Team on-demand: $0.01 of $0.10", res.details)

    def test_team_pool_detail_is_data_even_without_plan_windows(self):
        usage = {
            "planUsage": {},
            "spendLimitUsage": {"pooledUsed": 200, "pooledLimit": "5000"},
        }
        res = _fetch({"GetCurrentPeriodUsage": usage, "GetPlanInfo": _PLAN})
        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.windows, [])
        self.assertEqual(res.details, ["Team on-demand: $2.00 of $50.00"])

    def test_overall_limit_used_when_individual_absent(self):
        usage = dict(_USAGE, spendLimitUsage={"overallUsed": 300, "overallLimit": "1200"})
        res = _fetch({"GetCurrentPeriodUsage": usage, "GetPlanInfo": _PLAN})
        self.assertEqual(res.windows[-1].label, "On-demand")
        self.assertEqual(res.windows[-1].used_percent, 25.0)

    def test_requests_fit_sweep_budget(self):
        # quota_cache imports Hermes core, so read the constant from source.
        import re

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quota_cache.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        REFRESH_BUDGET_S = float(re.search(r"^REFRESH_BUDGET_S = ([0-9.]+)", src, re.M).group(1))
        worst = cursor._KEYCHAIN_TIMEOUT_S + 2 * cursor._HTTP_TIMEOUT_S
        self.assertLess(worst, REFRESH_BUDGET_S)

    def test_on_demand_without_limit_has_no_percent(self):
        usage = dict(_USAGE, spendLimitUsage={"individualUsed": 500})
        res = _fetch({"GetCurrentPeriodUsage": usage, "GetPlanInfo": _PLAN})
        self.assertNotIn("On-demand", [w.label for w in res.windows])
        self.assertEqual(res.details, [])

    def test_plan_lookup_failure_keeps_usage(self):
        res = _fetch({"GetCurrentPeriodUsage": _USAGE, "GetPlanInfo": b"garbage"})
        self.assertIsNone(res.unavailable_reason)
        self.assertIsNone(res.plan)

    def test_auth_failure(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch.object(cursor, "resolve_refresh_token", return_value=(None, None)):
            res = _fetch({"GetCurrentPeriodUsage": err})
        self.assertEqual(res.unavailable_reason, "auth-failed")

    def test_refreshes_after_auth_failure(self):
        calls = []
        usage_attempts = 0

        def opener(req, timeout=None):  # noqa: ANN001
            nonlocal usage_attempts
            endpoint = req.full_url.rsplit("/", 1)[-1]
            calls.append((endpoint, req.get_header("Authorization")))
            if endpoint == "GetCurrentPeriodUsage":
                usage_attempts += 1
                if usage_attempts == 1:
                    raise urllib.error.HTTPError(req.full_url, 401, "expired", {}, None)
                return _Resp(json.dumps(_USAGE).encode())
            if endpoint == "exchange_user_api_key":
                self.assertEqual(req.get_header("Authorization"), "Bearer refresh-token")
                return _Resp(json.dumps({
                    "accessToken": "fresh-token",
                    "refreshToken": "rotated-refresh",
                }).encode())
            if endpoint == "GetPlanInfo":
                return _Resp(json.dumps(_PLAN).encode())
            raise AssertionError(endpoint)

        with mock.patch.object(cursor, "resolve_access_token", return_value="expired-token"), \
             mock.patch.object(cursor, "resolve_refresh_token",
                               return_value=("refresh-token", "auth-file")), \
             mock.patch.object(cursor, "_persist_refreshed_credentials") as persist, \
             mock.patch.object(cursor.urllib.request, "urlopen", opener):
            res = cursor.fetch_cursor_quota()

        self.assertIsNone(res.unavailable_reason)
        self.assertEqual(res.plan, "Team")
        self.assertEqual(
            calls,
            [
                ("GetCurrentPeriodUsage", "Bearer expired-token"),
                ("exchange_user_api_key", "Bearer refresh-token"),
                ("GetCurrentPeriodUsage", "Bearer fresh-token"),
                ("GetPlanInfo", "Bearer fresh-token"),
            ],
        )
        persist.assert_called_once_with("auth-file", "fresh-token", "rotated-refresh")

    def test_garbage_and_empty_payloads(self):
        self.assertEqual(_fetch({"GetCurrentPeriodUsage": b"<html>"}).unavailable_reason, "bad-json")
        self.assertEqual(_fetch({"GetCurrentPeriodUsage": {}}).unavailable_reason, "no-data")

    def test_auth_file_fallback(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"accessToken": " file-token ", "refreshToken": "r"}, fh)
            with mock.patch.object(cursor, "_keychain_token", return_value=None), \
                 mock.patch.object(cursor, "_auth_file_path", return_value=path):
                self.assertEqual(cursor.resolve_access_token(), "file-token")

    def test_auth_file_refresh_token_is_resolved_and_persisted_atomically(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"accessToken": "old", "refreshToken": "refresh", "other": 1}, fh)
            with mock.patch.object(cursor, "_keychain_token", return_value=None), \
                 mock.patch.object(cursor, "_auth_file_path", return_value=path):
                self.assertEqual(
                    cursor.resolve_refresh_token("old"),
                    ("refresh", "auth-file"),
                )
                cursor._persist_refreshed_credentials("auth-file", "new", "rotated")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["accessToken"], "new")
            self.assertEqual(data["refreshToken"], "rotated")
            self.assertEqual(data["other"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
