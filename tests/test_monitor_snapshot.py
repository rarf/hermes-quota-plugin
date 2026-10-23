"""Snapshot contract, CLI subcommands, health, recommend and the agent tools (synthetic data, no network).

Run from the repo root:  python -m unittest tests.test_monitor_snapshot
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import time
import unittest
from datetime import datetime
from unittest import mock

from monitor_fixtures import Home, anthropic_record, iso, load, walk_keys

qm = load("quota_monitor")
health = load("quota_monitor.health")
usage = load("quota_monitor.usage")
store = load("quota_monitor.store")
tools = load("quota_monitor.tools")
commands = load("commands")

CONFIG = {
    "default": {"model": {"provider": "anthropic", "default": "model-a"},
                "fallback_providers": [{"provider": "anthropic", "model": "model-b"},
                                       {"provider": "openrouter", "model": "model-c"},
                                       {"provider": "zai", "model": "model-d"}]},
    "work": {"model": {"provider": "openrouter", "model": None, "default": "model-c"}},
}


def _no_network(*_a, **_k):
    raise AssertionError("the monitor must not open network connections")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Home(profiles=("work",))
        self.now = time.time()
        patcher = mock.patch.object(health, "_config", lambda home: CONFIG.get(
            "default" if home == self.home.root else home.name, {}))
        patcher.start()
        self.addCleanup(patcher.stop)
        net = mock.patch.object(socket.socket, "connect", _no_network)
        net.start()
        self.addCleanup(net.stop)

    def tearDown(self) -> None:
        self.home.close()

    def seed(self) -> None:
        h, now = self.home, self.now
        h.session("s1", "desktop", started=now - 3000, last=now - 100, title="private title one", cost=3.0)
        h.message("s1", "user", now - 200)
        h.message("s1", "assistant", now - 100, "done")
        h.usage_row("s1", model="model-a", tokens=5000, cost=3.0, calls=10, last_seen=now - 100)
        h.lease("s1", os.getpid())
        h.session("s2", "cli", "work", started=now - 5000, last=now - 4000, model="model-c", provider="openrouter",
                  cost=1.0, title="private title two")
        h.usage_row("s2", model="model-c", provider="openrouter", tokens=2000, cost=1.0, last_seen=now - 4000,
                    profile="work")
        h.write("auth.json", json.dumps({"credential_pool": {
            "anthropic": [{"label": "work", "source": "manual", "auth_type": "oauth", "priority": 0,
                           "last_status": "ok", "access_token": "synthetic-not-a-token", "request_count": 3}],
            "openrouter": [{"label": "home", "source": "env", "auth_type": "api_key", "priority": 0,
                            "last_status": "exhausted", "last_status_at": now - 10,
                            "last_error_reset_at": now + 3600}]}}))
        h.write("logs/errors.log", "%s,123 WARNING [s9] agent.conversation_loop: API call failed (attempt 1/1) "
                                    "error_type=RateLimit provider=zai model=model-d summary=HTTP 429 upstream says no\n"
                % datetime.fromtimestamp(now - 120).strftime("%Y-%m-%d %H:%M:%S"))
        h.card("t_1", "blocked", block_kind="needs_input")
        h.quota_cache({"anthropic": anthropic_record(30, 50, now + 3600, now + 86400),
                       "zai": {"label": "zai", "unavailable_reason": "no-credentials", "windows": []}},
                      iso(now - 30))


class SnapshotTest(_Base):
    def test_snapshot_contract(self):
        self.seed()
        snap = qm.snapshot()
        self.assertEqual(snap["monitor_version"], 1)
        self.assertAlmostEqual(snap["generated_at"], time.time(), delta=30)
        for key in ("live", "health", "kanban", "usage", "top_sessions", "clis", "errors"):
            self.assertIn(key, snap)
        self.assertEqual(snap["errors"], {})
        live = snap["live"]
        self.assertNotIn("quotas", live)
        for key in ("running", "counts", "burn", "last_1m", "last_5m", "last_60m", "cost_60m", "by_source_60m",
                    "providers", "by_profile", "tokens", "activity", "sampling_since", "sample_every", "attention"):
            self.assertIn(key, live)
        self.assertEqual(live["counts"]["your_turn"], 1)
        self.assertEqual(set(snap["usage"]), {"today", "month"})
        for rng in snap["usage"].values():
            self.assertEqual(set(rng), {"days", "totals", "providers", "daily", "by_provider", "by_model",
                                        "by_profile"})
        by_profile = {p["profile"]: p for p in snap["usage"]["month"]["by_profile"]}
        self.assertEqual(by_profile["default"]["cost"], 3.0)
        self.assertEqual(by_profile["work"]["cost"], 1.0)
        self.assertEqual(snap["top_sessions"][0]["id"], "s1")
        self.assertEqual(snap["kanban"]["boards"][0]["cards"][0]["id"], "t_1")
        self.assertEqual(set(snap["clis"]), {"sources", "daily", "by_model", "sessions", "presence"})
        self.assertEqual(set(snap["health"]), {"now", "accounts", "chains", "models", "speed"})
        json.dumps(snap)  # serialisable
        self.assertNotIn("synthetic-not-a-token", json.dumps(snap), "credentials never leave auth.json")

    def test_a_failing_section_lands_in_errors(self):
        self.seed()
        with mock.patch.object(usage, "usage_today_month", side_effect=RuntimeError("boom")):
            snap = qm.snapshot()
        self.assertEqual(snap["errors"], {"usage": "RuntimeError: boom"})
        self.assertIsNone(snap["usage"])
        self.assertIsNotNone(snap["live"])
        self.assertIsNotNone(snap["health"])

    def test_snapshot_on_an_empty_home(self):
        snap = qm.snapshot()
        self.assertEqual(snap["errors"], {})
        self.assertEqual(snap["live"]["running"], [])
        self.assertEqual(snap["kanban"], {"boards": []})


class HealthTest(_Base):
    def test_accounts_models_chains(self):
        self.seed()
        h = qm._health(load("quota_monitor.common").profile_homes(), load("quota_monitor.quotas").items_from_cache())
        acc = {a["provider"]: a for a in h["accounts"]}
        self.assertFalse(acc["anthropic"]["credentials"][0]["locked"])
        self.assertEqual(acc["anthropic"]["windows"][0]["used"], 30.0)
        orr = acc["openrouter"]["credentials"][0]
        self.assertTrue(orr["locked"])
        self.assertAlmostEqual(orr["exhausted_until"], self.now + 3600, delta=1)
        self.assertNotIn("access_token", json.dumps(h))
        models = {(m["provider"], m["model"]): m for m in h["models"]}
        self.assertEqual(models[("zai", "model-d")]["status"], "failing")
        self.assertEqual(models[("zai", "model-d")]["error_types"], {"HTTP 429": 1})
        self.assertEqual(models[("anthropic", "model-a")]["status"], "ok")
        self.assertEqual(models[("openrouter", "model-c")]["status"], "account_exhausted")
        chain = next(c for c in h["chains"] if c["profile"] == "default")
        self.assertEqual([(f["model"], f["state"], f["reason"]) for f in chain["fallbacks"]],
                         [("model-b", "skip", "same_backend"), ("model-c", "skip", "locked"),
                          ("model-d", "skip", "no_credentials")])

    def test_recommend_ranks_usable_models_and_explains_exclusions(self):
        self.seed()
        rec = qm.recommend_payload()
        self.assertEqual(rec["monitor_version"], 1)
        self.assertEqual([r["model"] for r in rec["recommended"]], ["model-a"])
        self.assertIn("50% quota left", rec["recommended"][0]["reasons"])
        reasons = {e["model"]: e["reason"] for e in rec["excluded"]}
        self.assertEqual(reasons["model-d"], "status failing")
        self.assertEqual(reasons["model-c"], "status account_exhausted")
        low = qm.recommend_payload(min_quota_left_pct=80)
        self.assertEqual(low["recommended"], [])
        self.assertIn("only 50% quota left", {e["reason"] for e in low["excluded"]})


class CliTest(_Base):
    def _cli(self, *argv):
        parser = argparse.ArgumentParser()
        commands.setup_argparse(parser)
        args = parser.parse_args(list(argv))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_each_subcommand_prints_one_json_object(self):
        self.seed()
        for argv in (("monitor", "snapshot", "--json"), ("monitor", "history", "--json", "--days", "7"),
                     ("monitor", "recommend", "--json", "--exclude", "zai,openrouter", "--top", "1"),
                     ("monitor", "budget", "--json")):
            with self.subTest(argv=argv):
                rc, out = self._cli(*argv)
                self.assertIn(rc, (0, None))
                self.assertEqual(len(out.strip().splitlines()), 1)
                payload = json.loads(out)
                self.assertEqual(payload["monitor_version"], 1)
                self.assertIn("generated_at", payload)

    def test_set_budget_round_trip(self):
        rc, out = self._cli("monitor", "set-budget", "--daily", "20", "--monthly", "400", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["budget"], {"daily": 20.0, "monthly": 400.0})
        stored = json.loads((self.home.root / "quota_monitor.json").read_text())
        self.assertEqual(stored, {"budget": {"daily": 20.0, "monthly": 400.0}})
        self._cli("monitor", "set-budget", "--daily", "0")
        self.assertEqual(qm.read_budget(), {"daily": None, "monthly": 400.0})
        rc, out = self._cli("monitor", "set-budget", "--daily", "-1")
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out))

    def test_budget_uses_month_usage(self):
        self.seed()
        qm.set_budget(daily=6.0, monthly=100.0)
        b = qm.budget_payload()
        self.assertEqual(b["daily_budget"], 6.0)
        if datetime.fromtimestamp(self.now - 4000).date() == datetime.now().date():
            self.assertEqual(b["today"], 4.0)
            self.assertEqual(b["daily_used_pct"], 67)
        self.assertGreaterEqual(b["month_to_date"], 3.0)

    def test_existing_commands_keep_working(self):
        rc, out = self._cli("status", "--json", "--cached")
        self.assertIn("providers", json.loads(out))


class ToolsTest(_Base):
    def test_tools_register_and_answer_without_titles_or_error_text(self):
        self.seed()
        registered = {}

        class Ctx:
            def register_tool(self, name, toolset, schema, handler, **kw):
                registered[name] = (toolset, schema, handler)

        names = tools.register_tools(Ctx())
        self.assertEqual(set(names), {"get_quota_status", "get_model_health", "get_fallback_chains", "get_budget",
                                      "get_running_counts", "get_agent_attention", "recommend_model"})
        for name, (toolset, schema, handler) in registered.items():
            with self.subTest(tool=name):
                self.assertEqual(schema["name"], name)
                self.assertEqual(toolset, "quota_monitor")
                out = handler({})
                payload = json.loads(out)
                self.assertNotIn("error", payload)
                self.assertNotIn("private title", out)
                self.assertNotIn("upstream says no", out)
                self.assertNotIn("title", walk_keys(payload))
        att = json.loads(registered["get_agent_attention"][2]({}))
        self.assertEqual(att["count"], 1)
        self.assertEqual(att["items"][0]["kind"], "your_turn")
        q = json.loads(registered["get_quota_status"][2]({"provider": "anthropic"}))
        self.assertEqual(q["providers"][0]["min_left_pct"], 50)

    def test_tool_failure_is_generic(self):
        with mock.patch.object(qm, "_routing_inputs", side_effect=RuntimeError("secret upstream text")):
            out = tools.get_quota_status({})
        self.assertEqual(json.loads(out), {"error": "quota monitor unavailable (RuntimeError)"})


if __name__ == "__main__":
    unittest.main()
