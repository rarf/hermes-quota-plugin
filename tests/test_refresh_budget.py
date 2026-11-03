"""Provider scheduling budgets must fit inside the sweep budget.

`quota_cache.REFRESH_BUDGET_S` (20s) limits how long a refresh waits for the
whole sweep; unfinished providers are recorded as `timeout`, but their daemon
workers are not cancelled. Provider deadlines cap socket timeouts and retry
scheduling. `urllib` timeouts measure socket inactivity, not total response wall
time, so a trickling response can outlive its provider budget.

This suite uses mocks and loopback only; no external provider calls.
"""
import email.message
import http.server
import importlib
import json
import threading
import time
import urllib.error
import urllib.request

from quota_providers import base as base_mod
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quota_providers.base import Deadline  # noqa: E402


def _refresh_budget() -> float:
    """Read the constant from source; quota_cache imports Hermes core."""
    src = (ROOT / "quota_cache.py").read_text(encoding="utf-8")
    return float(re.search(r"^REFRESH_BUDGET_S = ([0-9.]+)", src, re.M).group(1))


REFRESH_BUDGET_S = _refresh_budget()


class FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeadlineTests(unittest.TestCase):
    """The shared budget primitive."""

    def test_slice_is_capped_by_what_remains(self):
        clock = FakeClock()
        budget = Deadline(10.0, clock=clock)
        self.assertAlmostEqual(budget.slice(15.0), 10.0)
        clock.advance(4.0)
        self.assertAlmostEqual(budget.slice(15.0), 6.0)
        self.assertAlmostEqual(budget.slice(3.0), 3.0)

    def test_slice_never_goes_negative(self):
        clock = FakeClock()
        budget = Deadline(5.0, clock=clock)
        clock.advance(30.0)
        self.assertTrue(budget.expired())
        self.assertEqual(budget.slice(15.0), 0.0)
        self.assertEqual(budget.remaining(), 0.0)

    def test_a_zero_budget_is_immediately_spent(self):
        budget = Deadline(0.0)
        self.assertTrue(budget.expired())
        self.assertEqual(budget.slice(15.0), 0.0)

    def test_a_negative_budget_is_clamped_to_zero(self):
        self.assertTrue(Deadline(-5.0).expired())


class ProviderBudgetTests(unittest.TestCase):
    """Each provider's configured scheduling budget must fit the sweep."""

    def test_opencode_go_fits(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        # Attempts share a scheduling budget, not a hard response wall clock:
        # urllib can keep a trickling body active beyond its socket timeout.
        self.assertLessEqual(mod._FETCH_BUDGET_S, REFRESH_BUDGET_S)
        self.assertLessEqual(mod._REQUEST_TIMEOUT_S, mod._FETCH_BUDGET_S)
        naive = mod._RETRY_ATTEMPTS * mod._REQUEST_TIMEOUT_S + sum(mod._RETRY_BACKOFF_SECONDS)
        self.assertGreater(naive, REFRESH_BUDGET_S,
                           "if the naive sum ever drops below the budget this is no "
                           "longer the reason the provider needs a deadline")

    def test_antigravity_fits(self):
        mod = importlib.import_module("quota_providers.antigravity")
        self.assertLessEqual(mod._FETCH_BUDGET_S, REFRESH_BUDGET_S)
        self.assertLessEqual(mod._USAGE_TIMEOUT_S, mod._FETCH_BUDGET_S)
        # The version gate and /usage are serial and share one deadline.
        self.assertLess(mod._VERSION_TIMEOUT_S, mod._USAGE_TIMEOUT_S)
        self.assertLess(mod._FETCH_BUDGET_S,
                        mod._VERSION_TIMEOUT_S + mod._USAGE_TIMEOUT_S)

    def test_antigravity_process_cleanup_grace_fits(self):
        mod = importlib.import_module("quota_providers.antigravity")
        # Each bounded call may spend one small grace period killing and
        # draining, plus a final allowance for a cancelled reader; both serial
        # calls must still fit the sweep budget.
        self.assertLessEqual(
            mod._FETCH_BUDGET_S
            + 2 * (mod._KILL_GRACE_S + mod._READER_EXIT_S),
            REFRESH_BUDGET_S,
        )
        self.assertLessEqual(mod._DRAIN_GRACE_S, mod._KILL_GRACE_S)
        self.assertLessEqual(mod._READER_EXIT_S, mod._KILL_GRACE_S)

    def test_cursor_fits(self):
        mod = importlib.import_module("quota_providers.cursor")
        self.assertLessEqual(mod._FETCH_BUDGET_S, REFRESH_BUDGET_S)
        worst = mod._KEYCHAIN_TIMEOUT_S + 2 * mod._HTTP_TIMEOUT_S
        self.assertLessEqual(worst, mod._FETCH_BUDGET_S,
                             "read-only keychain plus two RPCs must fit the provider budget")

    def test_cursor_still_satisfies_the_original_happy_path_assertion(self):
        """test_cursor.test_requests_fit_sweep_budget must keep passing."""
        mod = importlib.import_module("quota_providers.cursor")
        worst = mod._KEYCHAIN_TIMEOUT_S + 2 * mod._HTTP_TIMEOUT_S
        self.assertLess(worst, REFRESH_BUDGET_S)


def _antigravity_usage():
    return json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {
        "groups": [{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.5,
             "reset_time": "2026-10-01T00:00:00Z"}]}]}}}).encode("utf-8")


class DeadlineIsActuallyUsedTests(unittest.TestCase):
    """A provider budget must prevent further work once exhausted."""

    def test_opencode_go_closes_http_error_response(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        response = mock.Mock()
        error = urllib.error.HTTPError(
            mod._API_URL, 503, "Service Unavailable", email.message.Message(), response)
        with mock.patch.object(mod, "urlopen_no_redirect", side_effect=error):
            result = mod._attempt_usage("SYNTHETIC_KEY")
        self.assertEqual(result[1], "usage-unavailable")
        response.close.assert_called_once_with()

    def test_opencode_go_preserves_503_when_an_attempt_uses_the_budget(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        clock = FakeClock()
        calls = []

        def _always_503(api_key, timeout=15.0):
            calls.append(timeout)
            clock.advance(timeout)      # the server's 503 arrives at the edge
            return None, "usage-unavailable", True

        with mock.patch.object(mod, "_attempt_usage", side_effect=_always_503), \
             mock.patch.object(mod.time, "monotonic", clock):
            result = mod.fetch_usage("SYNTHETIC_KEY", _sleep=lambda _s: None)
        self.assertEqual(result.unavailable_reason, "usage-unavailable")
        self.assertLess(len(calls), mod._RETRY_ATTEMPTS,
                        "should stop retrying once the budget is spent")
        for timeout in calls:
            self.assertLessEqual(timeout, mod._FETCH_BUDGET_S)

    def test_opencode_go_preserves_503_when_backoff_spends_the_budget(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        clock = FakeClock()
        calls = []

        def _always_503(api_key, timeout=15.0):
            calls.append(timeout)
            return None, "usage-unavailable", True

        with mock.patch.object(mod, "_FETCH_BUDGET_S", 0.1), \
             mock.patch.object(mod, "_attempt_usage", side_effect=_always_503), \
             mock.patch.object(mod.time, "monotonic", clock):
            result = mod.fetch_usage(
                "SYNTHETIC_KEY", _sleep=lambda delay: clock.advance(delay))
        self.assertEqual(result.unavailable_reason, "usage-unavailable")
        self.assertEqual(len(calls), 1,
                         "a retry must not start after backoff consumes its budget")

    def test_opencode_go_still_retries_within_budget(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        good = json.dumps({"usage": {"rolling": {"percent": 4}}}).encode()
        outcomes = [(None, "usage-unavailable", True), (good, None, False)]
        with mock.patch.object(mod, "_attempt_usage", side_effect=outcomes), \
             mock.patch("time.sleep", lambda _s: None):
            result = mod.fetch_usage("SYNTHETIC_KEY")
        self.assertIsNone(result.unavailable_reason)

    def test_antigravity_clamps_its_usage_call(self):
        mod = importlib.import_module("quota_providers.antigravity")
        seen = []

        def _run(path, args, *, timeout, max_bytes=mod._MAX_OUTPUT_BYTES,
                 log_name=None):
            seen.append(timeout)
            if tuple(args) == ("--version",):
                return mod._CliRun(b"1.4.12\n", 0)
            return mod._CliRun(_antigravity_usage(), 0)

        with mock.patch.object(mod, "_agy_lookup",
                               return_value=("/fake/agy", None)), \
             mock.patch.object(mod, "_run_cli", side_effect=_run):
            result = mod.fetch_antigravity_quota()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(seen), 2)
        for timeout in seen:
            self.assertLessEqual(timeout, mod._FETCH_BUDGET_S)

    def test_antigravity_usage_time_is_clamped_after_the_version_gate(self):
        """The version probe and /usage share one provider deadline."""
        mod = importlib.import_module("quota_providers.antigravity")
        clock = FakeClock()
        seen = []

        def _run(path, args, *, timeout, max_bytes=mod._MAX_OUTPUT_BYTES,
                 log_name=None):
            if tuple(args) == ("--version",):
                # The gate spends 10s of the 18s budget.
                clock.advance(10.0)
                return mod._CliRun(b"1.4.12\n", 0)
            seen.append(timeout)
            return mod._CliRun(_antigravity_usage(), 0)

        with mock.patch.object(mod, "_agy_lookup",
                               return_value=("/fake/agy", None)), \
             mock.patch.object(mod, "_run_cli", side_effect=_run), \
             mock.patch.object(base_mod.time, "monotonic", clock):
            result = mod.fetch_antigravity_quota()
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(seen)
        # 18s budget - 10s already spent = 8s left for the usage call.
        self.assertLessEqual(max(seen), mod._FETCH_BUDGET_S - 10.0)

    def test_antigravity_reports_timeout_when_the_budget_runs_out(self):
        """The budget starts with the fetcher, so it must be spent mid-flight."""
        mod = importlib.import_module("quota_providers.antigravity")
        clock = FakeClock()

        def _run(path, args, *, timeout, max_bytes=mod._MAX_OUTPUT_BYTES,
                 log_name=None):
            if tuple(args) == ("--version",):
                clock.advance(10.0)
                return mod._CliRun(b"1.4.12\n", 0)
            # The usage call uses its whole remaining slice.
            clock.advance(timeout)
            return mod._CliRun(b"", None, timed_out=True)

        with mock.patch.object(mod, "_agy_lookup",
                               return_value=("/fake/agy", None)), \
             mock.patch.object(mod, "_run_cli", side_effect=_run), \
             mock.patch.object(base_mod.time, "monotonic", clock):
            result = mod.fetch_antigravity_quota()
        self.assertEqual(result.unavailable_reason, "timeout")
        self.assertGreaterEqual(clock.now, mod._FETCH_BUDGET_S)

    def test_antigravity_cli_failure_is_not_reported_as_timeout(self):
        """A nonzero exit is a CLI fact, even when the budget is also spent."""
        mod = importlib.import_module("quota_providers.antigravity")
        clock = FakeClock()

        def _run(path, args, *, timeout, max_bytes=mod._MAX_OUTPUT_BYTES,
                 log_name=None):
            if tuple(args) == ("--version",):
                clock.advance(10.0)
                return mod._CliRun(b"1.4.12\n", 0)
            clock.advance(timeout)      # the failure arrives at the edge
            return mod._CliRun(b"", 1)

        with mock.patch.object(mod, "_agy_lookup",
                               return_value=("/fake/agy", None)), \
             mock.patch.object(mod, "_run_cli", side_effect=_run), \
             mock.patch.object(base_mod.time, "monotonic", clock):
            result = mod.fetch_antigravity_quota()
        self.assertEqual(result.unavailable_reason, "agy-error")

    def test_cursor_clamps_its_rpc(self):
        mod = importlib.import_module("quota_providers.cursor")
        seen = []

        def _post(method, token, **kwargs):
            seen.append(kwargs.get("timeout"))
            return {"spendLimitUsage": {"overallUsed": 1, "overallLimit": 2}}, None

        with mock.patch.object(mod, "resolve_access_token", return_value="t"), \
             mock.patch.object(mod, "_post", side_effect=_post), \
             mock.patch.object(mod, "_plan_name", return_value="Pro"):
            result = mod.fetch_cursor_quota()
        self.assertIsNone(result.unavailable_reason)
        self.assertTrue(seen)
        for timeout in seen:
            self.assertLessEqual(timeout, mod._FETCH_BUDGET_S)

    def test_cursor_reports_timeout_when_the_budget_runs_out(self):
        mod = importlib.import_module("quota_providers.cursor")
        clock = FakeClock()

        def _slow_post(method, token, **kwargs):
            clock.advance(mod._FETCH_BUDGET_S)
            return None, "auth-failed"

        with mock.patch.object(mod, "resolve_access_token", return_value="t"), \
             mock.patch.object(mod, "_post", side_effect=_slow_post), \
             mock.patch.object(base_mod.time, "monotonic", clock):
            result = mod.fetch_cursor_quota()
        self.assertEqual(result.unavailable_reason, "timeout")
        self.assertGreaterEqual(clock.now, mod._FETCH_BUDGET_S)


class UrllibSocketTimeoutLimitationsTests(unittest.TestCase):
    """Local HTTP cases for timeout/status behavior without provider calls."""

    def test_real_http_503_survives_backoff_exhausting_the_budget(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        clock = FakeClock()

        class _UnavailableHandler(http.server.BaseHTTPRequestHandler):
            requests = 0

            def do_GET(self):  # noqa: N802 - stdlib signature
                type(self).requests += 1
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002, ANN001
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _UnavailableHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), base_mod.NoRedirectHandler()).open
            with mock.patch.object(
                    mod, "_API_URL",
                    "http://127.0.0.1:%d/usage" % server.server_address[1]), \
                 mock.patch.object(mod, "_FETCH_BUDGET_S", 0.1), \
                 mock.patch.object(mod, "_REQUEST_TIMEOUT_S", 1.0), \
                 mock.patch.object(mod.time, "monotonic", clock), \
                 mock.patch.object(mod, "urlopen_no_redirect", local_opener):
                result = mod.fetch_usage(
                    "SYNTHETIC_KEY", _sleep=lambda delay: clock.advance(delay))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.unavailable_reason, "usage-unavailable")
        self.assertEqual(_UnavailableHandler.requests, 1,
                         "a retry must not start after backoff consumes its budget")

    def test_trickling_local_response_can_outlive_the_provider_budget(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        budget_s = 0.1
        body = b'{"usage":{"rolling":{"percent":4}}}'
        interval_s = 0.01

        class _TrickleHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for byte in body:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(interval_s)

            def log_message(self, format, *args):  # noqa: A002, ANN001
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _TrickleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), base_mod.NoRedirectHandler()).open
            with mock.patch.object(
                    mod, "_API_URL",
                    "http://127.0.0.1:%d/usage" % server.server_address[1]), \
                 mock.patch.object(mod, "_FETCH_BUDGET_S", budget_s), \
                 mock.patch.object(mod, "_REQUEST_TIMEOUT_S", 1.0), \
                 mock.patch.object(mod, "urlopen_no_redirect", local_opener):
                started = time.monotonic()
                result = mod.fetch_usage("SYNTHETIC_KEY", attempts=1)
                elapsed = time.monotonic() - started
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertIsNone(result.unavailable_reason)
        self.assertGreater(elapsed, budget_s,
                           "the local trickle should demonstrate inactivity-only timeout")


class HappyPathsUnchangedTests(unittest.TestCase):
    """A deadline must not change what a healthy account reports."""

    def test_antigravity_still_returns_its_windows_without_inventing_a_plan(self):
        mod = importlib.import_module("quota_providers.antigravity")

        def _run(path, args, *, timeout, max_bytes=mod._MAX_OUTPUT_BYTES,
                 log_name=None):
            if tuple(args) == ("--version",):
                return mod._CliRun(b"1.4.12\n", 0)
            return mod._CliRun(_antigravity_usage(), 0)

        with mock.patch.object(mod, "_agy_lookup",
                               return_value=("/fake/agy", None)), \
             mock.patch.object(mod, "_run_cli", side_effect=_run):
            result = mod.fetch_antigravity_quota()
        self.assertIsNone(result.unavailable_reason)
        # The CLI has no plan field; the response text must not be mined.
        self.assertIsNone(result.plan)
        self.assertEqual([w.used_percent for w in result.windows], [50.0])

    def test_cursor_still_returns_its_plan_and_windows(self):
        mod = importlib.import_module("quota_providers.cursor")
        with mock.patch.object(mod, "resolve_access_token", return_value="t"), \
             mock.patch.object(mod, "_post", return_value=({"spendLimitUsage":
                                                            {"overallUsed": 1,
                                                             "overallLimit": 2}}, None)), \
             mock.patch.object(mod, "_plan_name", return_value="Pro"):
            result = mod.fetch_cursor_quota()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.plan, "Pro")
        self.assertEqual([w.used_percent for w in result.windows], [50.0])

    def test_cursor_plan_lookup_gets_the_remaining_budget(self):
        """_plan_name is a real request; it must not run on a bare default."""
        mod = importlib.import_module("quota_providers.cursor")
        seen = {}

        def capture_plan(token, timeout=None):
            seen["timeout"] = timeout
            return "Pro"

        with mock.patch.object(mod, "resolve_access_token", return_value="tok"), \
             mock.patch.object(mod, "_post", return_value=({"spendLimitUsage":
                     {"overallUsed": 1, "overallLimit": 2}}, None)), \
             mock.patch.object(mod, "_plan_name", capture_plan):
            result = mod.fetch_cursor_quota()
        self.assertEqual(result.plan, "Pro")
        self.assertIn("timeout", seen,
                      "_plan_name called without a deadline slice")
        self.assertLessEqual(seen["timeout"], mod._FETCH_BUDGET_S)


    def test_opencode_go_live_shape_still_parses(self):
        mod = importlib.import_module("quota_providers.opencode_go")
        payload = {"usage": {
            "rolling": {"status": "ok", "percent": 4, "resetsAt": "2026-09-18T14:39:39.695Z"},
            "weekly": {"status": "ok", "percent": 1, "resetsAt": "2026-09-21T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-16T16:51:09.000Z"},
        }}
        with mock.patch.object(mod, "_attempt_usage",
                               return_value=(json.dumps(payload).encode(), None, False)):
            result = mod.fetch_usage("SYNTHETIC_KEY")
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([w.used_percent for w in result.windows], [4.0, 1.0, 0.0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
