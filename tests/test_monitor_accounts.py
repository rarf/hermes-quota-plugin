"""Accounts tab backend: listing, ownership, validation and the official-CLI write path.

Everything runs against a synthetic Hermes root in a temp dir (synthetic labels like ``work`` / ``home``,
fake token strings that must never reach the output). The write tests inject a runner that records the argv
the plugin hands to Hermes' CLI and applies the change the way Hermes would, so they run without Hermes.
``RealHermesCliTests`` additionally drives Hermes' real ``auth priority`` / ``config set`` against the same
synthetic root when a Hermes checkout is available (skipped otherwise); it pins HOME and the platform
default home to the temp dir, so the real ``~/.hermes`` is never resolved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from monitor_fixtures import load

accounts = load("quota_monitor.accounts")
qm = load("quota_monitor")
store = load("quota_monitor.store")

SECRET_BITS = ("SYNTH-ACCESS", "SYNTH-REFRESH", "sk-synthetic", "SYNTH-AGENT")


def _row(rid, label, priority, source="manual", auth_type="oauth", **extra):
    row = {"id": rid, "label": label, "auth_type": auth_type, "priority": priority, "source": source,
           "access_token": "SYNTH-ACCESS-" + rid, "refresh_token": "SYNTH-REFRESH-" + rid,
           "agent_key": "SYNTH-AGENT-" + rid, "base_url": "https://synthetic.invalid/v1", "request_count": 3}
    row.update(extra)
    return row


def _yaml(data) -> str:
    # tiny emitter: JSON is valid YAML, and read_config parses YAML
    return json.dumps(data, indent=1)


def build_root(base: Path) -> Path:
    """root (default) owns anthropic [work, home, spare] + openrouter [key1]; profile `coder` borrows them;
    profile `solo` owns its own anthropic [solo-acc]; profile `bare` has no config at all."""
    root = base / "root"
    (root / "profiles" / "coder").mkdir(parents=True)
    (root / "profiles" / "solo").mkdir(parents=True)
    (root / "profiles" / "bare").mkdir(parents=True)
    now = time.time()
    auth = {"version": 1, "credential_pool": {
        "anthropic": [
            _row("aa11bb", "work", 0),
            _row("cc22dd", "home", 1, last_status="exhausted", last_status_at=now - 60,
                 last_error_code=429, last_error_reset_at=now + 3600),
            _row("ee33ff", "spare", 2, source="claude_code", model_cooldowns={"claude-opus": now + 900}),
        ],
        "openrouter": [_row("0a1b2c", "key1", 0, auth_type="api_key", source="env:OPENROUTER_API_KEY",
                            api_key="sk-synthetic-openrouter")],
    }}
    (root / "auth.json").write_text(json.dumps(auth))
    (root / "config.yaml").write_text(_yaml({
        "model": {"provider": "anthropic", "default": "claude-synth"},
        "fallback_providers": [{"provider": "openrouter", "model": "synth/model-a", "api_key": "sk-synthetic-fb",
                                "base_url": "https://synthetic.invalid"}],
        "credential_pool_strategies": {"anthropic": "round_robin"},
    }))
    (root / "profiles" / "coder" / "config.yaml").write_text(_yaml({
        "model": {"provider": "openrouter", "default": "synth/model-b"},
        "fallback_providers": [{"provider": "anthropic", "model": "claude-synth"}],
    }))
    (root / "profiles" / "solo" / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {
        "anthropic": [_row("5a5a5a", "solo-acc", 0)]}}))
    (root / "profiles" / "solo" / "config.yaml").write_text(_yaml({
        "model": {"provider": "anthropic", "default": "claude-synth"},
        "credential_pool_strategies": {"anthropic": "least_used"},
    }))
    (root / "quota_cache.json").write_text(json.dumps({
        "fetched_at": "2026-10-22T10:00:00+00:00",
        "providers": {
            "anthropic": {"label": "anthropic", "plan": "max", "unavailable_reason": None, "details": [],
                          "windows": [{"label": "5h", "used_percent": 40, "reset_at": "2026-10-22T12:00:00+00:00"}]},
            "anthropic:home": {"label": "home", "account_label": "home", "provider": "anthropic",
                               "unavailable_reason": None, "details": [],
                               "windows": [{"label": "5h", "used_percent": 91, "reset_at": None}]},
            "openrouter": {"label": "openrouter", "unavailable_reason": None, "details": [], "windows": []},
            "nous": {"label": "nous", "unavailable_reason": "not-logged-in", "details": [], "windows": []},
            "grok": {"label": "grok", "unavailable_reason": "opt-in-disabled", "details": [], "windows": []},
            "copilot": {"label": "copilot", "unavailable_reason": "no-credentials", "details": [], "windows": []},
            "openai-codex": {"label": "openai-codex", "unavailable_reason": "reauth-required", "details": [],
                             "windows": []},
            "gemini": {"label": "gemini", "unavailable_reason": "rate-limited", "details": [], "windows": []},
        }}))
    return root


class _Env:
    """HERMES_HOME -> a profile dir of the synthetic root for the duration of a test."""

    def __init__(self, home: Path):
        self.home = home

    def __enter__(self):
        self.patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class FakeHermes:
    """Stands in for Hermes' CLI: records argv and applies the change like `auth priority` / `config set`."""

    def __init__(self, root: Path, code: int = 0, output: str = "ok"):
        self.root, self.code, self.output, self.calls = root, code, output, []

    def home(self, profile):
        return self.root if profile == "default" else self.root / "profiles" / profile

    def __call__(self, argv, env):
        self.calls.append(list(argv))
        if self.code != 0:
            return self.code, self.output
        assert argv[0] == "--profile"
        home = self.home(argv[1])
        if argv[2:4] == ["auth", "priority"]:
            provider, target, prio = argv[4], argv[5], int(argv[6])
            path = home / "auth.json"
            data = json.loads(path.read_text())
            rows = sorted(data["credential_pool"][provider], key=lambda r: r["priority"])
            moving = next(r for r in rows if r["id"] == target)
            rows.remove(moving)
            rows.insert(prio, moving)
            for i, r in enumerate(rows):
                r["priority"] = i
            data["credential_pool"][provider] = rows
            path.write_text(json.dumps(data))
        elif argv[2:4] == ["config", "set"]:
            key, value = argv[4], argv[5]
            path = home / "config.yaml"
            cfg = json.loads(path.read_text()) if path.exists() else {}
            _, prov = key.split(".", 1)
            cfg.setdefault("credential_pool_strategies", {})[prov.replace("\\.", ".")] = value
            path.write_text(json.dumps(cfg))
        return 0, self.output


class AccountsListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qm-acc-"))
        self.root = build_root(self.tmp)
        self.env = _Env(self.root)
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self):
        return accounts.accounts_payload()

    def test_pools_ownership_and_sharing(self):
        p = self.payload()
        pools = {(x["provider"], x["owner"]): x for x in p["pools"]}
        self.assertEqual(set(pools), {("anthropic", "default"), ("anthropic", "solo"), ("openrouter", "default")})
        shared = pools[("anthropic", "default")]
        self.assertTrue(shared["shared"])
        self.assertEqual(sorted(shared["used_by"]), ["bare", "coder", "default"])
        self.assertEqual(pools[("anthropic", "solo")]["used_by"], ["solo"])
        self.assertFalse(pools[("anthropic", "solo")]["shared"])
        prof = {x["profile"]: x for x in p["profiles"]}
        self.assertEqual(prof["coder"]["pools"], {"anthropic": "shared", "openrouter": "shared"})
        self.assertEqual(prof["solo"]["pools"], {"anthropic": "own", "openrouter": "shared"})
        self.assertEqual(prof["default"]["pools"]["anthropic"], "own")

    def test_rows_order_status_and_tried_first(self):
        rows = next(x for x in self.payload()["pools"] if x["owner"] == "default" and x["provider"] == "anthropic")["rows"]
        self.assertEqual([r["label"] for r in rows], ["work", "home", "spare"])
        self.assertEqual([r["status"] for r in rows], ["active", "exhausted", "cooldown"])
        self.assertEqual([r["tried_first"] for r in rows], [True, False, False])
        self.assertEqual(rows[2]["cooldown_models"], ["claude-opus"])
        self.assertIsNotNone(rows[1]["status_until"])

    def test_quota_matched_by_label_only(self):
        rows = next(x for x in self.payload()["pools"] if x["owner"] == "default" and x["provider"] == "anthropic")["rows"]
        self.assertEqual(rows[1]["quota_match"], "label")
        self.assertEqual(rows[1]["windows"][0]["used"], 91)
        # several rows and no label match: the provider-level reading is not pinned on an arbitrary row
        self.assertEqual(rows[0]["windows"], [])

    def test_strategies_models_fallbacks(self):
        p = self.payload()
        pools = {(x["provider"], x["owner"]): x for x in p["pools"]}
        self.assertEqual(pools[("anthropic", "default")]["strategies"],
                         {"default": "round_robin", "coder": "fill_first", "bare": "fill_first"})
        self.assertEqual(pools[("anthropic", "solo")]["strategies"], {"solo": "least_used"})
        prof = {x["profile"]: x for x in p["profiles"]}
        self.assertEqual(prof["coder"]["model"], {"provider": "openrouter", "model": "synth/model-b"})
        self.assertEqual(prof["default"]["fallbacks"],
                         [{"provider": "openrouter", "model": "synth/model-a", "custom_route": True}])
        self.assertFalse(prof["default"]["fallback_editable"])
        self.assertEqual(prof["solo"]["fallback_command"], "hermes --profile solo fallback")
        self.assertEqual(p["capabilities"]["reorder_fallbacks"], False)

    def test_unavailable_reasons_are_classified(self):
        un = {u["provider"]: u for u in self.payload()["unavailable"]}
        self.assertEqual(un["nous"]["kind"], "signin")
        self.assertEqual(un["nous"]["command"], "hermes auth add nous")
        self.assertEqual(un["nous"]["settings_path"], "/settings?tab=providers&pview=accounts")
        self.assertEqual(un["copilot"]["kind"], "signin")
        self.assertEqual(un["copilot"]["command"], "gh auth login")
        self.assertEqual(un["openai-codex"]["kind"], "reauth")
        self.assertEqual(un["openai-codex"]["command"], "hermes auth add openai-codex")
        self.assertEqual(un["grok"]["kind"], "optin")
        self.assertEqual(un["grok"]["command"], "hermes config set plugins.entries.quota.settings.grokEnabled true")
        self.assertEqual(un["gemini"]["kind"], "rate_limited")
        self.assertIsNone(un["gemini"]["command"])
        self.assertIsNone(un["gemini"]["settings_path"])

    def test_anthropic_rate_limit_retry_time(self):
        now = time.time()
        (self.root / "quota_anthropic_backoff.json").write_text(json.dumps({"digest1": now + 600, "old": now - 5}))
        cache = json.loads((self.root / "quota_cache.json").read_text())
        cache["providers"]["anthropic"]["unavailable_reason"] = "rate-limited"
        (self.root / "quota_cache.json").write_text(json.dumps(cache))
        un = {u["provider"]: u for u in self.payload()["unavailable"]}
        self.assertAlmostEqual(un["anthropic"]["retry_at"], now + 600, delta=1)

    def test_no_secrets_in_output(self):
        for home in (self.root, self.root / "profiles" / "coder", self.root / "profiles" / "solo"):
            with _Env(home):
                text = json.dumps(qm.accounts_view())
            for bit in SECRET_BITS + ("synthetic.invalid", "refresh_token", "access_token", "agent_key"):
                self.assertNotIn(bit, text)

    def test_active_profile_detection(self):
        self.assertEqual(self.payload()["active_profile"], "default")
        with _Env(self.root / "profiles" / "coder"):
            self.assertEqual(accounts.accounts_payload()["active_profile"], "coder")

    def test_missing_cache_still_lists_pools(self):
        (self.root / "quota_cache.json").unlink()
        p = self.payload()
        self.assertEqual(p["unavailable"], [])
        self.assertEqual(len(p["pools"]), 3)


class AccountsWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qm-accw-"))
        self.root = build_root(self.tmp)
        self.env = _Env(self.root / "profiles" / "coder")  # the widget's focused profile
        self.env.__enter__()
        self.fake = FakeHermes(self.root)

    def tearDown(self):
        self.env.__exit__()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def auth_bytes(self):
        return {p: p.read_bytes() for p in self.root.rglob("auth.json")}

    def test_priority_goes_through_official_cli_on_the_owner(self):
        res = accounts.set_priority("anthropic", "ee33ff", 0, profile="default", runner=self.fake)
        self.assertEqual(self.fake.calls, [["--profile", "default", "auth", "priority", "anthropic", "ee33ff", "0"]])
        self.assertTrue(res["changed"])
        self.assertEqual(res["before"], ["aa11bb", "cc22dd", "ee33ff"])
        self.assertEqual(res["after"], ["ee33ff", "aa11bb", "cc22dd"])
        self.assertEqual(res["effective"], 0)
        self.assertEqual(sorted(res["affected_profiles"]), ["bare", "coder", "default"])
        self.assertTrue(res["shared"])
        self.assertEqual(res["command"], "hermes --profile default auth priority anthropic ee33ff 0")
        rows = next(x for x in res["state"]["pools"] if x["owner"] == "default" and x["provider"] == "anthropic")["rows"]
        self.assertEqual(rows[0]["label"], "spare")
        audit = store.audit_rows()
        self.assertEqual(audit[0]["action"], "set-priority")
        self.assertEqual(audit[0]["detail"]["after"], ["ee33ff", "aa11bb", "cc22dd"])

    def test_priority_on_borrowed_pool_is_refused_without_writing(self):
        before = self.auth_bytes()
        with self.assertRaises(accounts.AccountsError) as cm:
            accounts.set_priority("anthropic", "ee33ff", 0, profile="coder", runner=self.fake)
        self.assertEqual(cm.exception.code, "borrowed-pool")
        self.assertEqual(cm.exception.extra["owner"], "default")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.auth_bytes(), before)
        self.assertFalse((self.root / "profiles" / "coder" / "auth.json").exists())

    def test_priority_validation(self):
        cases = [
            (("anthropic", "nope", 0, "default"), "unknown-target"),
            (("anthropic", "work", 0, "default"), "unknown-target"),  # labels are not accepted, only pool ids
            (("anthropic", "ee33ff", 3, "default"), "bad-priority"),
            (("anthropic", "ee33ff", -1, "default"), "bad-priority"),
            (("anthropic", "ee33ff", "x", "default"), "bad-priority"),
            (("anthropic", "ee33ff", 0, "ghost"), "unknown-profile"),
            (("anthropic", "ee33ff", 0, "../x"), "bad-profile"),
            (("bad provider!", "ee33ff", 0, "default"), "bad-provider"),
            (("xai", "ee33ff", 0, "default"), "no-pool"),
        ]
        for args, code in cases:
            with self.subTest(args=args):
                with self.assertRaises(accounts.AccountsError) as cm:
                    accounts.set_priority(*args, runner=self.fake)
                self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.fake.calls, [])

    def test_priority_noop_does_not_call_cli(self):
        res = accounts.set_priority("anthropic", "aa11bb", 0, profile="default", runner=self.fake)
        self.assertFalse(res["changed"])
        self.assertEqual(self.fake.calls, [])

    def test_priority_on_owned_profile_pool(self):
        self.assertEqual(accounts.set_priority("anthropic", "5a5a5a", 0, profile="solo", runner=self.fake)["changed"],
                         False)

    def test_cli_failure_is_reported_with_scrubbed_output(self):
        bad = FakeHermes(self.root, code=1, output="boom sk-synthetic-leak-123456 " + "x" * 50)
        with self.assertRaises(accounts.AccountsError) as cm:
            accounts.set_priority("anthropic", "ee33ff", 0, profile="default", runner=bad)
        self.assertEqual(cm.exception.code, "cli-failed")
        self.assertNotIn("sk-synthetic", str(cm.exception))
        out = qm._write_result(lambda: accounts.set_priority("anthropic", "ee33ff", 0, "default", bad))
        self.assertEqual(out["error_code"], "cli-failed")
        self.assertIn("command", out)

    def test_strategy_write(self):
        res = accounts.set_strategy("anthropic", "round_robin", profile="coder", runner=self.fake)
        self.assertEqual(self.fake.calls, [["--profile", "coder", "config", "set",
                                            "credential_pool_strategies.anthropic", "round_robin"]])
        self.assertEqual((res["before"], res["after"], res["changed"]), ("fill_first", "round_robin", True))
        self.assertEqual(res["affected_profiles"], ["coder"])
        prof = next(p for p in res["state"]["profiles"] if p["profile"] == "coder")
        self.assertEqual(prof["strategies"]["anthropic"], "round_robin")
        # the root's own strategy is untouched
        root_prof = next(p for p in res["state"]["profiles"] if p["profile"] == "default")
        self.assertEqual(root_prof["strategies"]["anthropic"], "round_robin")
        self.assertEqual(store.audit_rows()[0]["action"], "set-strategy")

    def test_strategy_dotted_provider_is_escaped(self):
        cmd = []
        with mock.patch.object(accounts, "accounts_payload", side_effect=lambda *a, **k: {
                "profiles": [{"profile": "default", "pools": {"custom:a.b": "own"}, "strategies": {}}],
                "strategies": ["fill_first", "round_robin"], "pools": []}):
            with self.assertRaises(accounts.AccountsError):  # the fake state never changes -> not-applied
                accounts.set_strategy("custom:a.b", "round_robin", "default",
                                      runner=lambda argv, env: (cmd.append(argv), (0, ""))[1])
        self.assertEqual(cmd[0][4], "credential_pool_strategies.custom:a\\.b")

    def test_strategy_validation(self):
        for args, code in [(("anthropic", "fastest", "coder"), "bad-strategy"),
                           (("xai", "random", "coder"), "no-pool"),
                           (("anthropic", "random", "nobody"), "unknown-profile")]:
            with self.subTest(args=args):
                with self.assertRaises(accounts.AccountsError) as cm:
                    accounts.set_strategy(*args, runner=self.fake)
                self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.fake.calls, [])

    def test_strategy_not_applied_is_an_error(self):
        lazy = lambda argv, env: (0, "")  # noqa: E731 - CLI says ok but nothing changed
        with self.assertRaises(accounts.AccountsError) as cm:
            accounts.set_strategy("anthropic", "random", "coder", runner=lazy)
        self.assertEqual(cm.exception.code, "not-applied")


class CommandWiringTests(unittest.TestCase):
    def test_argparse_accepts_the_new_commands(self):
        import argparse
        commands = load("commands")
        parser = argparse.ArgumentParser()
        commands.setup_argparse(parser)
        a = parser.parse_args(["monitor", "set-priority", "--provider", "anthropic", "--target", "ab12cd",
                               "--priority", "0", "--profile", "default", "--json"])
        self.assertEqual((a.monitor_command, a.provider, a.target, a.priority, a.target_profile),
                         ("set-priority", "anthropic", "ab12cd", "0", "default"))
        a = parser.parse_args(["monitor", "set-strategy", "--provider", "anthropic", "--strategy", "random"])
        self.assertIsNone(a.target_profile)
        self.assertEqual(parser.parse_args(["monitor", "accounts", "--json"]).monitor_command, "accounts")


def _hermes_agent() -> Path:
    return Path(os.environ.get("QUOTA_TEST_HERMES_AGENT") or Path.home() / ".hermes" / "hermes-agent")


@unittest.skipUnless((_hermes_agent() / "hermes_cli" / "auth_commands.py").exists(), "no Hermes checkout")
class RealHermesCliTests(unittest.TestCase):
    """The real `hermes auth priority` / `hermes config set` against the synthetic root.

    HOME and HERMES_DATA_DIR_SUFFIX point Hermes' platform default home into the temp dir, so its root
    resolution can never land on the user's ~/.hermes even though HERMES_HOME is a scratch path."""

    def setUp(self):
        # Hermes prepares its dependency venv under <root>/installs on first use (slow), so the synthetic root
        # lives in one reusable scratch dir; everything except installs/ is rebuilt for every test.
        self.tmp = Path(os.environ.get("QUOTA_TEST_HERMES_SCRATCH") or Path(tempfile.gettempdir()) / "qm-real-hermes")
        self.tmp.mkdir(parents=True, exist_ok=True)
        for child in self.tmp.iterdir():
            if child.name != "root":
                shutil.rmtree(child, ignore_errors=True)
        root = self.tmp / "root"
        if root.exists():
            for child in root.iterdir():
                if child.name != "installs":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        staging = Path(tempfile.mkdtemp(prefix="qm-real-stage-"))
        built = build_root(staging)
        root.mkdir(exist_ok=True)
        for child in built.iterdir():
            shutil.move(str(child), str(root / child.name))
        shutil.rmtree(staging, ignore_errors=True)
        self.root = root
        (self.tmp / "home").mkdir()
        self.py = os.environ.get("QUOTA_TEST_HERMES_PY") or sys.executable
        self.env = {"HOME": str(self.tmp / "home"), "HERMES_HOME": str(self.root),
                    "HERMES_DATA_DIR_SUFFIX": "-quota-test", "PYTHONPATH": str(_hermes_agent()),
                    "NO_COLOR": "1", "PATH": "/usr/bin:/bin"}
        if not (self.root / "installs").exists():
            # first use: let Hermes' bootstrap lazily install its dependency env under the scratch root
            prime = ("import sys; sys.path.insert(0, %r); import hermes_bootstrap; import ruamel.yaml"
                     % str(_hermes_agent()))
            subprocess.run([self.py, "-I", "-c", prime], env=self.env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=600)
        probe = self.runner(["auth", "priority", "--help"], {})
        if probe[0] != 0:
            self.skipTest("Hermes CLI not runnable with %s: %s" % (self.py, probe[1][-300:]))

    def tearDown(self):
        pass  # the scratch root is reused (its installs/ cache); its synthetic files are rebuilt in setUp

    def runner(self, argv, extra):
        env = dict(self.env)
        env.update(extra)
        # the launcher's own runtime command (`hermes --print-runtime-command`): bootstrap, then hermes_cli.main
        boot = ("import sys, runpy; sys.path.insert(0, %r); import hermes_bootstrap; "
                "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)" % str(_hermes_agent()))
        p = subprocess.run([self.py, "-I", "-c", boot] + argv, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def test_real_priority_and_strategy(self):
        real_root = Path.home() / ".hermes"
        before_real = (real_root / "auth.json").stat().st_mtime if (real_root / "auth.json").exists() else None
        with _Env(self.root / "profiles" / "coder"):
            res = accounts.set_priority("openrouter", "0a1b2c", 0, profile="default", runner=self.runner)
            self.assertFalse(res["changed"])
            # move `home` (cc22dd, manual) to the front of the shared anthropic pool via Hermes itself
            res = accounts.set_priority("anthropic", "cc22dd", 0, profile="default", runner=self.runner)
            self.assertEqual(res["after"][0], "cc22dd", res.get("cli_output"))
            res = accounts.set_strategy("anthropic", "least_used", profile="coder", runner=self.runner)
            self.assertEqual(res["after"], "least_used", res.get("cli_output"))
        # the borrower never got a copy of the pool, and secrets were preserved by Hermes
        self.assertFalse((self.root / "profiles" / "coder" / "auth.json").exists())
        data = json.loads((self.root / "auth.json").read_text())
        ids = {r["id"]: r for r in data["credential_pool"]["anthropic"]}
        self.assertEqual(ids["cc22dd"]["refresh_token"], "SYNTH-REFRESH-cc22dd")
        after_real = (real_root / "auth.json").stat().st_mtime if (real_root / "auth.json").exists() else None
        self.assertEqual(before_real, after_real)


if __name__ == "__main__":
    unittest.main()
