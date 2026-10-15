"""Command Code multi-card contract: one card per saved credential row.

The local store keeps one row per saved key; the fetcher must enumerate those
rows read-only and report every one as its own card, so one row's failure is
that card's typed state and never masks a sibling. Offline only: the pool and
every HTTP result are synthetic; no real credential is read.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from quota_providers import commandcode
from quota_providers.base import QuotaResult, QuotaWindow, build_unavailable

# Unicode display-hardening fixtures, built with ``chr()`` so this file stays
# plain ASCII: bidi override, C1 control, line/paragraph separators, zero-width
# formatting character, lone surrogate.
BIDI_OVERRIDE = chr(0x202E)
NEL = chr(0x0085)
LINE_SEP = chr(0x2028)
PARA_SEP = chr(0x2029)
ZERO_WIDTH = chr(0x200B)
LONE_SURROGATE = chr(0xD800)

ROWS = [
    {"id": "r1", "label": "alpha.gmail", "access_token": "SYNTH-ONE"},
    {"id": "r2", "label": "beta.gmail", "access_token": "SYNTH-TWO"},
    {"id": "r3", "label": "gamma.gmail", "access_token": "SYNTH-THREE"},
    {"id": "r4", "label": "delta.mail", "access_token": "SYNTH-FOUR"},
]


def card(used: float, *, plan: str = "Go") -> QuotaResult:
    return QuotaResult(
        label="commandcode",
        windows=[QuotaWindow(label="5h", used_percent=used, reset_at="2099-01-01T00:00:00Z")],
        plan=plan,
    )


class CredentialRowTests(unittest.TestCase):
    def rows(self, entries):
        with mock.patch.object(commandcode, "_pool_rows", return_value=list(entries)):
            return commandcode._credential_rows()

    def test_every_saved_row_yields_id_label_and_secret(self):
        rows = self.rows(ROWS)
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [("r1", "alpha.gmail"), ("r2", "beta.gmail"),
             ("r3", "gamma.gmail"), ("r4", "delta.mail")],
        )
        self.assertEqual(
            [row[2] for row in rows],
            ["SYNTH-ONE", "SYNTH-TWO", "SYNTH-THREE", "SYNTH-FOUR"],
        )

    def test_duplicates_and_secretless_rows_are_skipped(self):
        rows = self.rows([
            ROWS[0],
            {**ROWS[1], "access_token": "SYNTH-ONE"},  # same secret -> one card
            {"label": "no secret"},
            {"id": "blank", "label": "Blank", "access_token": "   "},
        ])
        self.assertEqual([row[2] for row in rows], ["SYNTH-ONE"])

    def test_labels_drop_control_characters_and_stay_bounded(self):
        rows = self.rows([
            {"id": "a", "label": " bad\x07label ", "access_token": "S"},
            {"id": "\x00\x1f", "label": "\U0001f600" * 80, "access_token": "T"},
        ])
        self.assertEqual(rows[0][1], "badlabel")
        self.assertIsNone(rows[1][0])  # id stripped to nothing
        self.assertEqual(rows[1][1], "\U0001f600" * 64)  # bounded display label

    def test_labels_and_ids_drop_unicode_controls_and_separators(self):
        rows = self.rows([
            {"id": "a" + BIDI_OVERRIDE + "b", "label": "x" + BIDI_OVERRIDE + "y",
             "access_token": "S"},
            {"id": "c" + NEL + "d", "label": "l" + NEL + "m", "access_token": "T"},
            {"id": "e" + LINE_SEP + "f", "label": "n" + PARA_SEP + "o",
             "access_token": "U"},
            {"id": LONE_SURROGATE + "bad", "label": "lone" + LONE_SURROGATE,
             "access_token": "V"},
            {"id": "g" + ZERO_WIDTH + "h", "label": "zero" + ZERO_WIDTH + "width",
             "access_token": "W"},
        ])
        self.assertEqual([row[0] for row in rows], ["ab", "cd", "ef", "bad", "gh"])
        self.assertEqual([row[1] for row in rows], ["xy", "lm", "no", "lone", "zerowidth"])


class MultiCardFetchTests(unittest.TestCase):
    CARDS = {
        "SYNTH-ONE": card(50.0), "SYNTH-TWO": card(40.0),
        "SYNTH-THREE": card(30.0), "SYNTH-FOUR": card(20.0),
    }

    def fetch(self, rows, cards=None, calls=None):
        cards = dict(cards or self.CARDS)

        def fake_card(key, deadline):
            if calls is not None:
                calls.append(key)
            return cards[key]

        with mock.patch.object(commandcode, "_pool_rows", return_value=list(rows)), \
                mock.patch.object(commandcode, "_load_api_key", return_value=None), \
                mock.patch.object(commandcode, "_fetch_card", side_effect=fake_card):
            return commandcode.fetch_commandcode_quota()

    def test_each_row_becomes_its_own_card(self):
        result = self.fetch(ROWS)
        self.assertEqual(result.label, "commandcode")
        self.assertEqual(result.account_label, "alpha.gmail")
        self.assertEqual([a.id for a in result.accounts], ["r2", "r3", "r4"])
        self.assertEqual(
            [a.label for a in result.accounts], ["beta.gmail", "gamma.gmail", "delta.mail"]
        )
        self.assertEqual(
            [a.windows[0].used_percent for a in result.accounts], [40.0, 30.0, 20.0]
        )
        self.assertTrue(all(a.unavailable_reason is None for a in result.accounts))

    def test_every_row_is_fetched_with_its_own_secret(self):
        calls = []
        self.fetch(ROWS, calls=calls)
        self.assertEqual(
            sorted(calls), ["SYNTH-FOUR", "SYNTH-ONE", "SYNTH-THREE", "SYNTH-TWO"]
        )

    def test_one_rows_failure_is_that_cards_typed_state(self):
        cards = dict(self.CARDS)
        cards["SYNTH-THREE"] = build_unavailable("commandcode", "auth-failed")
        result = self.fetch(ROWS, cards)
        self.assertIsNone(result.unavailable_reason)
        by_id = {a.id: a for a in result.accounts}
        self.assertEqual(by_id["r3"].unavailable_reason, "auth-failed")
        self.assertEqual(by_id["r3"].windows, [])
        self.assertEqual(by_id["r2"].windows[0].used_percent, 40.0)

    def test_raising_row_does_not_sink_its_siblings(self):
        def fake_card(key, deadline):
            if key == "SYNTH-TWO":
                raise RuntimeError("boom")
            return card(50.0)

        with mock.patch.object(commandcode, "_pool_rows", return_value=list(ROWS)), \
                mock.patch.object(commandcode, "_load_api_key", return_value=None), \
                mock.patch.object(commandcode, "_fetch_card", side_effect=fake_card):
            result = commandcode.fetch_commandcode_quota()
        self.assertIsNone(result.unavailable_reason)
        by_id = {a.id: a for a in result.accounts}
        self.assertEqual(by_id["r2"].unavailable_reason, "fetch-error:RuntimeError")
        self.assertEqual(by_id["r3"].windows[0].used_percent, 50.0)

    def test_base_row_failure_still_keeps_the_other_cards(self):
        cards = dict(self.CARDS)
        cards["SYNTH-ONE"] = build_unavailable("commandcode", "timeout")
        result = self.fetch(ROWS, cards)
        self.assertEqual(result.unavailable_reason, "timeout")
        self.assertEqual(result.account_label, "alpha.gmail")
        self.assertEqual(len(result.accounts), 3)
        self.assertEqual(result.accounts[0].windows[0].used_percent, 40.0)

    def test_pool_read_and_rows_are_not_mutated(self):
        rows = [dict(row) for row in ROWS]
        snapshot = [dict(row) for row in rows]
        self.fetch(rows)
        self.assertEqual(rows, snapshot)


class SingleCardUnchangedTests(unittest.TestCase):
    def test_empty_pool_falls_back_to_the_cli_or_core_key(self):
        sentinel = card(50.0)
        with mock.patch.object(commandcode, "_pool_rows", return_value=[]), \
                mock.patch.object(commandcode, "_load_api_key", return_value="SYNTH-ONE"), \
                mock.patch.object(commandcode, "_fetch_card", return_value=sentinel) as single:
            result = commandcode.fetch_commandcode_quota()
        single.assert_called_once_with("SYNTH-ONE", mock.ANY)
        self.assertIs(result, sentinel)

    def test_empty_pool_and_no_key_is_still_no_credentials(self):
        with mock.patch.object(commandcode, "_pool_rows", return_value=[]), \
                mock.patch.object(commandcode, "_load_api_key", return_value=None):
            result = commandcode.fetch_commandcode_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")
        self.assertEqual(result.accounts, [])

    def test_a_single_row_keeps_the_plain_single_card(self):
        sentinel = card(50.0)
        with mock.patch.object(commandcode, "_pool_rows", return_value=[ROWS[0]]), \
                mock.patch.object(commandcode, "_load_api_key", return_value="SYNTH-ONE"), \
                mock.patch.object(commandcode, "_fetch_card", return_value=sentinel) as single:
            result = commandcode.fetch_commandcode_quota()
        self.assertIs(result, sentinel)
        self.assertEqual(result.accounts, [])
        self.assertIsNone(result.account_label)
        single.assert_called_once()


class EnvBackedRowTests(unittest.TestCase):
    """R1: an ``env:`` row resolves through the current environment first.

    A saved row can outlive the environment variable it references; a rotated
    value must win over the stale persisted token, while a missing value keeps
    the persisted token as the fallback.
    """

    def rows(self, entries, environment):
        with mock.patch.object(commandcode, "_pool_rows", return_value=list(entries)), \
                mock.patch.object(commandcode, "_env_secret", side_effect=environment.get):
            return commandcode._credential_rows()

    def test_rotated_env_value_replaces_the_stale_persisted_token(self):
        rows = self.rows([
            {"id": "r1", "label": "alpha.gmail", "source": "env:SYNTH_ENV_KEY",
             "access_token": "SYNTH-STALE"},
        ], {"SYNTH_ENV_KEY": "SYNTH-ROTATED"})
        self.assertEqual([row[2] for row in rows], ["SYNTH-ROTATED"])

    def test_missing_env_value_keeps_the_persisted_token(self):
        rows = self.rows([
            {"id": "r1", "label": "alpha.gmail", "source": "env:SYNTH_ENV_KEY",
             "access_token": "SYNTH-PERSISTED"},
        ], {})
        self.assertEqual([row[2] for row in rows], ["SYNTH-PERSISTED"])


class PoolShapeTests(unittest.TestCase):
    """R2: each store's ``credential_pool`` shape is validated on its own.

    A malformed profile pool must neither raise nor hide a valid global pool;
    when neither store is usable the fetcher still runs the single-key path.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _home(self, name, payload):
        home = pathlib.Path(self.temp.name) / name
        home.mkdir()
        (home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
        return str(home)

    def _with_stores(self, profile_home, default_home):
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: profile_home
        constants.get_default_hermes_root = lambda: default_home
        return mock.patch.dict(sys.modules, {"hermes_constants": constants})

    def test_malformed_profile_pool_falls_back_to_the_global_store(self):
        profile = self._home("profile", {"credential_pool": ["not-a-dict"]})
        default = self._home("default", {"credential_pool": {"commandcode": [
            {"id": "g1", "label": "gamma.gmail", "access_token": "SYNTH-GLOBAL"},
        ]}})
        with self._with_stores(profile, default):
            rows = commandcode._pool_rows_from_store()
        self.assertEqual([row["id"] for row in rows], ["g1"])

    def test_malformed_global_pool_falls_back_to_the_profile_store(self):
        profile = self._home("profile", {"credential_pool": {"commandcode": [
            {"id": "p1", "label": "alpha.gmail", "access_token": "SYNTH-PROFILE"},
        ]}})
        default = self._home("default", {"credential_pool": "bogus"})
        with self._with_stores(profile, default):
            rows = commandcode._pool_rows_from_store()
        self.assertEqual([row["id"] for row in rows], ["p1"])

    def test_both_pools_unusable_still_fetches_with_the_single_key(self):
        profile = self._home("profile", {"credential_pool": ["not-a-dict"]})
        default = self._home("default", {"credential_pool": "bogus"})
        sentinel = card(50.0)
        with self._with_stores(profile, default), \
                mock.patch.object(commandcode, "_load_api_key", return_value="SYNTH-ONE"), \
                mock.patch.object(commandcode, "_fetch_card", return_value=sentinel) as single:
            result = commandcode.fetch_commandcode_quota()
        single.assert_called_once_with("SYNTH-ONE", mock.ANY)
        self.assertIs(result, sentinel)


class FanOutCapTests(unittest.TestCase):
    """R3: a huge saved pool cannot fan out unbounded fetches.

    With 64 saved rows, only ``_MAX_POOL_ROWS`` are accepted and at most
    ``_MAX_CONCURRENT_FETCHES`` fetches run at once; the remainder becomes one
    typed ``config-limit`` row, never a silent drop.
    """

    def test_row_and_worker_caps_bound_a_huge_pool(self):
        rows = [
            {"id": f"r{i:02d}", "label": f"user{i:02d}", "access_token": f"SYNTH-{i:02d}"}
            for i in range(64)
        ]
        lock = threading.Lock()
        release = threading.Event()
        started = threading.Event()
        calls = []
        active = 0
        peak = 0

        def stalled_card(key, _deadline):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append(key)
                if len(calls) >= commandcode._MAX_CONCURRENT_FETCHES:
                    started.set()
            release.wait(5.0)
            with lock:
                active -= 1
            return card(50.0)

        outcome = []
        with mock.patch.object(commandcode, "_pool_rows", return_value=rows), \
                mock.patch.object(commandcode, "_load_api_key", return_value=None), \
                mock.patch.object(commandcode, "_fetch_card", side_effect=stalled_card):
            runner = threading.Thread(
                target=lambda: outcome.append(commandcode.fetch_commandcode_quota()),
                daemon=True,
            )
            runner.start()
            self.assertTrue(started.wait(2.0), "concurrent fetches never reached the cap")
            with lock:
                in_flight = len(calls)
            self.assertEqual(in_flight, commandcode._MAX_CONCURRENT_FETCHES)
            release.set()
            runner.join(10.0)
        self.assertFalse(runner.is_alive())
        self.assertEqual(len(outcome), 1)
        result = outcome[0]

        expected = [f"SYNTH-{i:02d}" for i in range(commandcode._MAX_POOL_ROWS)]
        self.assertEqual(sorted(calls), sorted(expected))
        self.assertLessEqual(peak, commandcode._MAX_CONCURRENT_FETCHES)
        self.assertEqual(result.account_label, "user00")
        self.assertEqual(
            [account.id for account in result.accounts[: commandcode._MAX_POOL_ROWS - 1]],
            [f"r{i:02d}" for i in range(1, commandcode._MAX_POOL_ROWS)],
        )
        overflow = result.accounts[commandcode._MAX_POOL_ROWS - 1:]
        self.assertEqual([account.unavailable_reason for account in overflow], ["config-limit"])
        self.assertEqual(overflow[0].details, [commandcode._POOL_LIMIT_DETAIL])
        self.assertIn(str(commandcode._MAX_POOL_ROWS), commandcode._POOL_LIMIT_DETAIL)


class PrimaryInclusionTests(unittest.TestCase):
    """R4: a distinct CLI/core primary key is included as the plain base card.

    When the primary duplicates a saved row's credential it is deduped; with no
    primary the first saved row keeps the plain base card (unchanged).
    """

    def fetch(self, rows, primary):
        cards = {
            "SYNTH-ONE": card(50.0), "SYNTH-TWO": card(40.0), "SYNTH-THREE": card(30.0),
            "SYNTH-PRIMARY": card(90.0),
        }
        calls = []

        def fake_card(key, _deadline):
            calls.append(key)
            return cards[key]

        with mock.patch.object(commandcode, "_pool_rows", return_value=list(rows)), \
                mock.patch.object(commandcode, "_load_api_key", return_value=primary), \
                mock.patch.object(commandcode, "_fetch_card", side_effect=fake_card):
            return commandcode.fetch_commandcode_quota(), calls

    def test_a_distinct_primary_becomes_the_plain_base_card(self):
        result, calls = self.fetch(ROWS[:3], "SYNTH-PRIMARY")
        self.assertIsNone(result.account_label)
        self.assertEqual(result.windows[0].used_percent, 90.0)
        self.assertEqual([account.id for account in result.accounts], ["r1", "r2", "r3"])
        self.assertIn("SYNTH-PRIMARY", calls)

    def test_a_duplicate_primary_is_deduped_not_a_second_card(self):
        result, calls = self.fetch(ROWS[:3], "SYNTH-ONE")
        self.assertEqual(result.account_label, "alpha.gmail")
        self.assertEqual([account.id for account in result.accounts], ["r2", "r3"])
        self.assertEqual(sorted(calls), ["SYNTH-ONE", "SYNTH-THREE", "SYNTH-TWO"])

    def test_no_primary_keeps_the_first_saved_row_as_base(self):
        result, calls = self.fetch(ROWS[:3], None)
        self.assertEqual(result.account_label, "alpha.gmail")
        self.assertEqual([account.id for account in result.accounts], ["r2", "r3"])
        self.assertEqual(sorted(calls), ["SYNTH-ONE", "SYNTH-THREE", "SYNTH-TWO"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
