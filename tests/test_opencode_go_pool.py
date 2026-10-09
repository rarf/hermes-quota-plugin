from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    if "quota_plugin_opencode_go" in sys.modules:
        return sys.modules["quota_plugin_opencode_go"]
    from quota_providers import opencode_go

    return opencode_go


def write_auth(entries):
    """A temp hermes home whose auth.json holds the given pool entries."""
    tmp = tempfile.mkdtemp(prefix="quota-auth-")
    (Path(tmp) / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {"opencode-go": entries}})
    )
    return tmp


class PoolEntriesTests(unittest.TestCase):
    def test_reads_pool_from_auth_json(self):
        mod = load_module()
        home = write_auth(
            [
                {"id": "63e632", "label": "pasei@pasei", "access_token": " tokA ", "priority": 0},
                {"id": "c1ec3d", "label": "pasei2", "access_token": "tokB", "priority": 1},
            ]
        )
        with mock.patch.dict(sys.modules, {"hermes_constants": mock.MagicMock(get_hermes_home=lambda: Path(home))}):
            self.assertEqual(
                mod._pool_entries(),
                [("tokA", "pasei"), ("tokB", "pasei2")],
            )

    def test_dedupes_and_blank_tokens(self):
        mod = load_module()
        home = write_auth(
            [
                {"id": "a", "access_token": "tokA"},
                {"id": "b", "access_token": "tokA"},
                {"id": "c", "access_token": "   "},
                {"id": "d", "access_token": "tokC"},
            ]
        )
        with mock.patch.dict(sys.modules, {"hermes_constants": mock.MagicMock(get_hermes_home=lambda: Path(home))}):
            self.assertEqual(mod._pool_entries(), [("tokA", "a"), ("tokC", "d")])

    def test_global_root_pool_fallback_and_numbered_env_keys(self):
        mod = load_module()
        local = Path(tempfile.mkdtemp(prefix="quota-local-"))
        root = Path(write_auth([{"id": "root", "access_token": "root-token"}]))
        (local / "auth.json").write_text(json.dumps({"credential_pool": {"opencode-go": []}}))
        constants = mock.MagicMock(
            get_hermes_home=lambda: local,
            get_default_hermes_root=lambda: root,
        )
        with mock.patch.dict(sys.modules, {"hermes_constants": constants}), mock.patch.dict(
            "os.environ", {"OPENCODE_GO_API_KEY_2": "sibling-token"}, clear=True
        ):
            self.assertEqual(mod._pool_entries(), [("root-token", "root"), ("sibling-token", "chave 2")])

    def test_no_pool_falls_back_to_single_key(self):
        mod = load_module()
        home = write_auth([])
        with mock.patch.dict(sys.modules, {"hermes_constants": mock.MagicMock(get_hermes_home=lambda: Path(home))}), \
             mock.patch.object(mod, "resolve_api_key", return_value="envkey"):
            self.assertEqual(mod._pool_entries(), [("envkey", "key1")])

    def test_unreadable_home_falls_back(self):
        mod = load_module()
        with mock.patch.dict(sys.modules, {"hermes_constants": mock.MagicMock(get_hermes_home=lambda: Path("/nonexistent/quota-test-home"))}), \
             mock.patch.object(mod, "resolve_api_key", return_value=None):
            self.assertEqual(mod._pool_entries(), [])


class FetchAggregationTests(unittest.TestCase):
    def _result(self, windows, plan=None, reason=None):
        from quota_providers.base import QuotaResult

        return QuotaResult(label="opencode-go", windows=windows, plan=plan, unavailable_reason=reason)

    def test_windows_get_key_labels(self):
        mod = load_module()
        from quota_providers.base import QuotaWindow

        entries = [("tokA", "pasei"), ("tokB", "pasei2")]
        results = {
            "tokA": self._result([QuotaWindow(label="Monthly", used_percent=10)]),
            "tokB": self._result([], plan=None, reason="rate-limited"),
        }
        with mock.patch.object(mod, "_pool_entries", return_value=entries), \
             mock.patch.object(mod, "fetch_usage", side_effect=lambda tok, **kw: results[tok]):
            res = mod.fetch_opencode_go_quota()
        self.assertEqual(res.unavailable_reason, None)
        self.assertEqual([w.label for w in res.windows], ["Monthly · pasei"])
        self.assertEqual(res.details, ["pasei2: rate-limited"])

    def test_all_keys_fail(self):
        mod = load_module()
        with mock.patch.object(mod, "_pool_entries", return_value=[("tokA", "k1")]), \
             mock.patch.object(mod, "fetch_usage", return_value=self._result([], reason="no-data")):
            res = mod.fetch_opencode_go_quota()
        self.assertEqual(res.unavailable_reason, "no-data")

    def test_all_keys_share_one_deadline(self):
        mod = load_module()
        seen = []
        with mock.patch.object(mod, "_pool_entries", return_value=[("a", "k1"), ("b", "k2")]), \
             mock.patch.object(mod, "fetch_usage", side_effect=lambda token, **kw: seen.append(kw["deadline"]) or self._result([], reason="rate-limited")):
            mod.fetch_opencode_go_quota()
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])

    def test_no_entries(self):
        mod = load_module()
        with mock.patch.object(mod, "_pool_entries", return_value=[]):
            res = mod.fetch_opencode_go_quota()
        self.assertEqual(res.unavailable_reason, "no-credentials")


if __name__ == "__main__":
    unittest.main()
