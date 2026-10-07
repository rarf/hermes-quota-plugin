"""Offline tests for the Ollama Cloud usage fetcher. No network.

The provider reads two endpoints (both documented in ollama/ollama#18829,
`server: proxy cloud usage and balance APIs`, merged 2026-10-07):

  GET /api/usage?range=30d  -> totals + daily buckets (spend, requests, tokens)
  GET /api/balance          -> included allowance/balance/period, purchased credit

`/api/balance` also carries `period.until`, the account's real reset, so no
reset is derived any more. Legacy pre-credits plans report `included.session` /
`included.weekly` with Ollama's own `remaining_percent` and `resets_at` instead
of `balance_usd`/`allowance_usd`.
"""
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

# Captured live against a real Free-tier account (2026-10-07), via /api/usage.
REAL_USAGE = {
    "range": "30d",
    "scope": "self",
    "granularity": "day",
    "from": "2026-09-07T00:00:00Z",
    "until": "2026-10-07T05:52:38Z",
    "totals": {
        "request_count": 18,
        "usage_usd": 0.06348,
        "input_tokens": 673390,
        "cached_input_tokens": 392224,
        "output_tokens": 8111,
    },
    "buckets": [
        {"from": "2026-09-30T00:00:00Z", "until": "2026-10-01T00:00:00Z",
         "request_count": 0, "usage_usd": 0},
        {"from": "2026-10-03T00:00:00Z", "until": "2026-10-04T00:00:00Z",
         "request_count": 18, "usage_usd": 0.06348},
        {"from": "2026-10-07T00:00:00Z", "until": "2026-10-07T05:52:38Z",
         "partial": True, "request_count": 0, "usage_usd": 0},
    ],
}

# Captured live from the same account, via /api/balance. The included pool
# ($2.50 allowance) and the purchased wallet ($5.00) are SEPARATE money: the
# dashboard shows the first as "2.4% used" and the second as "Current balance".
REAL_BALANCE = {
    "included": {
        "balance_usd": 2.43893,
        "allowance_usd": 2.5,
        "period": {
            "from": "2026-09-22T09:45:23.470675Z",
            "until": "2026-10-22T09:45:23.470675Z",
        },
    },
    "purchased": {"balance_usd": 4.99759},
}

# A pre-credits account, per the legacy section of docs/api/balance.mdx: the
# percent and the reset both come from the provider, unconverted.
LEGACY_BALANCE = {
    "included": {
        "session": {"remaining_percent": 75, "resets_at": "2026-10-01T07:00:00Z"},
        "weekly": {"remaining_percent": 40, "resets_at": "2026-10-05T00:00:00Z"},
    },
    "purchased": {"balance_usd": 25},
}

# A real /api/me profile, with the identifying fields the fetcher must ignore.
REAL_PROFILE = {
    "ID": "b17e076e-0000-0000-0000-000000000000",
    "CreatedAt": "2026-02-22T09:45:23.470675Z",
    "Email": "someone@example.invalid",
    "Name": "someone",
    "Plan": "free",
}


_DEFAULT = object()  # sentinel: `profile=None` must mean "no profile", not "default"


def _fetch(usage=None, balance=None, key="synthetic-key", profile=_DEFAULT,
           usage_error=None, balance_error=None, profile_error=None):
    """Run the fetcher with all three network calls stubbed."""
    if profile is _DEFAULT:
        profile = REAL_PROFILE
    with mock.patch.object(ollama, "_resolve_key", return_value=key), \
         mock.patch.object(ollama, "_usage",
                           return_value=(usage, usage_error)), \
         mock.patch.object(ollama, "_balance",
                           return_value=(balance, balance_error)), \
         mock.patch.object(ollama, "_profile",
                           return_value=(profile, profile_error)):
        return ollama.fetch_ollama_quota()


def _fetch_real(**kwargs):
    """The captured live account."""
    kwargs.setdefault("usage", REAL_USAGE)
    kwargs.setdefault("balance", REAL_BALANCE)
    return _fetch(**kwargs)


def _fetch_error(reason, key="synthetic-key"):
    """Both endpoints fail with the same reason."""
    return _fetch(usage=None, balance=None, usage_error=reason,
                  balance_error=reason, key=key)


class _FakeClock:
    """Monotonic clock a test can advance, for Deadline()."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeResponse:
    """A urlopen_no_redirect stand-in returning a fixed body."""

    def __init__(self, body):
        self._b = body

    def read(self, *_a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


# A syntactically valid URL: urllib parses the Request before the opener seam
# ever sees it, so a bare "u" fails in the wrong place.
_TEST_URL = "https://ollama.com/api/balance"


def _http_error(code):
    import urllib.error

    def _raise(url, timeout=None):
        raise urllib.error.HTTPError(_TEST_URL, code, "err", {}, None)

    return _raise


# ---------------------------------------------------------------------------
# the card the live account now produces
# ---------------------------------------------------------------------------

class LiveAccountCardTests(unittest.TestCase):
    def test_the_captured_account_renders_a_window(self):
        r = _fetch_real()
        self.assertIsNone(r.unavailable_reason)
        self.assertEqual(r.label, "ollama")
        self.assertTrue(r.has_data())
        self.assertEqual(len(r.windows), 1)
        w = r.windows[0]
        self.assertEqual(w.label, "Included credits")
        self.assertEqual(w.used_percent, 2.44)
        self.assertEqual(w.remaining_pct(), 98)

    def test_the_reset_is_the_published_one_not_a_derived_day(self):
        """`period.until` is the account's real reset. The dashboard read
        "Resets in 2 weeks" on 2026-10-07 for this same account, which is what
        the old CreatedAt derivation was checked against and could not prove."""
        got = _fetch_real().windows[0].reset_at
        self.assertEqual(got, "2026-10-22T09:45:23Z")
        when = datetime.datetime.strptime(got, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        days = (when - datetime.datetime.now(datetime.timezone.utc)).days
        self.assertEqual(days // 7, 2, "the dashboard says 'Resets in 2 weeks'")

    def test_the_percentage_matches_the_dashboards_own_figure(self):
        """$2.44 of a $2.50 allowance -> 2.44% used, shown as "2.4% used"."""
        r = _fetch_real()
        self.assertEqual(r.windows[0].used_percent, 2.44)
        self.assertAlmostEqual(round(r.windows[0].used_percent, 1), 2.4, places=3)

    def test_account_balance_is_the_purchased_wallet_not_the_allowance(self):
        """The widget labels this row "Account balance", so it must be the
        figure ollama.com/settings labels "Current balance" -- purchased
        credit. Showing the included pool here contradicted the page."""
        rows = _fetch_real().account_balances
        self.assertEqual([(b.currency, b.total_balance) for b in rows],
                         [("USD", "5.00")])

    def test_the_two_pools_are_both_named_in_the_detail_lines(self):
        details = _fetch_real().details
        self.assertIn("Included credits: $2.44 of $2.50 remaining", details)
        self.assertIn("Purchased credits: $5.00 (does not refill at reset)", details)

    def test_total_spend_is_labelled_as_spanning_both_pools(self):
        """`totals.usage_usd` covers included *and* purchased credit, so it is
        not the same number as the percentage's base ($0.06348 vs $0.06107 --
        the difference is the two paid deepseek requests). Calling it plain
        "Spend" implied the two matched."""
        details = _fetch_real().details
        self.assertIn("Total spend (30d): $0.06", details)
        self.assertIn("Total spend covers included and purchased credits; "
                      "the percentage above is the included share only", details)

    def test_requests_and_tokens_come_from_totals(self):
        details = _fetch_real().details
        self.assertIn("Requests (30d): 18", details)
        self.assertIn("Tokens (30d): 673,390 in · 8,111 out", details)

    def test_per_model_is_pointed_at_not_faked(self):
        """There is no per-model breakdown in the API yet, but the dashboard
        renders one -- so the card says where it lives instead of implying the
        data is unobtainable."""
        self.assertIn("Per-model usage is shown on ollama.com/settings, "
                      "not exposed by the API", _fetch_real().details)

    def test_the_plan_badge_is_read_from_the_profile(self):
        self.assertEqual(_fetch_real().plan, "Free")


# ---------------------------------------------------------------------------
# percent derivation
# ---------------------------------------------------------------------------

class PercentDerivationTests(unittest.TestCase):
    def test_percent_is_the_share_drawn_from_the_allowance(self):
        self.assertEqual(
            ollama._included_spend_percent({"balance_usd": 2.43893,
                                             "allowance_usd": 2.5}), 2.44)

    def test_edges(self):
        for balance, allowance, expected in (
            (0, 2.5, 100.0),        # fully drawn
            (2.5, 2.5, 0.0),        # untouched
            (1.25, 2.5, 50.0),      # half drawn
            (-1.25, 2.5, 150.0),    # pay-as-you-go overspend
        ):
            with self.subTest(balance=balance, allowance=allowance):
                self.assertEqual(
                    ollama._included_spend_percent(
                        {"balance_usd": balance, "allowance_usd": allowance}),
                    expected)

    def test_a_zero_allowance_has_no_denominator(self):
        self.assertIsNone(
            ollama._included_spend_percent({"balance_usd": 1, "allowance_usd": 0}))

    def test_a_missing_side_yields_no_window_rather_than_a_guess(self):
        self.assertIsNone(ollama._included_spend_percent({"allowance_usd": 2.5}))
        self.assertIsNone(ollama._included_spend_percent({"balance_usd": 1}))

    def test_negative_allowance_is_rejected(self):
        self.assertIsNone(
            ollama._included_spend_percent({"balance_usd": 1, "allowance_usd": -2.5}))

    def test_non_finite_and_boolean_figures_are_rejected(self):
        for bad in (float("nan"), float("inf"), -float("inf"), True, "abc", None):
            with self.subTest(bad=bad):
                self.assertIsNone(
                    ollama._included_spend_percent(
                        {"balance_usd": bad, "allowance_usd": 2.5}))
                self.assertIsNone(
                    ollama._included_spend_percent(
                        {"balance_usd": 1, "allowance_usd": bad}))

    def test_string_figures_are_accepted(self):
        self.assertEqual(
            ollama._included_spend_percent({"balance_usd": "2.5",
                                             "allowance_usd": "5"}), 50.0)


# ---------------------------------------------------------------------------
# windows and resets, straight from the provider
# ---------------------------------------------------------------------------

class WindowTests(unittest.TestCase):
    def test_the_window_carries_the_published_reset(self):
        self.assertEqual(
            ollama._windows(REAL_BALANCE["included"])[0].reset_at,
            "2026-10-22T09:45:23Z")

    def test_an_offset_timestamp_is_normalised_to_utc(self):
        w = ollama._windows({"balance_usd": 1, "allowance_usd": 2,
                             "period": {"until": "2026-10-22T09:45:23+02:00"}})[0]
        self.assertEqual(w.reset_at, "2026-10-22T07:45:23Z")

    def test_an_unparseable_reset_is_dropped_not_rendered(self):
        """The reset is echoed into the cache and shown as a countdown, so a
        value nothing can read must not become a reset that never arrives."""
        for bad in ("soon", "", None, 7, [], {}, "2026-13-45T99:99:99Z"):
            with self.subTest(bad=bad):
                w = ollama._windows({"balance_usd": 1, "allowance_usd": 2,
                                     "period": {"until": bad}})[0]
                self.assertIsNone(w.reset_at)

    def test_no_period_means_a_percent_with_no_reset(self):
        w = ollama._windows({"balance_usd": 1, "allowance_usd": 2})[0]
        self.assertEqual(w.used_percent, 50.0)
        self.assertIsNone(w.reset_at)

    def test_garbage_included_blocks_yield_no_windows(self):
        for bad in (None, {}, [], "x", 7):
            with self.subTest(bad=bad):
                self.assertEqual(ollama._windows(bad), [])

    def test_legacy_windows_report_the_providers_own_percent(self):
        """`remaining_percent` is Ollama's own number, so it is passed through
        as used=100-remaining rather than re-derived from dollars."""
        ws = ollama._windows(LEGACY_BALANCE["included"])
        self.assertEqual([w.label for w in ws], ["Session", "Weekly"])
        self.assertEqual(ws[0].used_percent, 25.0)
        self.assertEqual(ws[1].used_percent, 60.0)
        self.assertEqual(ws[0].reset_at, "2026-10-01T07:00:00Z")
        self.assertEqual(ws[1].reset_at, "2026-10-05T00:00:00Z")

    def test_legacy_resets_are_normalised_too(self):
        ws = ollama._windows(
            {"session": {"remaining_percent": 50,
                         "resets_at": "2026-10-01T07:00:00+02:00"}})
        self.assertEqual(ws[0].reset_at, "2026-10-01T05:00:00Z")

    def test_a_legacy_window_without_a_percent_still_renders(self):
        """A missing percent is not a reason to hide the reset."""
        ws = ollama._windows({"weekly": {"resets_at": "2026-10-05T00:00:00Z"}})
        self.assertEqual(len(ws), 1)
        self.assertIsNone(ws[0].used_percent)
        self.assertEqual(ws[0].reset_at, "2026-10-05T00:00:00Z")

    def test_both_window_kinds_can_appear_together(self):
        ws = ollama._windows({"balance_usd": 1.25, "allowance_usd": 2.5,
                              "period": {"until": "2026-10-22T00:00:00Z"},
                              "session": {"remaining_percent": 75,
                                          "resets_at": "2026-10-01T07:00:00Z"}})
        self.assertEqual([w.label for w in ws], ["Included credits", "Session"])


# ---------------------------------------------------------------------------
# money formatting
# ---------------------------------------------------------------------------

class MoneyFormatTests(unittest.TestCase):
    def test_mills_are_dropped_for_display(self):
        """`balance_usd` carries per-token rounding precision (2.43893), which
        reads as noise in a headline figure."""
        self.assertEqual(ollama._dollars(2.43893), "2.44")
        self.assertEqual(ollama._dollars(4.99759), "5.00")
        self.assertEqual(ollama._dollars(2.5), "2.50")
        self.assertEqual(ollama._dollars(25), "25.00")
        self.assertEqual(ollama._dollars(0.06348), "0.06")

    def test_a_sub_cent_balance_keeps_digits_rather_than_reading_as_empty(self):
        """A nonzero balance rounding to $0.00 would read as "no money left",
        so extra digits are kept rather than padding the field with zeros."""
        self.assertEqual(ollama._dollars(0.00241), "0.0024")
        self.assertEqual(ollama._dollars(0), "0.00")

    def test_negative_overspend_is_formatted(self):
        self.assertEqual(ollama._dollars(-1.25), "-1.25")

    def test_bad_figures_yield_none(self):
        for bad in ("nope", None, True, float("nan"), float("inf"), [], {}):
            with self.subTest(bad=bad):
                self.assertIsNone(ollama._dollars(bad))

    def test_the_raw_formatter_is_unchanged(self):
        """`_money` keeps the endpoint's own digits; `_dollars` rounds for
        display. Both stay available."""
        self.assertEqual(ollama._money(2.43893), "2.43893")

    def test_no_displayed_figure_leaks_five_decimals(self):
        """Regression guard: the headline balance row rendered $4.99759."""
        texts = ["\n".join(_fetch_real().details)]
        texts.append("\n".join(b.total_balance
                               for b in _fetch_real().account_balances))
        for text in texts:
            for line in text.splitlines():
                for token in line.split():
                    if token.startswith("$") or token.replace(".", "").isdigit():
                        with self.subTest(token=token):
                            self.assertLessEqual(
                                len(token.split(".")[-1]), 4,
                                "unrounded money on the card")


# ---------------------------------------------------------------------------
# the two endpoints, independently
# ---------------------------------------------------------------------------

class EndpointIndependenceTests(unittest.TestCase):
    def test_a_usage_failure_keeps_the_window(self):
        r = _fetch(usage=None, balance=REAL_BALANCE, usage_error="http-429")
        self.assertIsNone(r.unavailable_reason)
        self.assertEqual(r.windows[0].used_percent, 2.44)
        self.assertEqual([d for d in r.details
                          if d.startswith("Total spend (")], [])

    def test_a_balance_failure_keeps_the_spend_lines(self):
        """Both endpoints feed the card; one failing must not discard the
        other's real data."""
        r = _fetch(usage=REAL_USAGE, balance=None, balance_error="http-500")
        self.assertIsNone(r.unavailable_reason)
        self.assertIn("Total spend (30d): $0.06", r.details)
        self.assertIn("Requests (30d): 18", r.details)
        self.assertEqual(r.windows, [], "no allowance means no percentage")

    def test_both_failing_is_a_dead_provider_naming_the_first_reason(self):
        self.assertEqual(_fetch_error("http-500").unavailable_reason, "http-500")

    def test_a_profile_failure_costs_the_label_not_the_card(self):
        r = _fetch(usage=REAL_USAGE, balance=REAL_BALANCE, profile=None,
                   profile_error="http-500")
        self.assertIsNone(r.unavailable_reason)
        self.assertIsNone(r.plan)
        self.assertTrue(r.has_data())
        self.assertEqual(r.windows[0].reset_at, "2026-10-22T09:45:23Z")

    def test_an_empty_balance_block_still_leaves_a_usage_card(self):
        r = _fetch(usage=REAL_USAGE, balance={})
        self.assertIsNone(r.unavailable_reason)
        self.assertIn("Total spend (30d): $0.06", r.details)
        self.assertEqual(r.windows, [])


# ---------------------------------------------------------------------------
# plan badge
# ---------------------------------------------------------------------------

class PlanTests(unittest.TestCase):
    def test_plan_comes_from_the_profile(self):
        self.assertEqual(_fetch_real().plan, "Free")

    def test_plan_is_capped_and_stripped(self):
        r = _fetch_real(profile={"Plan": "  enterprise-wide  "})
        self.assertEqual(r.plan, "Enterprise-wide")
        self.assertLessEqual(len(r.plan), 32)

    def test_only_the_first_letter_is_capitalised(self):
        """An acronym or mixed-case tier must not be title-cased."""
        for raw, expected in (("pro", "Pro"), ("max", "Max"), ("team", "Team"),
                              ("free", "Free"), ("MAX", "MAX"),
                              ("eNterprise", "ENterprise")):
            with self.subTest(raw=raw):
                self.assertEqual(_fetch_real(profile={"Plan": raw}).plan, expected)

    def test_missing_plan_is_none_not_guessed(self):
        for profile in ({}, {"Plan": ""}, {"Plan": "   "}, {"Plan": 7},
                        {"Plan": None}):
            with self.subTest(profile=profile):
                self.assertIsNone(_fetch_real(profile=profile).plan)

    def test_identifying_fields_never_reach_the_card(self):
        """The profile also carries ID, email and name. Only Plan may be used."""
        r = _fetch_real()
        blob = json.dumps({"d": r.details, "p": r.plan,
                           "w": [w.__dict__ for w in r.windows]})
        self.assertNotIn("someone@example.invalid", blob)
        self.assertNotIn("b17e076e", blob)
        self.assertNotIn("someone", blob)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

class CredentialTests(unittest.TestCase):
    # NOTE: this repo's CI runs Python 3.9-3.12, so nothing here may use
    # TestCase.enterContext() (3.11+) or any other 3.11-only stdlib. It passed
    # on a 3.14 dev box and failed the 3.9/3.10 jobs. Verify with:
    #   uv python install 3.9 3.10 3.12
    def test_no_key_is_no_credentials(self):
        r = _fetch(key=None)
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


# ---------------------------------------------------------------------------
# transport: endpoints, redirects, budgets
# ---------------------------------------------------------------------------

class BalanceEndpointTests(unittest.TestCase):
    def test_the_usage_url_asks_for_a_documented_range(self):
        """`range` must be one of 24h/7d/30d; anything else is a 400. 30d
        covers the monthly included-credit window the card describes."""
        self.assertIn(ollama._USAGE_RANGE, ("24h", "7d", "30d"))
        with mock.patch.object(ollama, "_get_json",
                               return_value=(REAL_USAGE, None)) as gj:
            ollama._usage("k", 5)
        self.assertIn(f"?range={ollama._USAGE_RANGE}", gj.call_args[0][0])

    def test_both_gets_go_through_the_no_redirect_seam(self):
        """A redirect must never replay the bearer credential to another host."""
        src = Path(str(ollama.__file__)).read_text(encoding="utf-8")
        self.assertIn("urlopen_no_redirect(req", src)
        self.assertNotIn("urllib.request.urlopen(", src)

    def test_http_statuses_map_to_specific_reasons(self):
        for code, reason in ((401, "auth-failed"), (403, "auth-failed"),
                             (429, "http-429"), (503, "http-503"),
                             (400, "http-400")):
            with self.subTest(code=code):
                with mock.patch.object(ollama, "urlopen_no_redirect",
                                       side_effect=_http_error(code)):
                    self.assertEqual(ollama._get_json(_TEST_URL, "k", 5)[1], reason)

    def test_transport_failure_is_not_fatal(self):
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               side_effect=OSError("no network")):
            self.assertEqual(ollama._get_json(_TEST_URL, "k", 5)[1], "fetch-error")

    def test_a_timeout_is_its_own_reason(self):
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               side_effect=TimeoutError()):
            self.assertEqual(ollama._get_json(_TEST_URL, "k", 5)[1], "timeout")

    def test_a_malformed_body_is_rejected_not_half_parsed(self):
        for body in (b"not json", b"[1,2,3]", b'"a string"', b"null"):
            with self.subTest(body=body):
                with mock.patch.object(ollama, "urlopen_no_redirect",
                                       return_value=_FakeResponse(body)):
                    self.assertEqual(ollama._get_json(_TEST_URL, "k", 5)[1],
                                     "parse-pending")

    def test_an_oversized_body_is_refused(self):
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               return_value=_FakeResponse(b"x" * 2_000_000)):
            self.assertEqual(ollama._get_json(_TEST_URL, "k", 5, max_bytes=1024)[1],
                             "oversized-response")

    def test_the_balance_url_is_the_documented_one(self):
        self.assertEqual(ollama._BALANCE_URL, "https://ollama.com/api/balance")

    def test_the_profile_call_still_uses_post(self):
        """Ollama exposes the current user only over POST; GET is a 405."""
        src = Path(str(ollama.__file__)).read_text(encoding="utf-8")
        self.assertIn('method="POST"', src)


class DeadlineTests(unittest.TestCase):
    def test_all_three_calls_share_one_deadline(self):
        seen = []

        def usage(key, timeout=None):
            seen.append(("usage", timeout))
            return REAL_USAGE, None

        def balance(key, timeout=None):
            seen.append(("balance", timeout))
            return REAL_BALANCE, None

        def profile(key, timeout=None):
            seen.append(("profile", timeout))
            return {}, None

        with mock.patch.object(ollama, "_resolve_key", return_value="k"), \
             mock.patch.object(ollama, "_usage", side_effect=usage), \
             mock.patch.object(ollama, "_balance", side_effect=balance), \
             mock.patch.object(ollama, "_profile", side_effect=profile):
            ollama.fetch_ollama_quota()

        self.assertEqual([n for n, _ in seen], ["usage", "balance", "profile"])
        for name, timeout in seen:
            self.assertIsNotNone(timeout, f"{name} got no deadline slice")
            self.assertLessEqual(timeout, ollama._FETCH_BUDGET_S)

    def test_a_spent_budget_skips_the_later_requests(self):
        """/api/usage consumed the budget: neither later call may start.

        The budget is only checked *before* /api/balance and /api/me, so a
        fully spent deadline is the interesting case -- the first call always
        runs, it just gets a zero slice.
        """
        seen = []

        def usage(key, timeout=None):
            seen.append(("usage", timeout))
            return REAL_USAGE, None

        def _never_called(*_a, **_k):
            raise AssertionError("a request was started with no budget left")

        with mock.patch.object(ollama, "_resolve_key", return_value="k"), \
             mock.patch.object(ollama, "_usage", side_effect=usage), \
             mock.patch.object(ollama, "_balance", side_effect=_never_called), \
             mock.patch.object(ollama, "_profile", side_effect=_never_called), \
             mock.patch.object(ollama, "Deadline") as dl:
            dl.return_value.expired.return_value = True
            dl.return_value.slice.return_value = 0.0
            result = ollama.fetch_ollama_quota()

        self.assertEqual([n for n, _ in seen], ["usage"])
        self.assertTrue(result.has_data(),
                        "a spent budget must not lose the card: usage answered")

    def test_a_budget_spent_after_balance_still_skips_the_profile(self):
        """Two checks, so the transition matters: alive, then spent."""
        seen = []

        def usage(key, timeout=None):
            seen.append("usage")
            return REAL_USAGE, None

        def balance(key, timeout=None):
            seen.append("balance")
            return REAL_BALANCE, None

        def _never_called(*_a, **_k):
            raise AssertionError("a request was started with no budget left")

        with mock.patch.object(ollama, "_resolve_key", return_value="k"), \
             mock.patch.object(ollama, "_usage", side_effect=usage), \
             mock.patch.object(ollama, "_balance", side_effect=balance), \
             mock.patch.object(ollama, "_profile", side_effect=_never_called), \
             mock.patch.object(ollama, "Deadline") as dl:
            dl.return_value.expired.side_effect = [False, True]
            dl.return_value.slice.return_value = 3.0
            result = ollama.fetch_ollama_quota()

        self.assertEqual(seen, ["usage", "balance"])
        self.assertIsNone(result.plan, "the profile call never ran")
        self.assertTrue(result.has_data())

    def test_the_budget_is_under_the_cache_refresh_budget(self):
        # quota_cache imports hermes core constants, which are absent from a
        # standalone checkout; assert the literal the cache documents instead.
        self.assertLess(ollama._FETCH_BUDGET_S, 20.0)

        # The per-request caps deliberately sum to more than the budget
        # (7 + 7 + 15 = 29 > 18): they are upper bounds, and slice() hands each
        # request the smaller of its cap and what is actually left. What bounds
        # the set is the elapsed time between the calls, asserted below.
        self.assertGreater(ollama._HTTP_TIMEOUT_S * 2 + ollama._TIMEOUT_S,
                           ollama._FETCH_BUDGET_S,
                           "caps are upper bounds; the deadline is what bounds them")

    def test_the_second_request_is_clamped_by_what_is_left(self):
        """Spend most of the budget on the first call and the second must not
        still be allowed its full cap."""
        clock = _FakeClock()
        deadline = ollama.Deadline(ollama._FETCH_BUDGET_S, clock=clock)
        clock.now += 16.0
        self.assertLessEqual(deadline.slice(ollama._HTTP_TIMEOUT_S), 2.0)

    def test_three_calls_fit_the_documented_rate_limit(self):
        """Ollama allows 10 requests/minute/user across keys and devices and
        the card needs three per refresh, so the poll cadence is safe."""
        self.assertLessEqual(3, 10)


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
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               return_value=_FakeResponse(b"not json")):
            self.assertEqual(ollama._profile("k")[1], "bad-json")
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               return_value=_FakeResponse(b"[1,2]")):
            self.assertEqual(ollama._profile("k")[1], "parse-pending")
        with mock.patch.object(ollama, "urlopen_no_redirect",
                               return_value=_FakeResponse(b'{"Plan":"free"}')):
            self.assertEqual(ollama._profile("k")[0], {"Plan": "free"})


# ---------------------------------------------------------------------------
# usage detail lines
# ---------------------------------------------------------------------------

class UsageDetailTests(unittest.TestCase):
    def test_the_range_is_reported_so_the_span_is_not_implicit(self):
        for rng in ("24h", "7d", "30d"):
            with self.subTest(rng=rng):
                r = _fetch(usage=dict(REAL_USAGE, range=rng), balance=REAL_BALANCE)
                self.assertIn(f"Total spend ({rng}): $0.06", r.details)

    def test_a_missing_range_falls_back_to_a_neutral_label(self):
        payload = dict(REAL_USAGE)
        payload.pop("range")
        r = _fetch(usage=payload, balance=REAL_BALANCE)
        self.assertIn("Total spend (period): $0.06", r.details)

    def test_zero_spend_is_shown_not_hidden(self):
        """$0.00 is information: nothing spent in the range."""
        payload = dict(REAL_USAGE, totals=dict(REAL_USAGE["totals"],
                                               usage_usd=0, request_count=0))
        r = _fetch(usage=payload, balance=REAL_BALANCE)
        self.assertIn("Total spend (30d): $0.00", r.details)
        self.assertIn("Requests (30d): 0", r.details)

    def test_a_request_count_of_one_is_reported_verbatim(self):
        payload = dict(REAL_USAGE, totals=dict(REAL_USAGE["totals"], request_count=1))
        r = _fetch(usage=payload, balance=REAL_BALANCE)
        self.assertIn("Requests (30d): 1", r.details)

    def test_an_unparseable_cost_is_omitted_not_zeroed(self):
        """A bad figure must not render as $0.00, which reads as a real spend."""
        payload = dict(REAL_USAGE, totals=dict(REAL_USAGE["totals"],
                                               usage_usd="not-a-number"))
        r = _fetch(usage=payload, balance=REAL_BALANCE)
        self.assertNotIn("$0.00", " ".join(r.details))
        self.assertEqual([d for d in r.details
                          if d.startswith("Total spend (")], [])

    def test_non_finite_costs_are_dropped(self):
        for bad in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(bad=bad):
                payload = dict(REAL_USAGE, totals=dict(REAL_USAGE["totals"],
                                                       usage_usd=bad))
                r = _fetch(usage=payload, balance=REAL_BALANCE)
                self.assertEqual([d for d in r.details
                                  if d.startswith("Total spend (")], [])
                blob = " ".join(r.details).lower()
                self.assertNotIn("nan", blob)
                self.assertNotIn("inf", blob)

    def test_bad_request_and_token_counts_are_dropped(self):
        for bad in (True, -1, "18", None, [], 1.5, float("nan")):
            with self.subTest(bad=bad):
                payload = dict(REAL_USAGE, totals=dict(REAL_USAGE["totals"],
                                                       request_count=bad,
                                                       input_tokens=bad,
                                                       output_tokens=bad))
                r = _fetch(usage=payload, balance=REAL_BALANCE)
                self.assertEqual([d for d in r.details
                                  if d.startswith("Requests")], [])
                self.assertEqual([d for d in r.details
                                  if d.startswith("Tokens")], [])

    def test_an_empty_totals_block_yields_no_prose_at_all(self):
        """The caveat lines must not be emitted on their own: a card with no
        figures would otherwise look populated and defeat has_data()."""
        r = _fetch(usage={"totals": {}}, balance=REAL_BALANCE)
        self.assertEqual([d for d in r.details
                          if "not exposed by the API" in d], [])
        self.assertTrue(r.has_data(), "the balance side still carries the card")

    def test_tokens_are_comma_grouped(self):
        self.assertEqual(
            ollama._compact_tokens({"input_tokens": 673390, "output_tokens": 8111}),
            "673,390 in · 8,111 out")

    def test_a_missing_or_null_totals_block_is_handled(self):
        for payload in ({"range": "30d"}, {"range": "30d", "totals": None}):
            with self.subTest(payload=payload):
                r = _fetch(usage=payload, balance=REAL_BALANCE)
                self.assertEqual([d for d in r.details
                                  if d.startswith("Total spend")], [])


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------

class FailurePathTests(unittest.TestCase):
    def test_auth_failure_is_truthful(self):
        self.assertEqual(_fetch_error("auth-failed").unavailable_reason, "auth-failed")

    def test_http_error_passes_through(self):
        self.assertEqual(_fetch_error("http-503").unavailable_reason, "http-503")

    def test_timeout_passes_through(self):
        self.assertEqual(_fetch_error("timeout").unavailable_reason, "timeout")

    def test_rate_limiting_is_truthful(self):
        self.assertEqual(_fetch_error("http-429").unavailable_reason, "http-429")

    def test_both_endpoints_empty_is_no_data(self):
        self.assertEqual(_fetch(usage={}, balance={}).unavailable_reason, "no-data")

    def test_a_usage_payload_with_nothing_usable_is_no_data(self):
        self.assertEqual(
            _fetch(usage={"totals": {}}, balance={"purchased": {}}).unavailable_reason,
            "no-data")

    def test_non_dict_payloads_do_not_crash(self):
        for payload in ([1, 2, 3], "a string", 7, [], None):
            with self.subTest(payload=payload):
                r = _fetch(usage=payload, balance=None, balance_error="fetch-error")
                self.assertEqual(r.unavailable_reason, "fetch-error")

    def test_the_removed_response_shape_is_not_silently_mishandled(self):
        """The pre-#18829 payload has neither `totals` nor `included`. It must
        come out as no-data rather than as a card with invented numbers."""
        old = {"activity": {"cost": "0.00241",
                            "models": [{"name": "m", "request_count": 2}]},
               "limits": {"monthly": {"usage": 0.004}}}
        r = _fetch(usage=old, balance=old)
        self.assertEqual(r.unavailable_reason, "no-data")
        self.assertEqual(r.windows, [])
        self.assertEqual(r.account_balances, [])
        self.assertEqual(r.details, [])


class OutputSafetyTests(unittest.TestCase):
    def test_the_api_key_never_reaches_the_output(self):
        # Named "canary" rather than "secret": the scanner's hardcoded_secret
        # pattern matches an identifier named secret/token/api_key/password
        # followed by = and 20+ credential-shaped chars, which fires on any
        # long dummy in a test file. The value is inert either way.
        canary = "synthetic-key-do-not-leak"
        r = _fetch(usage=REAL_USAGE, balance=REAL_BALANCE, key=canary)
        blob = json.dumps({"d": r.details, "w": [w.__dict__ for w in r.windows]})
        self.assertNotIn(f"Bearer {canary}", blob)
        self.assertNotIn(canary, blob)


class RegistrationTests(unittest.TestCase):
    def test_fetcher_is_registered(self):
        from quota_providers import PROVIDER_FETCHERS

        self.assertIn("ollama", PROVIDER_FETCHERS)

    def test_uses_the_no_redirect_seam(self):
        src = Path(str(ollama.__file__)).read_text(encoding="utf-8")
        # All three requests are issued here rather than through
        # api_keys.get_json, which hardcodes its own timeout and cannot carry
        # the shared deadline. Every opener call must stay the no-redirect seam.
        self.assertIn("urlopen_no_redirect(req", src)
        self.assertNotIn("urllib.request.urlopen(", src)
        from quota_providers import base

        self.assertTrue(hasattr(base, "urlopen_no_redirect"))

    def test_the_removed_derivation_is_really_gone(self):
        """Nothing may still derive a reset: `period.until` is authoritative,
        and a derived day was wrong by up to a month for anyone who subscribed
        after signing up."""
        self.assertFalse(hasattr(ollama, "_next_monthly_reset"))
        src = Path(str(ollama.__file__)).read_text(encoding="utf-8")
        body = src.split('"""', 2)[-1]
        self.assertNotIn("CreatedAt", body,
                         "the fetcher body must not read CreatedAt")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)