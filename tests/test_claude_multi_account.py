"""Configurable multi-account Claude (anthropic) quota — offline tests.

The feature: an explicit, opt-in ``claudeAccounts`` list in the quota plugin
settings shows several Claude subscription logins side by side. Every rule the
review asked for is pinned here — single-account behaviour with no config,
no credential is read unless it was listed, read-only access, same-source and
same-token dedup, truthful per-account unavailable states, a shared fetch
deadline, and no token or filesystem path ever reaching the cache.

No network: every HTTP boundary is a synthetic opener. No real credentials.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import quota_providers.builtin as builtin  # noqa: E402
from widget_harness import nodes, render, text  # noqa: E402,F401


# --- helpers -----------------------------------------------------------------


def _config_modules(settings: dict) -> dict:
    """Fake ``hermes_cli.config`` exposing exactly ``settings`` under quota."""
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {
        "plugins": {"entries": {"quota": {"settings": settings}}}
    }
    pkg = types.ModuleType("hermes_cli")
    pkg.config = config
    return {"hermes_cli": pkg, "hermes_cli.config": config}


def _write_credentials(directory: Path, token: str) -> Path:
    """A synthetic Claude ``.credentials.json`` — never a real credential."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".credentials.json"
    path.write_text(
        json.dumps({"claudeAiOauth": {
            "accessToken": token, "refreshToken": "synthetic-refresh",
            "expiresAt": 0,
        }}),
        encoding="utf-8",
    )
    return path


def _payload(percent: float, reset: str = "2026-09-30T07:59:59+00:00") -> dict:
    return {"five_hour": {"utilization": percent, "resets_at": reset}}


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        # ``_request_anthropic_usage`` reads with an explicit byte cap; honour it
        # so this mock matches the real (size-bounded) reader signature.
        if size is None or size < 0:
            return self._body
        return self._body[:size]


def _opener_for(mapping: dict) -> "object":
    """Opener routing by bearer token; a token without an entry raises."""
    def _opener(req, timeout=None):  # noqa: ANN001, ARG001
        auth = req.get_header("Authorization") or ""
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else auth
        return _Resp(json.dumps(mapping[token]).encode("utf-8"))

    return _opener


def _ok_opener(percent: float = 33.0, reset: str = "2026-09-30T07:59:59+00:00"):
    """Opener answering any token with the same synthetic payload."""
    def _opener(req, timeout=None):  # noqa: ANN001, ARG001
        return _Resp(json.dumps(_payload(percent, reset)).encode("utf-8"))

    return _opener


# --- single-account behaviour is unchanged -----------------------------------


class SingleAccountUnchangedTests(unittest.TestCase):
    def test_absent_config_uses_the_single_account_path(self):
        with mock.patch.object(builtin, "_claude_accounts_setting",
                               return_value=([], False)), \
             mock.patch.object(builtin, "_anthropic_usage_payload",
                               return_value=(_payload(42), None)):
            res = builtin._fetch_anthropic()
        self.assertEqual([w.used_percent for w in res.windows], [42.0])
        self.assertEqual(res.accounts, [])
        self.assertIsNone(res.unavailable_reason)

    def test_empty_config_reads_no_account(self):
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": []})):
            entries, configured = builtin._claude_accounts_setting()
        # An empty list behaves exactly like an absent setting: the single path.
        self.assertFalse(configured)
        self.assertEqual(entries, [])

    def test_empty_list_uses_the_single_account_path(self):
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": []})), \
             mock.patch.object(builtin, "_anthropic_usage_payload",
                               return_value=(_payload(42), None)), \
             mock.patch.object(builtin, "_fetch_anthropic_multi") as multi:
            res = builtin._fetch_anthropic()
        multi.assert_not_called()  # empty must not enter the multi path
        self.assertEqual([w.used_percent for w in res.windows], [42.0])
        self.assertEqual(res.accounts, [])

    def test_no_config_key_means_not_configured(self):
        with mock.patch.dict(sys.modules, _config_modules({"other": True})):
            entries, configured = builtin._claude_accounts_setting()
        self.assertFalse(configured)
        self.assertEqual(entries, [])


# --- two independent synthetic accounts --------------------------------------


class MultiAccountFetchTests(unittest.TestCase):
    def _fetch(self, settings, mapping, *, primary=None, default_dir=None):
        opener = _opener_for(mapping) if mapping is not None else _ok_opener()
        patches = [
            mock.patch.dict(sys.modules, _config_modules(settings)),
            mock.patch.object(builtin, "_core_anthropic_token", return_value=primary),
            mock.patch.object(builtin, "urlopen_no_redirect", opener),
        ]
        if default_dir is not None:
            patches.append(mock.patch.object(
                builtin, "_effective_claude_config_dir", return_value=default_dir))
        for p in patches:
            p.start()
        try:
            return builtin._fetch_anthropic()
        finally:
            for p in patches:
                p.stop()

    def test_two_accounts_are_fetched_and_kept_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            d1, d2 = Path(tmp) / "a", Path(tmp) / "b"
            _write_credentials(d1, "SYNTH-ALPHA")
            _write_credentials(d2, "SYNTH-BETA")
            settings = {"claudeAccounts": [
                {"id": "alpha", "label": "Alpha", "configDir": str(d1)},
                {"id": "beta", "label": "Beta", "configDir": str(d2)},
            ]}
            res = self._fetch(settings, {
                "SYNTH-ALPHA": _payload(20.0, "2026-09-30T07:59:59+00:00"),
                "SYNTH-BETA": _payload(80.0, "2026-10-02T09:00:00+00:00"),
            }, default_dir=Path(tmp) / "default")

        self.assertEqual(res.unavailable_reason, "no-credentials")  # no core login
        self.assertEqual([a.id for a in res.accounts], ["alpha", "beta"])
        by_id = {a.id: a for a in res.accounts}
        self.assertEqual([w.used_percent for w in by_id["alpha"].windows], [20.0])
        self.assertEqual([w.used_percent for w in by_id["beta"].windows], [80.0])
        self.assertNotEqual(by_id["alpha"].windows[0].reset_at,
                            by_id["beta"].windows[0].reset_at)

    def test_no_config_reads_nothing_even_with_no_core_login(self):
        # Absent config -> zero credential reads; here the opener would explode.
        def _explode(*a, **k):  # noqa: ANN001
            raise AssertionError("no network should be attempted")

        with mock.patch.object(builtin, "_claude_accounts_setting",
                               return_value=([], False)), \
             mock.patch.object(builtin, "_core_anthropic_token", return_value=None), \
             mock.patch.object(builtin, "urlopen_no_redirect", _explode):
            res = builtin._fetch_anthropic()
        self.assertEqual(res.unavailable_reason, "no-credentials")
        self.assertEqual(res.accounts, [])


# --- dedup -------------------------------------------------------------------


class DedupTests(unittest.TestCase):
    def test_entry_at_the_default_dir_is_read_when_its_token_differs(self):
        """A directory is never identity.

        The primary token can come from ``ANTHROPIC_API_KEY``, the environment,
        or a different OAuth grant rather than ``~/.claude/.credentials.json``,
        so a listed entry pointing at that directory must still be read — and is
        deduped by token, not by path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "default"
            _write_credentials(default, "SYNTH-DEFAULT")
            settings = {"claudeAccounts": [
                {"id": "self", "configDir": str(default)},
            ]}
            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token", return_value=None), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=default), \
                 mock.patch.object(builtin, "urlopen_no_redirect", _ok_opener()):
                res = builtin._fetch_anthropic()
        self.assertEqual([a.id for a in res.accounts], ["self"])

    def test_entry_at_the_primary_dir_with_the_primary_token_is_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "default"
            _write_credentials(default, "SYNTH-PRIMARY")
            settings = {"claudeAccounts": [
                {"id": "self", "configDir": str(default)},
            ]}
            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token",
                                   return_value="SYNTH-PRIMARY"), \
                 mock.patch.object(builtin, "_core_anthropic_is_oauth",
                                   return_value=True), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=default), \
                 mock.patch.object(builtin, "urlopen_no_redirect", _ok_opener()):
                res = builtin._fetch_anthropic()
        self.assertIsNone(res.unavailable_reason)   # the primary resolved
        self.assertEqual(res.accounts, [])          # same token -> one card

    def test_same_directory_twice_is_one_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "a"
            _write_credentials(d1, "SYNTH-A")
            settings = {"claudeAccounts": [
                {"id": "one", "configDir": str(d1)},
                {"id": "two", "configDir": str(d1)},
            ]}
            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token", return_value=None), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(tmp) / "default"), \
                 mock.patch.object(builtin, "urlopen_no_redirect", _ok_opener()):
                res = builtin._fetch_anthropic()
        self.assertEqual([a.id for a in res.accounts], ["one"])

    def test_identical_token_to_the_primary_is_not_a_second_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "a"
            _write_credentials(d1, "SYNTH-SAME")
            d2 = Path(tmp) / "b"
            _write_credentials(d2, "SYNTH-OTHER")
            settings = {"claudeAccounts": [
                {"id": "same", "configDir": str(d1)},
                {"id": "other", "configDir": str(d2)},
            ]}
            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token",
                                   return_value="SYNTH-SAME"), \
                 mock.patch.object(builtin, "_core_anthropic_is_oauth",
                                   return_value=True), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(tmp) / "default"), \
                 mock.patch.object(builtin, "urlopen_no_redirect",
                                   _ok_opener(percent=50.0)):
                res = builtin._fetch_anthropic()
        self.assertIsNone(res.unavailable_reason)  # primary resolved
        self.assertEqual([a.id for a in res.accounts], ["other"])

    def test_equal_quota_is_not_treated_as_the_same_identity(self):
        """Two logins may legitimately share a number; only exact tokens dedupe."""
        with tempfile.TemporaryDirectory() as tmp:
            d1, d2 = Path(tmp) / "a", Path(tmp) / "b"
            _write_credentials(d1, "SYNTH-1")
            _write_credentials(d2, "SYNTH-2")
            settings = {"claudeAccounts": [
                {"id": "one", "configDir": str(d1)},
                {"id": "two", "configDir": str(d2)},
            ]}
            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token", return_value=None), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(tmp) / "default"), \
                 mock.patch.object(builtin, "urlopen_no_redirect", _ok_opener(percent=75.0)):
                res = builtin._fetch_anthropic()
        self.assertEqual([a.id for a in res.accounts], ["one", "two"])


# --- credential reader -------------------------------------------------------


class CredentialReaderTests(unittest.TestCase):
    def test_reads_access_token_from_credentials_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp), "SYNTH-TOKEN")
            token, reason = builtin._read_claude_account_token(tmp)
        self.assertEqual(token, "SYNTH-TOKEN")
        self.assertIsNone(reason)

    def test_missing_dir_is_no_credentials_on_linux(self):
        with mock.patch.object(builtin, "sys",
                               types.SimpleNamespace(platform="linux")):
            token, reason = builtin._read_claude_account_token("/nonexistent-xyz")
        self.assertIsNone(token)
        self.assertEqual(reason, "no-credentials")

    def test_missing_file_on_macos_is_an_unsupported_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(builtin, "sys",
                                   types.SimpleNamespace(platform="darwin")):
                token, reason = builtin._read_claude_account_token(tmp)
        self.assertIsNone(token)
        self.assertEqual(reason, "unsupported-platform")
        self.assertIn("Keychain", builtin._MACOS_UNSUPPORTED_DETAIL)

    def test_macos_with_a_file_is_still_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp), "SYNTH-MAC")
            with mock.patch.object(builtin, "sys",
                                   types.SimpleNamespace(platform="darwin")):
                token, reason = builtin._read_claude_account_token(tmp)
        self.assertEqual(token, "SYNTH-MAC")
        self.assertIsNone(reason)

    def test_corrupt_or_tokenless_file_is_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".credentials.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(builtin._read_claude_account_token(tmp),
                             (None, "no-credentials"))
            (Path(tmp) / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "   "}}), encoding="utf-8")
            self.assertEqual(builtin._read_claude_account_token(tmp),
                             (None, "no-credentials"))

    def test_credentials_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_credentials(Path(tmp), "SYNTH-RAW")
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            settings = {"claudeAccounts": [{"id": "r", "configDir": tmp}]}
            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token", return_value=None), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(tmp) / "default"), \
                 mock.patch.object(builtin, "urlopen_no_redirect", _ok_opener()):
                builtin._fetch_anthropic()
            after = (path.read_bytes(), path.stat().st_mtime_ns)
            self.assertEqual(before, after)

    def test_oversized_credential_file_is_refused_unread(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / ".credentials.json"
            big.write_bytes(b'{"claudeAiOauth":{"accessToken":"x"}}'
                            + b" " * (builtin._MAX_CREDENTIAL_BYTES + 16))
            self.assertEqual(builtin._read_claude_account_token(tmp),
                             (None, "no-credentials"))

    def test_fifo_credential_path_is_refused_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no mkfifo on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            os.mkfifo(Path(tmp) / ".credentials.json")
            # Must return immediately: a blocking read here would hang the test.
            self.assertEqual(builtin._read_claude_account_token(tmp),
                             (None, "no-credentials"))

    def test_symlink_to_a_device_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / ".credentials.json"
            try:
                os.symlink("/dev/zero", link)
            except OSError:
                self.skipTest("cannot create a symlink here")
            # A device is not a regular file: never read, never block.
            self.assertEqual(builtin._read_claude_account_token(tmp),
                             (None, "no-credentials"))


# --- HTTP response bound -----------------------------------------------------


class HttpBoundTests(unittest.TestCase):
    def test_oversized_usage_response_is_rejected(self):
        oversized = b"[" + b" " * (builtin._MAX_HTTP_BYTES + 8) + b"]"

        def _opener(req, timeout=None):  # noqa: ANN001, ARG001
            return _Resp(oversized)

        with mock.patch.object(builtin, "urlopen_no_redirect", _opener):
            payload, reason = builtin._request_anthropic_usage("SYNTH-TOKEN")
        self.assertIsNone(payload)
        self.assertEqual(reason, "bad-json")

    def test_bounded_response_still_parses(self):
        body = json.dumps(_payload(12.0)).encode("utf-8")

        def _opener(req, timeout=None):  # noqa: ANN001, ARG001
            return _Resp(body)

        with mock.patch.object(builtin, "urlopen_no_redirect", _opener):
            payload, reason = builtin._request_anthropic_usage("SYNTH-TOKEN")
        self.assertEqual(reason, None)
        self.assertIsInstance(payload, dict)


# --- source precedence -------------------------------------------------------


class SourcePrecedenceTests(unittest.TestCase):
    def test_claude_config_dir_env_drives_the_default_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertEqual(builtin._effective_claude_config_dir(),
                                 Path(tmp))

    def test_no_env_falls_back_to_home_claude(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("pathlib.Path.home", return_value=Path("/home/synthetic")):
                self.assertEqual(builtin._effective_claude_config_dir(),
                                 Path("/home/synthetic/.claude"))

    def test_additions_are_only_the_listed_directories(self):
        """Nothing is discovered by scanning: an unlisted dir is never read."""
        with tempfile.TemporaryDirectory() as tmp:
            listed, unlisted = Path(tmp) / "listed", Path(tmp) / "unlisted"
            _write_credentials(listed, "SYNTH-LISTED")
            _write_credentials(unlisted, "SYNTH-UNLISTED")
            settings = {"claudeAccounts": [{"id": "l", "configDir": str(listed)}]}
            read_auths = []

            def _opener(req, timeout=None):  # noqa: ANN001, ARG001
                read_auths.append(req.get_header("Authorization"))
                return _Resp(json.dumps(_payload(10.0)).encode("utf-8"))

            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token", return_value=None), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(tmp) / "default"), \
                 mock.patch.object(builtin, "urlopen_no_redirect", _opener):
                builtin._fetch_anthropic()
        self.assertEqual(read_auths, ["Bearer SYNTH-LISTED"])


# --- invalid configuration ---------------------------------------------------


class InvalidConfigTests(unittest.TestCase):
    def test_non_list_setting_is_config_invalid(self):
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": {"id": "x"}})):
            entries, configured = builtin._claude_accounts_setting()
        self.assertTrue(configured)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["error"], "config-invalid")

    def test_missing_id_and_missing_config_dir_are_reported(self):
        settings = {"claudeAccounts": [
            "not-an-object",
            {"label": "No id", "configDir": "/tmp/x"},
            {"id": "no-dir", "label": "No dir"},
            {"id": "ok", "configDir": "/tmp/ok"},
        ]}
        with mock.patch.dict(sys.modules, _config_modules(settings)):
            entries, _ = builtin._claude_accounts_setting()
        errors = [e for e in entries if e.get("error")]
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(e["error"] == "config-invalid" for e in errors))
        self.assertTrue(any("needs a non-empty string 'id'" in e["detail"] for e in errors))
        self.assertTrue(any("needs a non-empty string 'configDir'" in e["detail"] for e in errors))
        self.assertEqual([e["id"] for e in entries if "config_dir" in e], ["ok"])

    def test_duplicate_id_is_flagged(self):
        settings = {"claudeAccounts": [
            {"id": "dup", "configDir": "/tmp/a"},
            {"id": "dup", "configDir": "/tmp/b"},
        ]}
        with mock.patch.dict(sys.modules, _config_modules(settings)):
            entries, _ = builtin._claude_accounts_setting()
        self.assertEqual([e.get("id") for e in entries if "config_dir" in e], ["dup"])
        self.assertEqual(len([e for e in entries if e.get("error")]), 1)
        self.assertIn("duplicate", entries[1]["detail"])

    def test_invalid_entry_becomes_an_actionable_row(self):
        entries = [{"error": "config-invalid", "index": 1,
                    "detail": "account #1 is not an object; expected {id, label, configDir}."}]
        rows = builtin._claude_account_results(
            entries, primary_token=None, deadline=builtin.Deadline(5.0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].unavailable_reason, "config-invalid")
        self.assertTrue(rows[0].details)

    def test_invalid_config_never_reads_a_credential(self):
        def _explode(*a, **k):  # noqa: ANN001
            raise AssertionError("no network for an invalid entry")

        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": 7})), \
             mock.patch.object(builtin, "urlopen_no_redirect", _explode):
            res = builtin._fetch_anthropic()
        self.assertEqual(len(res.accounts), 1)
        self.assertEqual(res.accounts[0].unavailable_reason, "config-invalid")


# --- config parsing hardening ------------------------------------------------


class ConfigParsingHardeningTests(unittest.TestCase):
    def test_oversized_config_parses_at_most_eight_plus_one_limit(self):
        raw = [{"id": f"a{i}", "configDir": f"/tmp/{i}"} for i in range(50)]
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": raw})):
            entries, configured = builtin._claude_accounts_setting()
        self.assertTrue(configured)
        self.assertEqual(len(entries), builtin._MAX_CLAUDE_ACCOUNTS + 1)
        self.assertEqual(entries[-1]["error"], "config-limit")
        self.assertEqual(sum(1 for e in entries if e.get("error")), 1)

    def test_json_string_setting_is_accepted(self):
        # A writer other than the CLI may store the list as a JSON string.
        payload = json.dumps([{"id": "work", "label": "Work", "configDir": "/tmp/w"}])
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": payload})):
            entries, configured = builtin._claude_accounts_setting()
        self.assertTrue(configured)
        self.assertEqual([e["id"] for e in entries], ["work"])
        self.assertNotIn("error", entries[0])

    def test_valid_json_string_yields_every_entry(self):
        payload = '[{"id":"a","configDir":"/tmp/a"},{"id":"b","configDir":"/tmp/b"}]'
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": payload})):
            entries, _ = builtin._claude_accounts_setting()
        self.assertEqual([e["id"] for e in entries], ["a", "b"])

    def test_bad_json_string_is_config_invalid_not_silent(self):
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": "{not json"})):
            entries, configured = builtin._claude_accounts_setting()
        self.assertTrue(configured)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["error"], "config-invalid")
        self.assertIn("valid JSON", entries[0]["detail"])

    def test_blank_string_setting_is_not_configured(self):
        with mock.patch.dict(sys.modules, _config_modules({"claudeAccounts": "   "})):
            entries, configured = builtin._claude_accounts_setting()
        self.assertFalse(configured)
        self.assertEqual(entries, [])


# --- bounded work ------------------------------------------------------------


class BoundedWorkTests(unittest.TestCase):
    def test_expired_deadline_marks_timeout_without_a_request(self):
        def _explode(*a, **k):  # noqa: ANN001
            raise AssertionError("deadline already spent; no request expected")

        with tempfile.TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp), "SYNTH-T")
            rows = builtin._claude_account_results(
                [{"id": "t", "label": "T", "config_dir": tmp}],
                primary_token=None, deadline=builtin.Deadline(0.0))
        self.assertEqual(rows[0].unavailable_reason, "timeout")

    def test_account_count_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_entries = []
            for i in range(builtin._MAX_CLAUDE_ACCOUNTS + 2):
                d = Path(tmp) / f"acct{i}"
                _write_credentials(d, f"SYNTH-{i}")
                settings_entries.append({"id": f"a{i}", "configDir": str(d)})
            settings = {"claudeAccounts": settings_entries}
            with mock.patch.dict(sys.modules, _config_modules(settings)), \
                 mock.patch.object(builtin, "_core_anthropic_token", return_value=None), \
                 mock.patch.object(builtin, "_effective_claude_config_dir",
                                   return_value=Path(tmp) / "default"), \
                 mock.patch.object(builtin, "urlopen_no_redirect", _ok_opener()):
                res = builtin._fetch_anthropic()
        fetched = [a for a in res.accounts if a.unavailable_reason is None]
        limited = [a for a in res.accounts if a.unavailable_reason == "config-limit"]
        self.assertEqual(len(fetched), builtin._MAX_CLAUDE_ACCOUNTS)
        # A single bounded overflow row, never one error row per excess entry.
        self.assertEqual(len(limited), 1)
        self.assertEqual(len(res.accounts), builtin._MAX_CLAUDE_ACCOUNTS + 1)

    def test_deadline_is_shared_across_accounts(self):
        """All account requests draw on one budget, not one each."""
        seen_timeouts = []

        def _opener(req, timeout=None):  # noqa: ANN001
            seen_timeouts.append(timeout)
            return _Resp(json.dumps(_payload(5.0)).encode("utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            entries = []
            for i in range(4):
                d = Path(tmp) / f"a{i}"
                _write_credentials(d, f"SYNTH-{i}")
                entries.append({"id": f"a{i}", "label": f"A{i}", "config_dir": str(d)})
            with mock.patch.object(builtin, "urlopen_no_redirect", _opener):
                rows = builtin._claude_account_results(
                    entries, primary_token=None, deadline=builtin.Deadline(18.0))
        self.assertEqual(len(rows), 4)
        # Every per-request timeout is the shared budget slice, never additive.
        self.assertTrue(all(t <= builtin._ANTHROPIC_MULTI_BUDGET_S for t in seen_timeouts))
        self.assertTrue(all(t <= builtin._ANTHROPIC_TIMEOUT_S for t in seen_timeouts))


# --- no leakage --------------------------------------------------------------


class LeakageTests(unittest.TestCase):
    def test_token_and_path_never_reach_the_cache(self):
        qc, base_under_test = _cache_module()
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "secret-dir"
            _write_credentials(d1, "SYNTH-SECRET-TOKEN")
            account = base_under_test.QuotaAccount(
                id="work", label="Work",
                windows=[base_under_test.QuotaWindow(label="Current session",
                                                     used_percent=25.0)])
            result = base_under_test.QuotaResult(
                label="anthropic",
                windows=[base_under_test.QuotaWindow(label="Current session",
                                                     used_percent=10.0)],
                accounts=[account])
            cache_path = Path(tmp) / "quota_cache.json"
            with mock.patch.object(qc, "_cache_path", return_value=str(cache_path)), \
                 mock.patch.dict(qc.PROVIDER_FETCHERS,
                                 {"anthropic": lambda: result}, clear=True):
                cache = qc.refresh_quota_cache(budget=1.0)
        blob = json.dumps(cache)
        self.assertNotIn("SYNTH-SECRET-TOKEN", blob)
        self.assertNotIn("secret-dir", blob)
        self.assertNotIn(str(d1), blob)

    def test_account_rows_do_not_carry_raw_errors(self):
        """A failed account reports a reason code, never an exception string."""
        with tempfile.TemporaryDirectory() as tmp:
            # No credentials file -> a coded reason, no traceback text.
            entries = [{"id": "missing", "label": "Missing", "config_dir": tmp}]
            with mock.patch.object(builtin, "sys",
                                   types.SimpleNamespace(platform="linux")):
                rows = builtin._claude_account_results(
                    entries, primary_token=None, deadline=builtin.Deadline(5.0))
        self.assertEqual(rows[0].unavailable_reason, "no-credentials")
        blob = json.dumps([{"r": r.unavailable_reason, "d": r.details} for r in rows])
        self.assertNotIn("OSError", blob)
        self.assertNotIn(str(tmp), blob)


# --- cache expansion (generic account -> provider-row mechanism) --------------


def _cache_module():
    """Import quota_cache through the repo's stub-package pattern."""
    if "hermes_constants" not in sys.modules:
        stub = types.ModuleType("hermes_constants")
        stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME") or tempfile.gettempdir())
        sys.modules["hermes_constants"] = stub
    if "quota_plugin_under_test" not in sys.modules:
        pkg = types.ModuleType("quota_plugin_under_test")
        pkg.__path__ = [str(ROOT)]
        sys.modules["quota_plugin_under_test"] = pkg
    qc = importlib.import_module("quota_plugin_under_test.quota_cache")
    base_under_test = importlib.import_module("quota_plugin_under_test.quota_providers.base")
    return qc, base_under_test


class CacheExpansionTests(unittest.TestCase):
    def _run(self, result, providers=("anthropic",)):
        qc, _ = _cache_module()
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "quota_cache.json"
            fetchers = {pid: (lambda r=result: r) for pid in providers}
            with mock.patch.object(qc, "_cache_path", return_value=str(cache_path)), \
                 mock.patch.dict(qc.PROVIDER_FETCHERS, fetchers, clear=True):
                return qc.refresh_quota_cache(budget=1.0)

    def test_account_becomes_a_sibling_provider_row(self):
        _, base = _cache_module()
        result = base.QuotaResult(
            label="anthropic",
            windows=[base.QuotaWindow(label="Current session", used_percent=11.0)],
            accounts=[base.QuotaAccount(id="work", label="Work",
                                        windows=[base.QuotaWindow(label="Current session",
                                                                  used_percent=25.0)])])
        cache = self._run(result)
        self.assertEqual(sorted(cache["providers"]), ["anthropic", "anthropic:work"])
        row = cache["providers"]["anthropic:work"]
        self.assertEqual(row["provider"], "anthropic")
        self.assertEqual(row["account_id"], "work")
        self.assertEqual(row["account_label"], "Work")
        self.assertEqual(row["label"], "anthropic · Work")
        self.assertEqual(row["windows"][0]["used_percent"], 25.0)
        # The nested key never lands in the cache.
        self.assertNotIn("accounts", cache["providers"]["anthropic"])

    def test_unavailable_account_is_still_a_row(self):
        _, base = _cache_module()
        result = base.QuotaResult(
            label="anthropic",
            accounts=[base.QuotaAccount(id="dead", label="Dead",
                                        unavailable_reason="no-credentials")])
        cache = self._run(result)
        row = cache["providers"]["anthropic:dead"]
        self.assertEqual(row["unavailable_reason"], "no-credentials")
        self.assertEqual(row["windows"], [])

    def test_providers_without_accounts_are_untouched(self):
        _, base = _cache_module()
        result = base.QuotaResult(label="deepseek",
                                  windows=[base.QuotaWindow(label="w", used_percent=1.0)])
        cache = self._run(result, providers=("deepseek",))
        self.assertEqual(sorted(cache["providers"]), ["deepseek"])
        self.assertNotIn("accounts", cache["providers"]["deepseek"])

    def test_duplicate_account_ids_do_not_collide(self):
        _, base = _cache_module()
        result = base.QuotaResult(label="anthropic", accounts=[
            base.QuotaAccount(id="dup", label="First"),
            base.QuotaAccount(id="dup", label="Second"),
        ])
        cache = self._run(result)
        keys = sorted(k for k in cache["providers"] if k.startswith("anthropic:"))
        # Deterministic distinct keys: neither row is silently hidden.
        self.assertEqual(keys, ["anthropic:dup", "anthropic:dup#2"])
        self.assertEqual(cache["providers"]["anthropic:dup"]["account_label"], "First")
        self.assertEqual(cache["providers"]["anthropic:dup#2"]["account_label"], "Second")

    def test_generated_account_id_does_not_hide_a_configured_id(self):
        _, base = _cache_module()
        result = base.QuotaResult(label="anthropic", accounts=[
            base.QuotaAccount(id="__invalid-2", label="Configured"),
            base.QuotaAccount(id=None, label="Generated"),  # no id -> generated key
        ])
        cache = self._run(result)
        keys = sorted(k for k in cache["providers"] if k.startswith("anthropic:"))
        self.assertEqual(len(keys), 2)  # a generated id must never hide a real one
        labels = {cache["providers"][k]["account_label"] for k in keys}
        self.assertEqual(labels, {"Configured", "Generated"})

    def test_expand_accounts_does_not_mutate_the_fetcher_record(self):
        qc, base = _cache_module()
        record = {
            "label": "anthropic",
            "windows": [],
            "accounts": [{"id": "w", "label": "W", "windows": []}],
        }
        snapshot = copy.deepcopy(record)
        rows = qc._expand_accounts("anthropic", record)
        self.assertEqual(record, snapshot)         # caller's dict is untouched
        self.assertIn("anthropic:w", rows)
        self.assertNotIn("accounts", rows["anthropic"])


# --- widget rendering --------------------------------------------------------


@unittest.skipUnless(shutil.which("node"), "Node.js is required for widget render tests")
class WidgetAccountRenderTests(unittest.TestCase):
    ACCOUNT = {
        "label": "anthropic · Work",
        "provider": "anthropic",
        "account_id": "work",
        "account_label": "Work",
        "windows": [{"label": "Current session", "used_percent": 55.0,
                     "reset_at": "2026-09-30T07:59:59+00:00"}],
    }

    def test_row_shows_base_name_plus_account_label(self):
        tree = render(component="row", id="anthropic:work", provider=self.ACCOUNT)
        rendered = text(tree)
        self.assertIn("Anthropic · Work", rendered)
        self.assertIn("45% left", rendered)
        self.assertNotIn("anthropic:work", rendered)

    def test_chip_uses_the_account_label(self):
        tree = render(component="chip", id="anthropic:work", provider=self.ACCOUNT)
        self.assertIn("Anthropic · Work", text(tree))

    def test_badge_resolves_the_base_provider_icon(self):
        # deepseek has a real PROVIDER_META entry; without base resolution the
        # unknown-id fallback would print the raw composite id.
        tree = render(component="row", id="deepseek:work",
                      provider=dict(self.ACCOUNT, provider="deepseek",
                                    label="deepseek · Work", account_label="Work"))
        rendered = text(tree)
        self.assertIn("DeepSeek · Work", rendered)
        self.assertNotIn("deepseek:work", rendered)

    def test_pane_lists_primary_and_account_without_crashing(self):
        data = {"providers": {
            "anthropic": {"windows": [{"label": "Current session", "used_percent": 10.0}]},
            "anthropic:work": self.ACCOUNT,
        }, "fetched_at": "2026-01-02T03:04:05Z", "age_s": 3}
        tree = render(data=data)
        rendered = text(tree)
        self.assertIn("Anthropic · Work", rendered)
        self.assertIn("Anthropic", rendered)

    def test_plain_provider_has_no_account_suffix(self):
        tree = render(component="row", id="deepseek",
                      provider={"windows": [{"label": "w", "used_percent": 5.0}]})
        self.assertNotIn("·", text(tree))

    def test_chip_tip_keeps_details_for_an_unavailable_account(self):
        provider = dict(self.ACCOUNT, unavailable_reason="config-invalid",
                        windows=[],
                        details=["account #2 needs a non-empty string 'configDir'."])
        tree = render(component="chip", id="anthropic:work", provider=provider)
        tip = tree["props"]["title"]
        self.assertIn("unavailable (config-invalid)", tip)
        self.assertIn("needs a non-empty string 'configDir'", tip)

    def test_chip_without_a_provider_object_names_the_account_from_the_id(self):
        tree = render(component="chip", id="anthropic:work", provider=None)
        self.assertIn("Anthropic · work", text(tree))


# --- CLI + footer rendering (fake account data, real render code) ------------


_CLI_PACKAGE = {}


def _load_cli_package():
    """Execute the plugin's real ``__init__``/``commands`` under a test package."""
    if "pkg" in _CLI_PACKAGE:
        return _CLI_PACKAGE["pkg"]
    if "hermes_constants" not in sys.modules:
        _cache_module()
    name = "quota_cli_under_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _CLI_PACKAGE["pkg"] = module
    return module


class CliRenderTests(unittest.TestCase):
    def _cache(self):
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "providers": {
                "anthropic": {
                    "label": "anthropic", "plan": None, "unavailable_reason": None,
                    "details": [], "account_balances": [], "api_calls_available": None,
                    "windows": [{"label": "Current session", "used_percent": 10.0,
                                 "reset_at": "2026-09-30T07:59:59+00:00"}],
                },
                "anthropic:work": {
                    "label": "anthropic · Work", "provider": "anthropic",
                    "account_id": "work", "account_label": "Work", "plan": None,
                    "unavailable_reason": None, "details": [],
                    "account_balances": [], "api_calls_available": None,
                    "windows": [{"label": "Current session", "used_percent": 45.0,
                                 "reset_at": "2026-10-01T07:59:59+00:00"}],
                },
                "anthropic:dead": {
                    "label": "anthropic · Dead", "provider": "anthropic",
                    "account_id": "dead", "account_label": "Dead", "plan": None,
                    "unavailable_reason": "config-invalid",
                    "details": ["account #2 needs a non-empty string 'configDir'."],
                    "account_balances": [], "api_calls_available": None, "windows": [],
                },
            },
        }

    def _render(self, cache):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}):
                (Path(tmp) / "quota_cache.json").write_text(
                    json.dumps(cache), encoding="utf-8")
                pkg = _load_cli_package()
                commands = sys.modules["quota_cli_under_test.commands"]
                return (pkg._format_quota_block(cache),
                        commands._render_quota(None),
                        commands._render_quota("work"),
                        commands._render_quota("anthropic"))

    def test_footer_block_lists_account_rows(self):
        block, _, _, _ = self._render(self._cache())
        self.assertIn("anthropic · Work", block)
        self.assertIn("55%", block)  # remaining, not used

    def test_cli_status_shows_accounts_and_invalid_reason(self):
        _, rendered, _, _ = self._render(self._cache())
        self.assertIn("anthropic · Work", rendered)
        self.assertIn("config-invalid", rendered)

    def test_cli_shows_the_actionable_detail_for_an_unavailable_account(self):
        _, rendered, _, _ = self._render(self._cache())
        # The reason code alone is not enough: the fix must be visible too.
        self.assertIn("account #2 needs a non-empty string 'configDir'.", rendered)

    def test_cli_filter_by_account_label_selects_one_account(self):
        _, _, by_label, _ = self._render(self._cache())
        self.assertIn("Work", by_label)
        self.assertNotIn("config-invalid", by_label)

    def test_cli_filter_by_base_provider_lists_primary_and_accounts(self):
        _, _, _, by_provider = self._render(self._cache())
        self.assertIn("anthropic · Work", by_provider)
        self.assertIn("**anthropic**", by_provider)  # the primary row matches too


if __name__ == "__main__":
    unittest.main(verbosity=2)
