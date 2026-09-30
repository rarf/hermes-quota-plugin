"""Parser honesty: a percentage must come from a real denominator.

Rule under test, from docs/add-provider.md:
  "Percentages need denominators. Emit used_percent only from used/limit,
   remaining/limit, or a server-reported percent. Never fabricate a percent
   from a bare balance."

Every case here produced a plausible-looking but wrong number before the fix.
All offline; no network.
"""
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load(name):
    """Import a provider module the way the repo's own tests do."""
    return importlib.import_module("quota_providers." + name)


class GeminiFractionRangeTests(unittest.TestCase):
    """gemini had no range guard; its sibling antigravity.py has one."""

    def _windows(self, fraction):
        mod = load("gemini")
        payload = {"quota": [{"bucketId": "gemini-5h", "modelId": "gemini-5h",
                              "remainingFraction": fraction,
                              "resetTime": "2026-10-01T00:00:00Z"}]}
        result = mod._parse_quota(payload)
        return None if result is None else [w.used_percent for w in result.windows]

    def test_in_range_fractions_are_used(self):
        self.assertEqual(self._windows(0.5), [50.0])
        self.assertEqual(self._windows(0.926965), [7.3])   # from the live capture
        self.assertEqual(self._windows(0.0), [100.0])      # drained
        self.assertEqual(self._windows(1.0), [0.0])        # untouched

    def test_out_of_range_fraction_is_dropped_not_multiplied(self):
        """86.2 is not "8620% remaining" -- it is a different unit entirely."""
        for fraction in (86.2, -0.5, 2.5, 1e9):
            with self.subTest(fraction=fraction):
                self.assertIsNone(self._windows(fraction))

    def test_non_numeric_fraction_is_dropped(self):
        for fraction in ("0.5", True, [0.5], {"v": 1}):
            with self.subTest(fraction=fraction):
                self.assertIsNone(self._windows(fraction))

    def test_matches_the_antigravity_guard(self):
        """The two Google providers must not agree to disagree on range.

        antigravity._window carries the range guard. It also has a separate,
        deliberately pinned rule -- a non-numeric fraction with a resetTime is
        an *exhausted* counter, not an unknown one (test_antigravity.py:246).
        gemini has no such rule, so this compares the numeric range only and
        leaves the string/bool behaviour to the question raised in the PR body
        rather than quietly narrowing a pinned rule here.
        """
        anti = load("antigravity")
        for fraction in (0.5, 0.926965, 86.2, -0.5, 2.5, 1e9):
            with self.subTest(fraction=fraction):
                anti_window = anti._window(
                    {"bucketId": "gemini-5h", "window": "5h",
                     "remainingFraction": fraction,
                     "resetTime": "2026-10-01T00:00:00Z"}, "5h")
                self.assertEqual(
                    self._windows(fraction) is None, anti_window is None,
                    "gemini and antigravity disagree on %r" % (fraction,))


class OpenCodeGoUsedLimitTests(unittest.TestCase):
    """"usage" in _PERCENT_KEYS shadowed the documented used/limit fallback."""

    def setUp(self):
        self.mod = load("opencode_go")

    def test_dollar_amount_is_not_read_as_a_percentage(self):
        # The module docstring: "percent can be computed from used/limit pairs
        # when no direct field exists" and "never fake zeros".
        self.assertAlmostEqual(
            self.mod._window_percent({"usage": 3.0, "limit": 12.0}), 25.0, places=2)
        self.assertAlmostEqual(
            self.mod._window_percent({"used": 3.0, "limit": 12.0}), 25.0, places=2)

    def test_a_small_dollar_amount_is_not_inflated(self):
        # 0.3 of 12 dollars is 2.5%, not 30%.
        self.assertAlmostEqual(
            self.mod._window_percent({"usage": 0.3, "limit": 12.0}), 2.5, places=2)

    def test_genuine_percent_fields_still_win(self):
        for key in ("percent", "usagePercent", "used_percent", "utilization"):
            with self.subTest(key=key):
                self.assertEqual(self.mod._window_percent({key: 42}), 42.0)

    def test_integral_percent_is_face_value(self):
        """Issue #8: percent 1 means 1%, not a 0-1 fraction worth 100%."""
        self.assertEqual(self.mod._window_percent({"percent": 1}), 1.0)

    def test_live_shape_still_parses(self):
        """The verified-live payload from tests/test_opencode_go.py."""
        payload = {"usage": {
            "rolling": {"status": "ok", "percent": 4, "resetsAt": "2026-09-18T14:39:39.695Z"},
            "weekly": {"status": "ok", "percent": 1, "resetsAt": "2026-09-21T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-16T16:51:09.000Z"},
        }}
        windows = self.mod.parse_usage_payload(payload, now=0)
        self.assertEqual([w.label for w in windows], ["5-hour", "Weekly", "Monthly"])
        self.assertEqual([w.used_percent for w in windows], [4.0, 1.0, 0.0])


class AnthropicUtilizationTests(unittest.TestCase):
    """`used <= 1` rescaled a genuine 1% to 100%."""

    def _used(self, utilization):
        mod = load("builtin")
        windows, _ = mod._parse_anthropic_usage(
            {"five_hour": {"utilization": utilization, "resets_at": "2026-09-23T09:00:00Z"}})
        return windows[0].used_percent if windows else None

    def test_genuine_one_percent_stays_one_percent(self):
        self.assertEqual(self._used(1), 1.0)
        self.assertEqual(self._used(1.0), 1.0)

    def test_a_real_fraction_is_still_rescaled(self):
        self.assertEqual(self._used(0.42), 42.0)
        self.assertEqual(self._used(0.005), 0.5)

    def test_zero_and_large_values_are_untouched(self):
        self.assertEqual(self._used(0), 0.0)
        self.assertEqual(self._used(65.0), 65.0)   # the live-captured Team plan
        self.assertEqual(self._used(99.5), 99.5)

    def test_out_of_range_is_clamped_by_the_existing_min_max(self):
        self.assertEqual(self._used(150), 100.0)


class CodexWindowClampTests(unittest.TestCase):
    """builtin._window wrote used_percent unclamped, so 150.0 reached the cache."""

    def _fetch(self, used_percent):
        mod = load("builtin")
        # _fetch_codex_with_models imports its helpers from agent.account_usage
        # inside the function, so a fake module is needed rather than an
        # attribute patch on `builtin`.
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
                return {"rate_limit": {"primary_window": {"used_percent": used_percent,
                                                           "reset_at": 1780000000}}}

        class Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, *a, **k):
                return Response()

            def post(self, *a, **k):
                return Response()

        fake_httpx = types.ModuleType("httpx")
        fake_httpx.Client = lambda *a, **k: Ctx()
        with mock.patch.dict(sys.modules, {
                "agent": agent,
                "agent.account_usage": usage,
                "httpx": fake_httpx,
        }):
            return mod._fetch_codex_with_models()

    def test_out_of_range_percent_is_clamped_like_its_siblings(self):
        result = self._fetch(150)
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([w.used_percent for w in result.windows], [100.0])

    def test_in_range_percent_is_untouched(self):
        result = self._fetch(42)
        self.assertEqual([w.used_percent for w in result.windows], [42.0])


class CursorPercentRangeTests(unittest.TestCase):
    """Clamping reported 150% as a full bar reads as 'exhausted'."""

    def setUp(self):
        self.mod = load("cursor")

    def test_in_range_passes_through(self):
        self.assertEqual(self.mod._pct(0), 0.0)
        self.assertEqual(self.mod._pct(42.5), 42.5)
        self.assertEqual(self.mod._pct(100), 100.0)

    def test_out_of_range_is_none_not_a_full_bar(self):
        for value in (150, -5, 1e9):
            with self.subTest(value=value):
                self.assertIsNone(self.mod._pct(value))

    def test_non_numeric_is_still_none(self):
        for value in ("abc", None, True, float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(self.mod._pct(value))


class ZaiPercentTests(unittest.TestCase):
    """>100 clamped to 100 while <0 was rejected -- the same rule, two answers."""

    def setUp(self):
        self.mod = load("zai")

    def _pct_from(self, entry):
        return self.mod._used_percent(entry)

    def test_in_range_is_kept(self):
        if not hasattr(self.mod, "_used_percent"):
            self.skipTest("helper renamed")
        self.assertEqual(self._pct_from({"percentage": 15}), 15.0)
        self.assertEqual(self._pct_from({"percentage": 0}), 0.0)
        self.assertEqual(self._pct_from({"percentage": 100}), 100.0)

    def test_out_of_range_is_rejected(self):
        if not hasattr(self.mod, "_used_percent"):
            self.skipTest("helper renamed")
        for value in (-3, 150):
            with self.subTest(value=value):
                self.assertIsNone(self._pct_from({"percentage": value}))


class ZaiSubscriptionTests(unittest.TestCase):
    """An item with neither `valid` nor `status` was returned as the plan."""

    def setUp(self):
        self.mod = load("zai")

    def test_live_status_is_accepted(self):
        self.assertEqual(
            self.mod._subscription_plan({"data": [{"productName": "Pro", "status": "ACTIVE"}]}),
            ("Pro", None))
        self.assertEqual(
            self.mod._subscription_plan({"data": [{"productName": "Pro", "valid": True}]}),
            ("Pro", None))

    def test_a_bare_canceled_item_is_not_the_active_plan(self):
        for item in ({"productName": "Canceled Plan"},
                     {"productName": "Canceled Plan", "status": "CANCELED"},
                     {"productName": "Canceled Plan", "valid": False}):
            with self.subTest(item=item):
                self.assertEqual(self.mod._subscription_plan({"data": [item]}), (None, None))

    def test_a_canceled_item_does_not_shadow_a_live_one(self):
        self.assertEqual(
            self.mod._subscription_plan({"data": [
                {"productName": "Canceled Plan"},
                {"productName": "Pro", "status": "ACTIVE"},
            ]}),
            ("Pro", None))


class KimiNestedFallbackTests(unittest.TestCase):
    """`.get(key, default)` ignores the default when the value is present-but-null."""

    def setUp(self):
        self.mod = load("kimi")

    def test_null_top_level_falls_back_to_detail(self):
        window = self.mod._parse_block(
            {"limit": None, "used": None, "remaining": None,
             "detail": {"limit": "100", "used": "57", "remaining": "43",
                        "resetTime": "2026-09-23T04:08:05.435444Z"}})
        self.assertEqual(window.used_percent, 57.0)
        self.assertEqual(window.reset_at, "2026-09-23T04:08:05.435444Z")

    def test_live_shape_is_unchanged(self):
        """The docstring's verified-live payload."""
        window = self.mod._parse_block(
            {"window": {"duration": 300}, "detail": {"limit": "100", "used": "57",
                                                     "remaining": "43"}})
        self.assertEqual(window.used_percent, 57.0)

    def test_top_level_values_still_win(self):
        window = self.mod._parse_block({"limit": 200, "used": 50})
        self.assertEqual(window.used_percent, 25.0)


class OptInFlagTests(unittest.TestCase):
    """`bool("false")` is True, so an explicit opt-out read as opt-in."""

    def setUp(self):
        self.base = load("base")

    def test_string_false_is_not_an_opt_in(self):
        for value in ("false", "False", "FALSE", "no", "0", "off", " false "):
            with self.subTest(value=value):
                self.assertFalse(self.base.opt_in_flag(value))

    def test_real_booleans_are_respected(self):
        self.assertTrue(self.base.opt_in_flag(True))
        self.assertFalse(self.base.opt_in_flag(False))

    def test_affirmative_strings_and_numbers(self):
        for value in ("true", "yes", "on", "1", 1, 1.0):
            with self.subTest(value=value):
                self.assertTrue(self.base.opt_in_flag(value))
        for value in ("", None, [], {}, "maybe", 0):
            with self.subTest(value=value):
                self.assertFalse(self.base.opt_in_flag(value))

    def _grok_enabled_via_config(self, value):
        mod = load("grok")
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {"grokEnabled": value}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}):
            with mock.patch.dict(mod._os.environ, {}, clear=True):
                return mod._grok_enabled()

    def test_grok_does_not_read_cookies_after_an_explicit_opt_out(self):
        """The sensitive-source rule: cookie readers are opt-in and default off."""
        for value in ("false", "no", "0", "off"):
            with self.subTest(value=value):
                self.assertFalse(self._grok_enabled_via_config(value))
        self.assertTrue(self._grok_enabled_via_config(True))
        self.assertTrue(self._grok_enabled_via_config("true"))

    def test_minimax_video_opt_in_is_equally_strict(self):
        mod = load("minimax")
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {"minimaxVideoEnabled": "false"}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}):
            with mock.patch.dict(mod.os.environ, {}, clear=True):
                self.assertFalse(mod._video_enabled())


class MiniMaxStatusCodeTests(unittest.TestCase):
    """`isinstance("1004", int)` is False, so the envelope check was skipped."""

    def setUp(self):
        self.mod = load("minimax")

    def test_string_status_code_is_still_an_error(self):
        payload = {"base_resp": {"status_code": "1004", "status_msg": "plan not found"}}
        self.assertEqual(self.mod.parse_quota_payload(payload).unavailable_reason,
                         "no-subscription")

    def test_int_status_code_behaves_the_same(self):
        payload = {"base_resp": {"status_code": 1004, "status_msg": "plan not found"}}
        self.assertEqual(self.mod.parse_quota_payload(payload).unavailable_reason,
                         "no-subscription")

    def test_zero_status_is_not_an_error(self):
        payload = {"base_resp": {"status_code": "0", "status_msg": "ok"},
                   "model_remains": [{"model_name": "general",
                                      "current_interval_remaining_percent": 96,
                                      "current_weekly_remaining_percent": 88}]}
        self.assertIsNone(self.mod.parse_quota_payload(payload).unavailable_reason)


class CopilotHasQuotaTests(unittest.TestCase):
    """A third clause made a quota the account lacks still draw a bar."""

    def setUp(self):
        self.mod = load("copilot")

    def test_no_quota_attached_is_skipped(self):
        self.assertIsNone(self.mod._parse_snapshot(
            {"percent_remaining": 50.0, "entitlement": 0, "has_quota": False}))

    def test_a_remaining_count_does_not_resurrect_a_missing_quota(self):
        self.assertIsNone(self.mod._parse_snapshot(
            {"percent_remaining": 50.0, "entitlement": 0, "quota_remaining": 50,
             "has_quota": False}))

    def test_a_real_quota_still_renders(self):
        self.assertEqual(self.mod._parse_snapshot(
            {"percent_remaining": 50.0, "entitlement": 200, "has_quota": True}), 50.0)

    def test_live_shape_percentages_are_unchanged(self):
        self.assertEqual(self.mod._parse_snapshot(
            {"percent_remaining": 40.5, "entitlement": 200, "has_quota": True}), 59.5)
        self.assertEqual(self.mod._parse_snapshot(
            {"percent_remaining": 100.0, "entitlement": 200, "has_quota": True}), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
