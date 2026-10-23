"""Live view, attention rules, token rate and the kanban glance (synthetic data, no network).

Run from the repo root:  python -m unittest tests.test_monitor_live
"""

from __future__ import annotations

import json
import os
import time
import unittest

from monitor_fixtures import Home, load

live = load("quota_monitor.live")
usage = load("quota_monitor.usage")
common = load("quota_monitor.common")

ME = os.getpid()  # a live pid for lease entries


def _call(name: str, args: dict, cid: str) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class LiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Home(profiles=("work",))
        self.now = time.time()

    def tearDown(self) -> None:
        self.home.close()

    def _live(self, **kw):
        return live.live(common.profile_homes(), self.now, **kw)

    def _row(self, data, sid):
        return next(r for r in data["running"] if r["id"] == sid)

    def test_states_counts_and_attention(self):
        h, now = self.home, self.now
        # finished desktop chat with an open window -> your turn
        h.session("s-turn", "desktop", started=now - 3000, last=now - 400)
        h.message("s-turn", "user", now - 500)
        h.message("s-turn", "assistant", now - 400, "done")
        h.lease("s-turn", ME)
        # closed desktop window (no lease) -> not listed at all
        h.session("s-closed", "desktop", started=now - 3000, last=now - 30)
        h.message("s-closed", "assistant", now - 30, "done")
        # pending approval (Hermes says so only in its activity label)
        h.session("s-appr", "cli", started=now - 600, last=now - 20, act="waiting for user approval (12s elapsed)")
        h.message("s-appr", "assistant", now - 20, "", tool_calls=[_call("terminal", {"cmd": "rm"}, "c1")])
        h.lease("s-appr", ME)
        # unattended kanban worker that finished its turn: never "your turn"
        h.session("s-kanban", "kanban", "work", started=now - 900, last=now - 100)
        h.message("s-kanban", "user", now - 200, "work kanban task t_abc123", profile="work")
        h.message("s-kanban", "assistant", now - 100, "done", profile="work")
        # stalled: an activity label claims a turn but nothing was written for 20 min (no tool in flight)
        h.session("s-stall", "telegram", started=now - 3600, last=now - 300, act="thinking")
        h.message("s-stall", "user", now - 1200)
        # loop: the same tool call 4 times in a row + a very large context
        h.session("s-loop", "tui", started=now - 900, last=now - 5, act="running tool")
        h.message("s-loop", "user", now - 800, "x" * 900_000)
        for i in range(4):
            h.message("s-loop", "assistant", now - 300 + i * 10, "", tool_calls=[_call("read_file", {"path": "a"}, "l%d" % i)])
            h.message("s-loop", "tool", now - 295 + i * 10, "same")
        h.lease("s-loop", ME)

        data = self._live(record_sample=False)
        ids = {r["id"] for r in data["running"]}
        self.assertNotIn("s-closed", ids)
        self.assertEqual(self._row(data, "s-turn")["state"], "waiting")
        self.assertEqual(self._row(data, "s-appr")["state"], "needs_approval")
        self.assertEqual(self._row(data, "s-kanban")["state"], "waiting")
        self.assertFalse(self._row(data, "s-kanban")["interactive"])
        self.assertEqual(self._row(data, "s-stall")["state"], "stalled")
        loop_row = self._row(data, "s-loop")
        self.assertEqual(loop_row["loop_max"], 4)
        self.assertGreater(loop_row["context_tokens"], 200_000)
        self.assertEqual(data["counts"], {"open": 5, "working": 1, "needs_approval": 1, "your_turn": 2,
                                          "stalled": 1, "subagents": 0})
        kinds = {(a["kind"], a["session_id"]) for a in data["attention"]}
        self.assertEqual(kinds, {("approval", "s-appr"), ("your_turn", "s-turn"), ("stalled", "s-stall"),
                                 ("loop", "s-loop"), ("big_context", "s-loop")})
        self.assertNotIn(("your_turn", "s-kanban"), kinds, "unattended workers never produce your_turn")
        self.assertEqual(data["attention"][0]["kind"], "approval", "most urgent first")
        for a in data["attention"]:
            self.assertEqual(set(a), {"id", "kind", "session_id", "profile", "title", "since", "detail", "source",
                                      "value"})
        self.assertTrue(all(not k.startswith("_") for r in data["running"] for k in r), "private keys stripped")
        self.assertEqual(self._row(data, "s-turn")["open"]["command"], "hermes --resume s-turn")
        self.assertEqual(self._row(data, "s-kanban")["open"]["kind"], "hermes-session")
        self.assertEqual(self._row(data, "s-kanban")["profile"], "work")

    def test_running_card_without_session_is_a_row_and_pairs_by_prompt(self):
        h, now = self.home, self.now
        h.card("t_abc123", "running", title="build it", started=now - 600, created=now - 700)
        h.card("t_def456", "running", title="orphan", started=now - 600, created=now - 700, worker_pid=0)
        h.session("s-kanban", "kanban", "work", started=now - 500, last=now - 10, act="working")
        h.message("s-kanban", "user", now - 500, "work kanban task t_abc123", profile="work")
        data = self._live(record_sample=False)
        paired = self._row(data, "s-kanban")
        self.assertEqual(paired["kanban"]["id"], "t_abc123")
        self.assertEqual(paired["open"]["kind"], "kanban-card")
        self.assertIn("session_command", paired["open"])
        orphan = self._row(data, "t_def456")
        self.assertTrue(orphan["card_only"])
        self.assertEqual(orphan["state"], "stalled")

    def test_token_rate_fills_from_persisted_samples(self):
        h, now = self.home, self.now
        h.session("s1", "cron", started=now - 600, last=now - 5)
        h.usage_row("s1", tokens=1000, cost=1.0, last_seen=now - 100)
        first = live.live(common.profile_homes(), now - 30)
        self.assertEqual(first["last_60m"], 0)
        self.assertIsNone(first["sample_every"])
        h.usage_row("s1", tokens=4000, cost=2.5, last_seen=now - 5)
        second = live.live(common.profile_homes(), now)
        self.assertEqual(second["last_60m"], 3000)
        self.assertAlmostEqual(second["cost_60m"], 1.5)
        self.assertEqual(second["providers"], ["hermes"])
        self.assertEqual(second["by_profile"], [{"profile": "default", "tokens": 3000, "cost": 1.5}])
        self.assertEqual(second["sample_every"], 30)
        self.assertEqual(second["sampling_since"], now - 30)

    def test_rate_skips_gaps_longer_than_the_widget_poll(self):
        key = live.KEY_SEP.join(("default", "anthropic", "s", "m"))
        a = {key: [0, 0.0]}
        b = {key: [5000, 1.0]}
        rate = live.token_rate([(self.now - 4000, a), (self.now - 10, b)], self.now)
        self.assertEqual(dict(rate["minutes"]), {})

    def test_missing_databases_yield_an_empty_view(self):
        data = self._live()
        self.assertEqual(data["running"], [])
        self.assertEqual(data["counts"]["open"], 0)
        self.assertEqual(len(data["activity"]), 60)
        self.assertEqual(len(data["tokens"]), 60)

    def test_degrades_without_ps_or_cli_registries(self):
        """Linux / containers: no `ps`, no ~/.claude, no Hermes.app — rows still come back, process facts empty."""
        h, now = self.home, self.now
        h.session("s-turn", "desktop", started=now - 300, last=now - 60)
        h.message("s-turn", "assistant", now - 60, "done")
        h.lease("s-turn", ME)
        from unittest import mock
        with mock.patch.object(live.subprocess, "run", side_effect=OSError("no ps")), \
                mock.patch.object(live.sys, "platform", "linux"):
            data = self._live(record_sample=False)
        row = self._row(data, "s-turn")
        self.assertIsNone(row["open"]["url"])
        self.assertFalse(row["open"]["running"])
        self.assertIsNone(row["open"]["app"])
        self.assertEqual(self.home.clis.claude_open_sessions(), None)


class ContextWindowTest(unittest.TestCase):
    """context_tokens from the usage anchor, context_max / context_percent from (patched) Hermes metadata."""

    def setUp(self) -> None:
        self.home = Home(profiles=("work",))
        self.now = time.time()
        self.ctx = live.context
        self.ctx.clear_cache()
        from unittest import mock
        self.windows = {"model-a": 100_000, "model-b": 1_000_000}
        self.calls = []

        def window(model, provider, base_url):
            self.calls.append((model, provider, base_url))
            return self.windows.get(model)

        self.patch = mock.patch.object(self.ctx, "_hermes_window", side_effect=window)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.ctx.clear_cache()
        self.home.close()

    def _open(self, sid, model="model-a", anchor=None, profile="default", messages=3, tail=400):
        h, now = self.home, self.now
        h.session(sid, "desktop", profile, started=now - 900, last=now - 30, model=model)
        for i in range(messages - 1):
            h.message(sid, "user" if i % 2 == 0 else "assistant", now - 300 + i, "y" * 40, profile=profile)
        h.message(sid, "assistant", now - 30, "z" * tail, profile=profile)
        h.lease(sid, ME, profile)
        if anchor is not None:
            with h.state(profile) as c:
                c.execute("UPDATE sessions SET model_config = ? WHERE id = ?",
                          (json.dumps({"service_tier": "normal", "_usage_anchor": anchor}), sid))

    def _row(self, sid):
        data = live.live(common.profile_homes(), self.now, record_sample=False)
        return next(r for r in data["running"] if r["id"] == sid)

    def test_anchor_plus_tail_estimate_and_percent(self):
        self._open("s-anch", anchor={"prompt_tokens": 90_000, "completion_tokens": 1000, "base_count": 2})
        row = self._row("s-anch")
        self.assertEqual(row["context_tokens"], 90_000 + 1000 + 100, "anchor + chars/4 of the message after it")
        self.assertFalse(row["context_estimated"])
        self.assertEqual(row["context_max"], 100_000)
        self.assertEqual(row["context_percent"], 91.1)

    def test_no_anchor_falls_back_to_estimate(self):
        self._open("s-est", model="model-b", tail=4000)
        row = self._row("s-est")
        self.assertEqual(row["context_tokens"], (40 * 2 + 4000) // 4)
        self.assertTrue(row["context_estimated"])
        self.assertEqual(row["context_max"], 1_000_000)
        self.assertEqual(row["context_percent"], 0.1)

    def test_stale_anchor_is_ignored(self):
        # base_count beyond the active transcript (e.g. after a compression rewrote it): not usable.
        self._open("s-stale", anchor={"prompt_tokens": 500_000, "completion_tokens": 0, "base_count": 99})
        row = self._row("s-stale")
        self.assertTrue(row["context_estimated"])
        self.assertLess(row["context_tokens"], 1000)

    def test_unknown_model_is_null_not_a_guess(self):
        self._open("s-unk", model="mystery-model")
        row = self._row("s-unk")
        self.assertIsNotNone(row["context_tokens"])
        self.assertIsNone(row["context_max"])
        self.assertIsNone(row["context_percent"])

    def test_resolver_failure_is_fail_open(self):
        self.patch.stop()
        from unittest import mock
        self.ctx.clear_cache()
        with mock.patch.object(self.ctx, "_hermes_window", side_effect=RuntimeError("boom")):
            self._open("s-boom")
            row = self._row("s-boom")
        self.patch.start()
        self.assertIsNone(row["context_max"])
        self.assertIsNone(row["context_percent"])
        self.assertIsNotNone(row["context_tokens"])

    def test_profile_pin_wins_for_its_model_only(self):
        if not self._yaml():
            self.skipTest("no YAML parser in this interpreter")
        self.home.write("config.yaml", "model:\n  default: model-a\n  context_length: 50000\n", profile="work")
        self._open("s-pin", profile="work", anchor={"prompt_tokens": 40_000, "completion_tokens": 0,
                                                    "base_count": 3})
        self._open("s-other", model="model-b", profile="work")
        self.assertEqual(self._row("s-pin")["context_max"], 50_000)
        self.assertEqual(self._row("s-pin")["context_percent"], 80.0)
        self.assertEqual(self._row("s-other")["context_max"], 1_000_000, "pin is for the configured model")

    def test_window_lookups_are_cached(self):
        self._open("s-a1")
        self._open("s-a2")
        live.live(common.profile_homes(), self.now, record_sample=False)
        live.live(common.profile_homes(), self.now, record_sample=False)
        self.assertEqual(self.calls.count(("model-a", "anthropic", "")), 1)

    def test_rows_without_context_carry_null_keys(self):
        h, now = self.home, self.now
        h.session("s-parent", "desktop", started=now - 900, last=now - 30)
        h.message("s-parent", "assistant", now - 30, "done")
        h.lease("s-parent", ME)
        h.session("s-sub", "desktop", started=now - 600, last=now - 20, parent="s-parent")
        data = live.live(common.profile_homes(), now, record_sample=False)
        sub = next(r for r in data["running"] if r["id"] == "s-sub")
        self.assertIsNone(sub["context_max"])
        self.assertIsNone(sub["context_percent"])

    @staticmethod
    def _yaml() -> bool:
        for mod in ("ruamel.yaml", "yaml"):
            try:
                __import__(mod)
                return True
            except ImportError:
                continue
        return False


class KanbanGlanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Home()

    def tearDown(self) -> None:
        self.home.close()

    def test_only_cards_a_glance_needs_capped(self):
        now = time.time()
        h = self.home
        h.card("t_run", "running", started=now - 100)
        h.card("t_blk", "blocked", block_kind="needs_input")
        h.card("t_rev", "review")
        h.card("t_todo", "todo")
        h.card("t_fail", "todo", failures=2)
        h.card("t_done", "done", completed=now - 100)
        h.card("t_donefail", "done", completed=now - 100, failures=1)
        h.card("t_arch", "archived")
        for i in range(5):
            h.card("t_side%d" % i, "running", board="side")
        out = usage.kanban_glance(h.root, now, cap=3)
        boards = {b["board"]: b for b in out["boards"]}
        main = boards["default"]
        self.assertEqual(main["counts"]["todo"], 2)
        ids = [c["id"] for c in main["cards"]]
        self.assertEqual(ids[:3], ["t_run", "t_blk", "t_rev"])
        self.assertEqual(main["truncated"], 2, "t_fail + t_donefail left out by the cap")
        self.assertEqual(main["cards"][1]["waiting_for"], "input")
        self.assertEqual(main["blocked_by_kind"], {"needs_input": 1})
        self.assertEqual(boards["side"]["truncated"], 2)
        self.assertIn("--board side", boards["side"]["cards"][0]["open"]["command"])
        full = usage.kanban_glance(h.root, now)
        self.assertEqual({c["id"] for c in full["boards"][0]["cards"]},
                         {"t_run", "t_blk", "t_rev", "t_fail", "t_donefail"})


if __name__ == "__main__":
    unittest.main()
