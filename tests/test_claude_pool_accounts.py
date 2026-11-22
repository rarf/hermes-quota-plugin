"""``claudeAccounts`` entries backed by Hermes' credential pool (``poolId``).

An extra Claude account may name a ``hermes auth list`` row
instead of a Claude Code ``configDir``. These tests pin that the row is found
through the core pool API (never ``auth.json``), that refresh is delegated to
the core pool, and that dedup/failure rules match the ``configDir`` path.

No network, no real credentials: the pool module and the HTTP opener are fakes.
"""

from __future__ import annotations

import json
import sys
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quota_providers.builtin as builtin  # noqa: E402


class _Row:
    def __init__(self, id, label, token, *, auth_type="oauth", status=None, expires_ms=None):  # noqa: A002
        self.id = id
        self.label = label
        self.access_token = token
        self.auth_type = auth_type
        self.last_status = status
        self.expires_at_ms = expires_ms


class _Pool:
    def __init__(self, rows, refreshed=None):
        self._rows = rows
        self._refreshed = refreshed or {}
        self.refresh_calls = []

    def entries(self):
        return list(self._rows)

    def try_refresh_matching(self, api_key_hint=None, credential_id=None):
        self.refresh_calls.append(credential_id)
        return self._refreshed.get(credential_id)


def _modules(settings, pool):
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"plugins": {"entries": {"quota": {"settings": settings}}}}
    cli = types.ModuleType("hermes_cli")
    cli.config = config
    cp = types.ModuleType("agent.credential_pool")
    cp.load_pool = lambda provider: pool if provider == "anthropic" else _Pool([])
    agent = types.ModuleType("agent")
    agent.credential_pool = cp
    return {"hermes_cli": cli, "hermes_cli.config": config, "agent": agent, "agent.credential_pool": cp}


class _Resp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]


def _payload(percent):
    return {"five_hour": {"utilization": percent, "resets_at": "2026-09-30T07:59:59+00:00"}}


def _opener(mapping, seen=None):
    def _open(req, timeout=None):  # noqa: ARG001
        token = (req.get_header("Authorization") or "")[len("Bearer "):]
        if seen is not None:
            seen.append(token)
        if token not in mapping:
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
        return _Resp(json.dumps(mapping[token]).encode())
    return _open


def _fetch(settings, pool, mapping, *, primary="PRIMARY", seen=None):
    with mock.patch.dict(sys.modules, _modules(settings, pool)), \
         mock.patch.object(builtin, "_core_anthropic_token", return_value=primary), \
         mock.patch.object(builtin, "_core_anthropic_is_oauth", return_value=True), \
         mock.patch.object(builtin, "_load_shared_payload", return_value=None), \
         mock.patch.object(builtin, "_store_shared_payload", return_value=None), \
         mock.patch.object(builtin, "_anthropic_cooling_down", return_value=False), \
         mock.patch.object(builtin, "urlopen_no_redirect", _opener(mapping, seen)):
        return builtin._fetch_anthropic()


class PoolSettingTests(unittest.TestCase):
    def _parse(self, accounts):
        with mock.patch.dict(sys.modules, _modules({"claudeAccounts": accounts}, _Pool([]))):
            return builtin._claude_accounts_setting()

    def test_pool_entry_is_accepted_without_config_dir(self):
        entries, configured = self._parse([{"id": "conta2", "label": "Conta 2", "poolId": "a1b2c3"}])
        self.assertTrue(configured)
        self.assertEqual(entries, [{"id": "conta2", "label": "Conta 2", "pool_ref": "a1b2c3"}])

    def test_both_config_dir_and_pool_id_is_invalid(self):
        entries, _ = self._parse([{"id": "x", "configDir": "/tmp/x", "poolId": "a1b2c3"}])
        self.assertEqual(entries[0]["error"], "config-invalid")

    def test_blank_or_non_string_pool_id_is_invalid(self):
        for bad in ("", "   ", 7, "a\nb"):
            entries, _ = self._parse([{"id": "x", "poolId": bad}])
            self.assertEqual(entries[0]["error"], "config-invalid", bad)


class PoolFetchTests(unittest.TestCase):
    SETTINGS = {"claudeAccounts": [{"id": "conta2", "label": "Conta 2", "poolId": "a1b2c3"}]}

    def test_pool_account_gets_its_own_card(self):
        pool = _Pool([_Row("d4e5f6", "anthropic-oauth-1", "PRIMARY"), _Row("a1b2c3", "conta2", "TOK2")])
        res = _fetch(self.SETTINGS, pool, {"PRIMARY": _payload(10), "TOK2": _payload(70)})
        self.assertEqual([w.used_percent for w in res.windows], [10.0])
        self.assertEqual([a.id for a in res.accounts], ["conta2"])
        self.assertEqual(res.accounts[0].label, "Conta 2")
        self.assertEqual([w.used_percent for w in res.accounts[0].windows], [70.0])
        self.assertEqual(pool.refresh_calls, [])  # token still valid: no refresh

    def test_row_is_found_by_label_too(self):
        settings = {"claudeAccounts": [{"id": "c2", "poolId": "conta2"}]}
        pool = _Pool([_Row("a1b2c3", "conta2", "TOK2")])
        res = _fetch(settings, pool, {"PRIMARY": _payload(1), "TOK2": _payload(5)})
        self.assertEqual([w.used_percent for w in res.accounts[0].windows], [5.0])

    def test_ambiguous_label_is_config_invalid(self):
        settings = {"claudeAccounts": [{"id": "c2", "poolId": "dup"}]}
        pool = _Pool([_Row("aaa111", "dup", "T1"), _Row("bbb222", "dup", "T2")])
        res = _fetch(settings, pool, {"PRIMARY": _payload(1)})
        self.assertEqual(res.accounts[0].unavailable_reason, "config-invalid")
        self.assertTrue(res.accounts[0].details)

    def test_missing_row_is_no_credentials(self):
        res = _fetch(self.SETTINGS, _Pool([]), {"PRIMARY": _payload(1)})
        self.assertEqual(res.accounts[0].unavailable_reason, "no-credentials")

    def test_primary_row_listed_again_is_deduped(self):
        settings = {"claudeAccounts": [{"id": "me", "poolId": "d4e5f6"}]}
        pool = _Pool([_Row("d4e5f6", "anthropic-oauth-1", "PRIMARY")])
        res = _fetch(settings, pool, {"PRIMARY": _payload(1)})
        self.assertEqual(res.accounts, [])

    def test_dead_row_is_auth_failed_without_a_request(self):
        seen = []
        pool = _Pool([_Row("a1b2c3", "conta2", "TOK2", status="dead")])
        res = _fetch(self.SETTINGS, pool, {"PRIMARY": _payload(1)}, seen=seen)
        self.assertEqual(res.accounts[0].unavailable_reason, "auth-failed")
        self.assertNotIn("TOK2", seen)

    def test_api_key_row_is_refused(self):
        pool = _Pool([_Row("a1b2c3", "conta2", "sk-ant-api", auth_type="api_key")])
        res = _fetch(self.SETTINGS, pool, {"PRIMARY": _payload(1)})
        self.assertEqual(res.accounts[0].unavailable_reason, builtin._ANTHROPIC_OAUTH_REQUIRED_REASON)

    def test_expiring_token_is_refreshed_by_the_core_pool(self):
        soon = int(time.time() * 1000) + 10_000
        pool = _Pool([_Row("a1b2c3", "conta2", "OLD", expires_ms=soon)],
                     refreshed={"a1b2c3": _Row("a1b2c3", "conta2", "NEW")})
        seen = []
        res = _fetch(self.SETTINGS, pool, {"PRIMARY": _payload(1), "NEW": _payload(40)}, seen=seen)
        self.assertEqual(pool.refresh_calls, ["a1b2c3"])
        self.assertNotIn("OLD", seen)
        self.assertEqual([w.used_percent for w in res.accounts[0].windows], [40.0])

    def test_401_triggers_one_core_refresh_and_retry(self):
        pool = _Pool([_Row("a1b2c3", "conta2", "REVOKED")],
                     refreshed={"a1b2c3": _Row("a1b2c3", "conta2", "FRESH")})
        res = _fetch(self.SETTINGS, pool, {"PRIMARY": _payload(1), "FRESH": _payload(55)})
        self.assertEqual(pool.refresh_calls, ["a1b2c3"])
        self.assertEqual([w.used_percent for w in res.accounts[0].windows], [55.0])

    def test_failed_refresh_after_401_is_auth_failed(self):
        pool = _Pool([_Row("a1b2c3", "conta2", "REVOKED")])
        res = _fetch(self.SETTINGS, pool, {"PRIMARY": _payload(1)})
        self.assertEqual(pool.refresh_calls, ["a1b2c3"])
        self.assertEqual(res.accounts[0].unavailable_reason, "auth-failed")

    def test_no_core_pool_module_is_fetcher_unavailable(self):
        mods = _modules(self.SETTINGS, _Pool([]))
        mods["agent.credential_pool"] = None  # import raises ImportError
        with mock.patch.dict(sys.modules, mods), \
             mock.patch.object(builtin, "_core_anthropic_token", return_value=None):
            res = builtin._fetch_anthropic()
        self.assertEqual(res.accounts[0].unavailable_reason, "fetcher-unavailable")

    def test_token_never_reaches_the_account_payload(self):
        pool = _Pool([_Row("a1b2c3", "conta2", "SECRET-TOKEN")])
        res = _fetch(self.SETTINGS, pool, {"PRIMARY": _payload(1), "SECRET-TOKEN": _payload(2)})
        self.assertNotIn("SECRET-TOKEN", repr(res.accounts))


class PrimaryLabelTests(unittest.TestCase):
    def _run(self, settings):
        pool = _Pool([_Row("d4e5f6", "anthropic-oauth-1", "PRIMARY"), _Row("a1b2c3", "conta2", "TOK2")])
        return _fetch(settings, pool, {"PRIMARY": _payload(10), "TOK2": _payload(70)})

    def test_primary_label_names_the_core_card(self):
        res = self._run({"claudePrimaryLabel": "Pessoal",
                         "claudeAccounts": [{"id": "conta2", "label": "Work", "poolId": "a1b2c3"}]})
        self.assertEqual(res.account_label, "Pessoal")
        self.assertEqual(res.accounts[0].label, "Work")

    def test_primary_label_works_without_extra_accounts(self):
        res = self._run({"claudePrimaryLabel": "Pessoal"})
        self.assertEqual(res.account_label, "Pessoal")
        self.assertEqual(res.accounts, [])

    def test_invalid_primary_label_is_ignored(self):
        for bad in ("", "  ", 3, "x" * 81, "a\nb"):
            self.assertIsNone(self._run({"claudePrimaryLabel": bad}).account_label, bad)

    def test_cache_and_cli_show_both_names(self):
        import importlib
        import os
        import tempfile
        if "hermes_constants" not in sys.modules:
            stub = types.ModuleType("hermes_constants")
            stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME") or tempfile.gettempdir())
            sys.modules["hermes_constants"] = stub
        if "quota_plugin_under_test" not in sys.modules:
            pkg = types.ModuleType("quota_plugin_under_test")
            pkg.__path__ = [str(ROOT)]
            sys.modules["quota_plugin_under_test"] = pkg
        qc = importlib.import_module("quota_plugin_under_test.quota_cache")
        base = importlib.import_module("quota_plugin_under_test.quota_providers.base")
        commands = importlib.import_module("quota_plugin_under_test.commands")
        res = self._run({"claudePrimaryLabel": "Pessoal",
                         "claudeAccounts": [{"id": "conta2", "label": "Work", "poolId": "a1b2c3"}]})
        # Rebuild with the package-under-test classes (isinstance checks in the cache).
        acc = res.accounts[0]
        mirrored = base.QuotaResult(
            label=res.label, account_label=res.account_label,
            windows=[base.QuotaWindow(w.label, w.used_percent, w.reset_at) for w in res.windows],
            accounts=[base.QuotaAccount(id=acc.id, label=acc.label, windows=[
                base.QuotaWindow(w.label, w.used_percent, w.reset_at) for w in acc.windows])])
        rows = qc._expand_accounts("anthropic", qc._result_to_record(mirrored))
        self.assertEqual(rows["anthropic"]["account_label"], "Pessoal")
        self.assertEqual(commands._provider_label("anthropic", rows["anthropic"]), "Anthropic · Pessoal")
        self.assertEqual(commands._provider_label("anthropic:conta2", rows["anthropic:conta2"]), "Anthropic · Work")



class PrimaryFollowsListedAccountTests(unittest.TestCase):
    """The primary card is named after the listed account whose token it uses.

    The core resolves the primary token from the pool row with the highest
    priority. When that row is also listed in ``claudeAccounts`` its card is
    deduped into the primary, so the primary must carry its name; otherwise a
    priority change would show one account under the other's name and hide it.
    """

    ROWS = [_Row("d4e5f6", "anthropic-oauth-1", "PERSONAL"), _Row("a1b2c3", "conta2", "WORK")]
    MAPPING = {"PERSONAL": _payload(10), "WORK": _payload(70)}
    BOTH = [{"id": "personal", "label": "Personal", "poolId": "d4e5f6"},
            {"id": "work", "label": "Work", "poolId": "a1b2c3"}]

    def _run(self, primary, accounts, **extra):
        settings = {"claudeAccounts": accounts, **extra}
        return _fetch(settings, _Pool(list(self.ROWS)), self.MAPPING, primary=primary)

    def test_primary_takes_the_matching_entry_label(self):
        res = self._run("WORK", self.BOTH)
        self.assertEqual(res.account_label, "Work")
        self.assertEqual([(a.id, a.label) for a in res.accounts], [("personal", "Personal")])
        self.assertEqual(res.windows[0].used_percent, 70.0)
        self.assertEqual(res.accounts[0].windows[0].used_percent, 10.0)

    def test_names_follow_the_account_when_priority_flips(self):
        res = self._run("PERSONAL", self.BOTH)
        self.assertEqual(res.account_label, "Personal")
        self.assertEqual([(a.id, a.label) for a in res.accounts], [("work", "Work")])
        self.assertEqual(res.windows[0].used_percent, 10.0)

    def test_matching_entry_wins_over_claude_primary_label(self):
        res = self._run("WORK", self.BOTH, claudePrimaryLabel="Pessoal")
        self.assertEqual(res.account_label, "Work")

    def test_primary_label_still_applies_when_nothing_matches(self):
        res = self._run("PERSONAL", [self.BOTH[1]], claudePrimaryLabel="Personal")
        self.assertEqual(res.account_label, "Personal")
        self.assertEqual([a.label for a in res.accounts], ["Work"])

    def test_unlabelled_match_uses_the_entry_id(self):
        res = self._run("WORK", [{"id": "work", "poolId": "a1b2c3"}])
        self.assertEqual(res.account_label, "work")
        self.assertEqual(res.accounts, [])

    def test_config_dir_entry_with_the_primary_token_names_it_too(self):
        with mock.patch.object(builtin, "_read_claude_account_token", return_value=("WORK", None)), \
             mock.patch.object(builtin, "_claude_subscription_type", return_value=None):
            res = self._run("WORK", [{"id": "work", "label": "Work", "configDir": "/tmp/claude-work"}])
        self.assertEqual(res.account_label, "Work")
        self.assertEqual(res.accounts, [])

    def test_first_matching_entry_wins(self):
        accounts = [{"id": "a", "label": "First", "poolId": "a1b2c3"},
                    {"id": "b", "label": "Second", "poolId": "conta2"}]
        res = self._run("WORK", accounts)
        self.assertEqual(res.account_label, "First")
        self.assertEqual(res.accounts, [])

@unittest.skipUnless(__import__("shutil").which("node"), "Node.js is required for widget render tests")
class PrimaryLabelWidgetTests(unittest.TestCase):
    def test_widget_row_and_chip_show_the_primary_label(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from widget_harness import render, text
        primary = {"label": "anthropic", "account_label": "Pessoal",
                   "windows": [{"label": "Current session", "used_percent": 10.0}]}
        for component in ("row", "chip"):
            rendered = text(render(component=component, id="anthropic", provider=primary))
            self.assertIn("Anthropic · Pessoal", rendered, component)


if __name__ == "__main__":
    unittest.main()
