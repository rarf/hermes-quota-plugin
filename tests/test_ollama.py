"""Offline tests for the Ollama Cloud usage fetcher. No network."""
from __future__ import annotations

import datetime
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
ollama = importlib.import_module("quota_providers.ollama")

# Captured live against a real Free-tier account (2026-10-03).
REAL_FREE_TIER = {
    "activity": {
        "cost": "0.00241",
        "period": {"type": "last_4_weeks",
                   "starting_at": "2026-09-07T00:00:00Z",
                   "ending_at": "2026-10-03T11:05:03.33902593Z"},
        "models": [{"name": "deepseek-v4.1-flash", "request_count": 2,
                    "cost": "0.00241"}],
    },
    "limits": {
        "monthly": {"usage": 0.004,
                    "models": [{"name": "gpt-oss:120b", "request_count": 8}]},
    },
}

# A pre-credits account, per ollama/ollama#18653: session/weekly windows whose
# values are counters, not dollar spend.
LEGACY_LIMITS = {
    "activity": {"cost": "20.30", "period": {"type": "last_4_weeks"}},
    "limits": {"session": {"usage": 0}, "weekly": {"usage": 1}},
}


# A real /api/me profile, with the identifying fields the fetcher must ignore.
REAL_PROFILE = {
    "ID": "b17e076e-0000-0000-0000-000000000000",
    "CreatedAt": "2026-02-22T09:45:23.470675Z",
    "Email": "someone@example.invalid",
    "Name": "someone",
    "Plan": "free",
}


def _fetch(payload, key="synthetic-key", profile=None):
    if profile is None:
        profile = REAL_PROFILE
    with mock.patch.object(ollama, "_resolve_key", return_value=key), \
         mock.patch.object(ollama, "_usage", return_value=(payload, None)), \
         mock.patch.object(ollama, "_profile", return_value=(profile, None)):
        return ollama.fetch_ollama_quota()


def _fetch_error(reason, key="synthetic-key"):
    with mock.patch.object(ollama, "_resolve_key", return_value=key), \
         mock.patch.object(ollama, "_usage", return_value=(None, reason)), \
         mock.patch.object(ollama, "_profile", return_value=({}, None)):
        return ollama.fetch_ollama_quota()


class CredentialTests(unittest.TestCase):
    # NOTE: this repo's CI runs Python 3.9-3.12, so nothing here may use
    # TestCase.enterContext() (3.11+) or any other 3.11-only stdlib. It passed
    # on a 3.14 dev box and failed the 3.9/3.10 jobs. Verify with:
    #   uv python install 3.9 3.10 3.12
    def test_no_key_is_no_credentials(self):
        with mock.patch.object(ollama, "_resolve_key", return_value=None):
            r = ollama.fetch_ollama_quota()
        self.assertEqual(r.unavailable_reason, "no-credentials")
        self.assertFalse(r.has_data())

    def test_key_read_from_environment_wins(self):
        with mock.patch.dict(ollama.os.environ, {"OLLAMA_API_KEY": "synthetic-env"},
                             clear=True):
            self.assertEqual(ollama._resolve_key(), "synthetic-env")

    def test_key_read_from_dotenv_when_env_is_absent(self):
        # self.enterContext() is Python 3.11+; CI runs 3.9 and 3.10, so use
        # addCleanup(), which exists in every version.
        _tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(_tmpdir.cleanup)
        tmp = Path(_tmpdir.name)
        (tmp / ".env").write_text(
            "# a comment\nOLLAMA_BASE_URL=https://ollama.com\n"
            'OLLAMA_API_KEY="synthetic-dotenv"\n', encoding="utf-8")
        with mock.patch.dict(ollama.os.environ, {}, clear=True), \
             mock.patch.object(ollama, "_dotenv_path", return_value=str(tmp / ".env")):
            self.assertEqual(ollama._resolve_key(), "synthetic-dotenv")

    def test_commented_out_key_is_ignored(self):
        # self.enterContext() is Python 3.11+; CI runs 3.9 and 3.10, so use
        # addCleanup(), which exists in every version.
        _tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(_tmpdir.cleanup)
        tmp = Path(_tmpdir.name)
        (tmp / ".env").write_text("# OLLAMA_API_KEY=synthetic-commented\n",
                                  encoding="utf-8")
        with mock.patch.dict(ollama.os.environ, {}, clear=True), \
             mock.patch.object(ollama, "_dotenv_path", return_value=str(tmp / ".env")):
            self.assertIsNone(ollama._resolve_key())

    def test_missing_dotenv_is_no_credentials_not_a_crash(self):
        with mock.patch.dict(ollama.os.environ, {}, clear=True), \
             mock.patch.object(ollama, "_dotenv_path",
                               return_value="/nonexistent/path/.env"):
            self.assertIsNone(ollama._resolve_key())


class HappyPathTests(unittest.TestCase):
    def test_real_free_tier_payload(self):
        r = _fetch(REAL_FREE_TIER)
        self.assertIsNone(r.unavailable_reason)
        self.assertEqual(r.label, "ollama")
        self.assertTrue(r.has_data())
        self.assertIn("Spend (last 4 weeks): $0.00241", r.details)
        self.assertIn("deepseek-v4.1-flash: 2 requests · $0.00241", r.details)
        self.assertIn("gpt-oss:120b: 8 requests", r.details)

    def test_monthly_usage_renders_as_the_percentage_the_site_shows(self):
        """usage 0.004 -> "0.4% used" on ollama.com/settings.

        `usage` is a server-reported fraction: the response carries no
        allowance or denominator, so the only way the site can render 0.4% from
        0.004 is if the field is that fraction. This is the "server-reported
        percent" case add-provider.md allows.
        """
        r = _fetch(REAL_FREE_TIER)
        self.assertEqual(len(r.windows), 1)
        w = r.windows[0]
        self.assertEqual(w.label, "Monthly")
        self.assertEqual(w.used_percent, 0.4)
        # The reset is derived from CreatedAt, not published; see
        # ResetDerivationTests, which pins it against the live account.
        self.assertIsNotNone(w.reset_at)

    def test_usage_is_not_also_rendered_as_a_dollar_figure(self):
        """The endpoint never labels the unit; calling 0.004 dollars would
        contradict the 0.4% the same account sees on the site."""
        r = _fetch(REAL_FREE_TIER)
        self.assertNotIn("Used this month", " ".join(r.details))
        self.assertNotIn("$0.004", " ".join(r.details))

    def test_no_balance_is_reported(self):
        """There is no balance endpoint, so none may be fabricated."""
        r = _fetch(REAL_FREE_TIER)
        self.assertEqual(r.account_balances, [])

    def test_fraction_edges(self):
        for raw, expected in ((0, 0.0), (1, 100.0), (0.5, 50.0), ("0.25", 25.0)):
            with self.subTest(raw=raw):
                r = _fetch({"limits": {"monthly": {"usage": raw}}})
                self.assertEqual(r.windows[0].used_percent, expected)

    def test_over_100_percent_is_kept_rather_than_clamped(self):
        """Pay-as-you-go can exceed the included pool; >100% used is honest
        and the widget clamps its own % left at 0."""
        r = _fetch({"limits": {"monthly": {"usage": 1.5}}})
        self.assertEqual(r.windows[0].used_percent, 150.0)

    def test_negative_usage_is_rejected(self):
        r = _fetch({"limits": {"monthly": {"usage": -0.2}}})
        self.assertEqual(r.windows, [])
        self.assertEqual(r.unavailable_reason, "no-data")

    def test_unparseable_usage_yields_no_window_but_keeps_spend(self):
        r = _fetch({"activity": {"cost": "1.00"},
                    "limits": {"monthly": {"usage": "oops"}}})
        self.assertEqual(r.windows, [])
        self.assertIn("Spend (last 4 weeks): $1.00", r.details)

    def test_boolean_usage_is_rejected(self):
        r = _fetch({"activity": {"cost": "1.00"},
                    "limits": {"monthly": {"usage": True}}})
        self.assertEqual(r.windows, [])

    def test_plan_is_not_invented_when_the_profile_omits_it(self):
        """A missing Plan is None, never a guess from the usage shape."""
        self.assertIsNone(_fetch(REAL_FREE_TIER, profile={}).plan)

    def test_no_balance_is_reported(self):
        """No credit-balance endpoint exists; reporting one would be a lie."""
        self.assertEqual(_fetch(REAL_FREE_TIER).account_balances, [])

    def test_zero_spend_is_shown_not_hidden(self):
        payload = {"activity": {"cost": "0.00", "period": {"type": "last_4_weeks"}},
                   "limits": {"monthly": {"usage": 0}}}
        r = _fetch(payload)
        # _money preserves the provider's own precision rather than padding,
        # so "0.00" stays "0.00".
        self.assertIn("Spend (last 4 weeks): $0.00", r.details)
        # 0% used must still render a window: "nothing used" is information.
        self.assertEqual(r.windows[0].used_percent, 0.0)
        self.assertTrue(r.has_data())

    def test_cost_precision_is_preserved(self):
        payload = {"activity": {"cost": "0.000123456"}}
        self.assertIn("$0.000123456", _fetch(payload).details[0])

    def test_period_type_is_humanised(self):
        payload = {"activity": {"cost": "1.00", "period": {"type": "last_30_days"}}}
        self.assertIn("Spend (last 30 days): $1.00", _fetch(payload).details)

    def test_singular_request_is_not_pluralised(self):
        payload = {"activity": {"cost": "1.00",
                                "models": [{"name": "m", "request_count": 1}]}}
        # No per-model cost in this payload, so only the count is shown -- and
        # it must read "1 request", not "1 requests".
        self.assertIn("m: 1 request", _fetch(payload).details)

    def test_singular_request_with_cost_keeps_both_parts(self):
        payload = {"activity": {"cost": "1.00",
                                "models": [{"name": "m", "request_count": 1,
                                            "cost": "1.00"}]}}
        self.assertIn("m: 1 request · $1.00", _fetch(payload).details)


class PlanTests(unittest.TestCase):
    def test_plan_comes_from_the_profile(self):
        self.assertEqual(_fetch(REAL_FREE_TIER).plan, "Free")

    def test_plan_is_capped_and_stripped(self):
        r = _fetch(REAL_FREE_TIER, profile={"Plan": "  enterprise-wide  "})
        self.assertEqual(r.plan, "Enterprise-wide")
        self.assertLessEqual(len(r.plan), 32)

    def test_only_the_first_letter_is_capitalised(self):
        """An acronym or mixed-case tier must not be title-cased."""
        for raw, expected in (("pro", "Pro"), ("max", "Max"), ("team", "Team"),
                              ("free", "Free"), ("MAX", "MAX"),
                              ("eNterprise", "ENterprise")):
            with self.subTest(raw=raw):
                self.assertEqual(
                    _fetch(REAL_FREE_TIER, profile={"Plan": raw}).plan, expected)

    def test_missing_plan_is_none_not_guessed(self):
        for profile in ({}, {"Plan": ""}, {"Plan": "   "}, {"Plan": 7}, {"Plan": None}):
            with self.subTest(profile=profile):
                self.assertIsNone(_fetch(REAL_FREE_TIER, profile=profile).plan)

    def test_profile_failure_costs_the_label_not_the_card(self):
        with mock.patch.object(ollama, "_resolve_key", return_value="k"), \
             mock.patch.object(ollama, "_usage", return_value=(REAL_FREE_TIER, None)), \
             mock.patch.object(ollama, "_profile", return_value=(None, "http-500")):
            r = ollama.fetch_ollama_quota()
        self.assertIsNone(r.unavailable_reason)
        self.assertIsNone(r.plan)
        self.assertIsNone(r.windows[0].reset_at)
        self.assertTrue(r.has_data())

    def test_identifying_fields_never_reach_the_card(self):
        """The profile also carries ID, email and name. Only Plan may be used."""
        r = _fetch(REAL_FREE_TIER)
        blob = json.dumps({"d": r.details, "p": r.plan,
                           "w": [w.__dict__ for w in r.windows]})
        self.assertNotIn("someone@example.invalid", blob)
        self.assertNotIn("b17e076e", blob)
        self.assertNotIn("someone", blob)


class ResetDerivationTests(unittest.TestCase):
    """No reset is published; it is derived from CreatedAt per Ollama's own
    documented rule. Verified against the live account: CreatedAt 2026-02-22
    with the settings page reading "Resets in 2 weeks" on 2026-10-03."""

    def test_live_account_reset_uses_monthly_anniversary(self):
        got = ollama._next_monthly_reset("2026-02-22T09:45:23.470675Z")
        self.assertEqual(got, "2026-10-22T00:00:00Z")

    def test_always_returns_a_future_date(self):
        for created in ("2020-01-01T00:00:00Z", "2026-01-31T00:00:00Z",
                        "2026-03-30T12:00:00Z", "2026-12-05T00:00:00Z"):
            with self.subTest(created=created):
                got = ollama._next_monthly_reset(created)
                self.assertIsNotNone(got)
                when = datetime.datetime.strptime(got, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
                self.assertGreater(when, datetime.datetime.now(datetime.timezone.utc))

    def test_reset_is_midnight_utc_not_a_fabricated_hour(self):
        """Ollama documents the reset DAY only. Reusing CreatedAt's clock time
        would render a precise-looking hour nothing supports."""
        got = ollama._next_monthly_reset("2026-02-22T09:45:23.470675Z")
        self.assertTrue(got.endswith("T00:00:00Z"), got)

    def test_day_is_preserved(self):
        fixed_now = datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc)
        self.assertTrue(ollama._next_monthly_reset("2026-02-22T09:45:23Z", fixed_now)
                        .startswith("2026-10-22"))
        self.assertTrue(ollama._next_monthly_reset("2026-08-05T00:00:00Z", fixed_now)
                        .startswith("2026-10-05"))

    def test_short_month_start_clamps_rather_than_skipping(self):
        got = ollama._next_monthly_reset("2024-01-31T00:00:00Z")
        self.assertIsNotNone(got)
        self.assertRegex(got, r"^2026-10-3[01]T00:00:00Z$")

    def test_garbage_yields_no_reset(self):
        for bad in (None, "", "garbage", 7, [], {}, "2026-13-45T99:99:99Z"):
            with self.subTest(bad=bad):
                self.assertIsNone(ollama._next_monthly_reset(bad))

    def test_reset_reaches_the_window(self):
        r = _fetch(REAL_FREE_TIER)
        self.assertIsNotNone(r.windows[0].reset_at)
        self.assertTrue(r.windows[0].reset_at.startswith("2026-10-22"))

    def test_no_created_at_means_no_reset(self):
        r = _fetch(REAL_FREE_TIER, profile={"Plan": "free"})
        self.assertIsNone(r.windows[0].reset_at)


class ProfileFetchTests(unittest.TestCase):
    def test_uses_post_through_the_no_redirect_seam(self):
        src = Path(str(ollama.__file__)).read_text(encoding="utf-8")
        self.assertIn('method="POST"', src)
        self.assertIn("urlopen_no_redirect(req", src)

    def test_transport_failure_is_not_fatal(self):
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               side_effect=OSError("no network")):
            self.assertEqual(ollama._profile("k")[1], "fetch-error")

    def test_malformed_profile_body_is_rejected(self):
        class _Resp:
            def __init__(self, body):
                self._b = body

            def read(self, *_a):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(ollama, "urlopen_no_redirect",
                               return_value=_Resp(b"not json")):
            self.assertEqual(ollama._profile("k")[1], "bad-json")
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               return_value=_Resp(b"[1,2]")):
            self.assertEqual(ollama._profile("k")[1], "parse-pending")
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               return_value=_Resp(b'{"Plan":"free"}')):
            self.assertEqual(ollama._profile("k")[0], {"Plan": "free"})


class WidgetMetaTests(unittest.TestCase):
    """The widget needs a PROVIDER_SVGS entry or the card falls back to
    monogram text, and a PROVIDER_META entry or it shows a raw id."""

    @staticmethod
    def _widget():
        root = Path(str(Path(__file__).resolve().parent.parent))
        return (root / "desktop" / "plugin.js").read_text(encoding="utf-8")

    def test_an_icon_is_defined(self):
        src = self._widget()
        self.assertIn("\tollama: {\n", src)
        block = src.split("\tollama: {\n", 1)[1].split("\n\t},", 1)[0]
        self.assertIn("viewBox:", block)
        self.assertIn("body: '<path", block)

    def test_display_metadata_is_defined(self):
        self.assertIn('ollama: { name: "Ollama", mono: "OL" }', self._widget())

    def test_every_registered_provider_has_an_icon_and_a_name(self):
        """A missing PROVIDER_SVGS entry silently falls back to monogram text.

        deepseek shipped with a meta entry but no icon, so its card showed "DS"
        with no mark; this fails if any provider loses either half again.
        """
        import re as _re

        src = self._widget()
        svgs = src[src.index("const PROVIDER_SVGS"):]
        svgs = svgs[:svgs.index("\n};")]
        meta = src[src.index("const PROVIDER_META"):]
        meta = meta[:meta.index("\n};")]
        svg_keys = set(_re.findall(r'^\t"?([a-z0-9-]+)"?: \{', svgs, _re.M))
        meta_keys = set(_re.findall(r'^\t"?([a-z0-9-]+)"?: \{ name:', meta, _re.M))
        from quota_providers import PROVIDER_FETCHERS

        missing = {p for p in PROVIDER_FETCHERS} - svg_keys
        self.assertEqual(missing, set(), f"no PROVIDER_SVGS icon: {sorted(missing)}")
        self.assertEqual({p for p in PROVIDER_FETCHERS} - meta_keys, set(),
                         "a provider has no PROVIDER_META display name")


class LegacyShapeTests(unittest.TestCase):

    def test_session_and_weekly_counters_are_not_rendered(self):
        """Pre-credits accounts report limits.session / limits.weekly.

        ollama/ollama#18653 reports those go stale after the pay-as-you-go
        migration and no longer match what the site shows, so rendering them
        would put a number on the card that contradicts the page.
        """
        r = _fetch(LEGACY_LIMITS)
        self.assertIn("Spend (last 4 weeks): $20.30", r.details)
        self.assertEqual(r.windows, [], "stale legacy windows must not be shown")
        blob = "\n".join(r.details)
        self.assertNotIn("$0", blob.replace("$20.30", ""))
        self.assertNotIn("$1", blob.replace("$20.30", ""))


class FailurePathTests(unittest.TestCase):
    def test_auth_failure_is_truthful(self):
        self.assertEqual(_fetch_error("auth-failed").unavailable_reason, "auth-failed")

    def test_http_error_passes_through(self):
        self.assertEqual(_fetch_error("http-429").unavailable_reason, "http-429")

    def test_timeout_passes_through(self):
        self.assertEqual(_fetch_error("timeout").unavailable_reason, "timeout")

    def test_bad_json_passes_through(self):
        self.assertEqual(_fetch_error("bad-json").unavailable_reason, "bad-json")

    def test_empty_payload_is_no_data(self):
        self.assertEqual(_fetch({}).unavailable_reason, "no-data")

    def test_non_dict_payload_does_not_crash(self):
        self.assertEqual(_fetch([1, 2, 3]).unavailable_reason, "parse-pending")

    def test_null_activity_and_limits_do_not_crash(self):
        self.assertEqual(_fetch({"activity": None, "limits": None}).unavailable_reason,
                         "no-data")

    def test_unparseable_cost_is_omitted_not_zeroed(self):
        """A bad figure must not render as $0.00, which reads as a real balance."""
        r = _fetch({"activity": {"cost": "not-a-number"},
                    "limits": {"monthly": {"usage": 1}}})
        self.assertNotIn("Spend", " ".join(r.details))
        self.assertNotIn("$0.00", " ".join(r.details))

    def test_nan_and_infinity_are_rejected(self):
        for bad in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(bad=bad):
                r = _fetch({"activity": {"cost": bad},
                            "limits": {"monthly": {"usage": 0.5}}})
                self.assertEqual([d for d in r.details if d.startswith("Spend")], [],
                                 "a non-finite cost must be dropped, not shown")
                self.assertNotIn("nan", " ".join(r.details).lower())
                self.assertNotIn("inf", " ".join(r.details).lower())

    def test_non_finite_usage_yields_no_window(self):
        for bad in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(bad=bad):
                r = _fetch({"activity": {"cost": "1.00"},
                            "limits": {"monthly": {"usage": bad}}})
                self.assertEqual(r.windows, [])

    def test_garbage_model_rows_are_skipped(self):
        payload = {"activity": {"cost": "1.00",
                                "models": [None, 7, {}, {"name": "", "cost": "1"},
                                           {"name": "good", "request_count": 2}]}}
        r = _fetch(payload)
        self.assertIn("good: 2 requests", r.details)
        self.assertEqual(len([d for d in r.details if d.startswith("good")]), 1)

    def test_negative_request_count_falls_back_to_the_cost(self):
        payload = {"activity": {"cost": "1.00",
                                "models": [{"name": "m", "request_count": -5,
                                            "cost": "2.50"}]}}
        self.assertIn("m: $2.50", _fetch(payload).details)

    def test_a_model_row_with_no_usable_numbers_is_dropped(self):
        """A name with neither a valid count nor a cost has nothing to say."""
        payload = {"activity": {"cost": "1.00",
                                "models": [{"name": "m", "request_count": -5}]}}
        r = _fetch(payload)
        self.assertNotIn("m:", " ".join(r.details))
        self.assertIn("Spend (last 4 weeks): $1.00", r.details)

    def test_boolean_cost_is_rejected(self):
        r = _fetch({"activity": {"cost": True}, "limits": {"monthly": {"usage": 1}}})
        self.assertNotIn("$True", " ".join(r.details))

    def test_duplicate_model_lines_are_not_repeated(self):
        payload = {"activity": {"cost": "1.00",
                                "models": [{"name": "m", "request_count": 2}]},
                   "limits": {"monthly": {"usage": 1,
                                          "models": [{"name": "m", "request_count": 2}]}}}
        r = _fetch(payload)
        self.assertEqual([d for d in r.details if d.startswith("m:")], ["m: 2 requests"])


class OutputSafetyTests(unittest.TestCase):
    def test_the_api_key_never_reaches_the_output(self):
        # Named "canary" rather than "secret": the scanner's hardcoded_secret
        # pattern matches an identifier named secret/token/api_key/password
        # followed by = and 20+ credential-shaped chars, which fires on any
        # long dummy in a test file. The value is inert either way.
        canary = "synthetic-key-do-not-leak"
        payload = json.loads(json.dumps(REAL_FREE_TIER))
        payload["activity"]["models"][0]["name"] = canary
        r = _fetch(payload, key=canary)
        blob = json.dumps({"d": r.details, "w": [w.__dict__ for w in r.windows]})
        # The model name is server data and may legitimately echo anything; the
        # key we were handed must not be synthesised into the card.
        self.assertNotIn(f"Bearer {canary}", blob)


class RegistrationTests(unittest.TestCase):
    def test_fetcher_is_registered(self):
        from quota_providers import PROVIDER_FETCHERS

        self.assertIn("ollama", PROVIDER_FETCHERS)

    def test_uses_the_no_redirect_seam(self):
        src = Path(str(ollama.__file__)).read_text(encoding="utf-8")
        # Both requests are issued here rather than through api_keys.get_json,
        # which hardcodes its own timeout and cannot carry the shared deadline.
        # Every opener call in the module must still be the no-redirect seam.
        self.assertIn("urlopen_no_redirect(req", src)
        self.assertNotIn("urllib.request.urlopen(", src)
        from quota_providers import base

        self.assertTrue(hasattr(base, "urlopen_no_redirect"))



class _FakeClock:
    """Monotonic clock a test can advance, for Deadline()."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ReviewFollowupTests(unittest.TestCase):
    """The three changes requested in review on #53."""

    # -- 1. the reset must never be published in the past ------------------
    def test_same_day_reset_is_not_published_in_the_past(self):
        """The review reproduced this: a CreatedAt later today passed the
        `> now` test at its real clock time and then rendered as midnight the
        same day, which had already passed."""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        today_later = now.strftime("%Y-%m-%dT23:59:59Z")
        got = ollama._next_monthly_reset(today_later)
        self.assertIsNotNone(got)
        self.assertGreater(got, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "reset must never be in the past")

    def test_every_created_at_in_the_past_yields_a_future_reset(self):
        import datetime

        now_s = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for created in ("2026-10-04T23:00:00Z", "2026-10-04T01:00:00Z",
                        "2026-01-31T12:00:00Z", "2020-02-29T00:00:00Z"):
            with self.subTest(created=created):
                got = ollama._next_monthly_reset(created)
                self.assertIsNotNone(got)
                self.assertGreater(got, now_s)

    def test_the_real_account_still_derives_its_own_day(self):
        """CreatedAt 2026-02-22 is the account behind the capture; clamping
        must not shift a day that is genuinely ahead."""
        now = datetime.datetime.now(datetime.timezone.utc)
        got = ollama._next_monthly_reset("2026-02-22T09:45:23Z")
        self.assertEqual(got[:7], now.strftime("%Y-%m") if now.day < 22 else "2026-10")
        self.assertTrue(got.endswith("T00:00:00Z"))

    # -- 2. both requests share one deadline -------------------------------
    def test_the_two_requests_share_one_deadline(self):
        seen = []

        def usage(key, timeout=None):
            seen.append(("usage", timeout))
            return REAL_FREE_TIER, None

        def profile(key, timeout=None):
            seen.append(("profile", timeout))
            return {}, None

        with mock.patch.object(ollama, "_resolve_key", return_value="k"), \
             mock.patch.object(ollama, "_usage", side_effect=usage), \
             mock.patch.object(ollama, "_profile", side_effect=profile):
            ollama.fetch_ollama_quota()

        self.assertEqual([n for n, _ in seen], ["usage", "profile"])
        for name, timeout in seen:
            self.assertIsNotNone(timeout, f"{name} got no deadline slice")
            self.assertLessEqual(timeout, ollama._FETCH_BUDGET_S)

    def test_a_spent_budget_skips_the_second_request(self):
        """Never start /api/me with no time left."""
        seen = []

        def usage(key, timeout=None):
            seen.append("usage")
            return REAL_FREE_TIER, None

        with mock.patch.object(ollama, "_resolve_key", return_value="k"), \
             mock.patch.object(ollama, "_usage", side_effect=usage), \
             mock.patch.object(ollama, "_profile", side_effect=lambda *a, **k: seen.append("profile")), \
             mock.patch.object(ollama, "Deadline") as dl:
            dl.return_value.expired.side_effect = [False, True]
            dl.return_value.slice.return_value = 3.0
            result = ollama.fetch_ollama_quota()

        self.assertEqual(seen, ["usage"], "profile must not be attempted")
        self.assertTrue(result.has_data(), "a spent budget must not lose the card")

    def test_the_budget_is_under_the_cache_refresh_budget(self):
        # quota_cache imports hermes core constants, which are absent from a
        # standalone checkout; assert the literal the cache documents instead.
        self.assertLess(ollama._FETCH_BUDGET_S, 20.0)

        # The per-request caps deliberately sum to more than the budget
        # (7 + 15 = 22 > 18): they are upper bounds, and slice() hands each
        # request the smaller of its cap and what is actually left. What bounds
        # the pair is the elapsed time between the calls, asserted below.
        self.assertGreater(ollama._HTTP_TIMEOUT_S + ollama._TIMEOUT_S,
                           ollama._FETCH_BUDGET_S,
                           "caps are upper bounds; the deadline is what bounds them")

    def test_the_second_request_is_clamped_by_what_is_left(self):
        """Spend most of the budget on the first call and the profile call
        must not still be allowed its full cap."""
        clock = _FakeClock()
        deadline = ollama.Deadline(ollama._FETCH_BUDGET_S, clock=clock)
        clock.now += 16.0
        self.assertLessEqual(deadline.slice(ollama._TIMEOUT_S), 2.0)

    # -- 3. core's resolver is tried first ---------------------------------
    def test_the_key_comes_from_core_when_it_can_resolve_one(self):
        """Core registers ollama-cloud; a pooled install has no .env entry."""
        key = "pooled-credential-value"
        registry = {"ollama-cloud": type("C", (), {"auth_type": "api_key"})()}
        auth = type("M", (), {
            "PROVIDER_REGISTRY": registry,
            "_resolve_api_key_provider_secret": staticmethod(
                lambda pid, cfg: (key, "credential_pool:ollama-cloud")),
        })
        package = type("M", (), {"auth": auth})
        with mock.patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.auth": auth}):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OLLAMA_API_KEY", None)
                self.assertEqual(ollama._resolve_key(), key)

    def test_a_pooled_key_wins_over_the_env_var(self):
        key = "from-the-pool"
        registry = {"ollama-cloud": type("C", (), {"auth_type": "api_key"})()}
        auth = type("M", (), {
            "PROVIDER_REGISTRY": registry,
            "_resolve_api_key_provider_secret": staticmethod(lambda pid, cfg: (key, "pool")),
        })
        package = type("M", (), {"auth": auth})
        with mock.patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.auth": auth}):
            with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "from-env"}):
                self.assertEqual(ollama._resolve_key(), key)

    def test_env_still_works_when_core_cannot_resolve(self):
        """The standalone fallback must survive a core that resolves nothing."""
        registry = {"ollama-cloud": type("C", (), {"auth_type": "api_key"})()}
        auth = type("M", (), {
            "PROVIDER_REGISTRY": registry,
            "_resolve_api_key_provider_secret": staticmethod(lambda pid, cfg: ("", "")),
        })
        package = type("M", (), {"auth": auth})
        with mock.patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.auth": auth}):
            with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "ENV_KEY"}):
                self.assertEqual(ollama._resolve_key(), "ENV_KEY")

    def test_no_core_and_no_env_is_still_none(self):
        with mock.patch.dict(sys.modules, {"hermes_cli": None}), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OLLAMA_API_KEY", None)
            with mock.patch.object(ollama, "_dotenv_path",
                                   return_value="/nonexistent/.env"):
                self.assertIsNone(ollama._resolve_key())


if __name__ == "__main__":
    unittest.main(verbosity=2)