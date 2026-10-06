"""Synthetic-file regression tests: never import writer-capable auth readers."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from test_codex_accounts import token
from quota_providers import codex


class ReadonlyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name) / 'profile'
        self.root = Path(self.temp.name) / 'root'
        self.local.mkdir()
        self.root.mkdir()
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: self.local
        constants.get_default_hermes_root = lambda: self.root
        auth = types.ModuleType('hermes_cli.auth')
        auth.read_credential_pool = mock.Mock(side_effect=AssertionError('writer-capable reader'))
        auth.get_provider_auth_state = mock.Mock(side_effect=AssertionError('writer-capable reader'))
        self.auth = auth
        self.patch = mock.patch.dict('sys.modules', {'hermes_constants': constants, 'hermes_cli.auth': auth})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_discovery_spending_budget_never_starts_http(self):
        clock = [0.0]
        def slow():
            clock[0] = 16.0
            return [(token('slow'), 'slow', False, '', None, True)]
        with mock.patch.object(codex, '_credentials', side_effect=slow), mock.patch.object(codex.time, 'monotonic', side_effect=lambda: clock[0]), mock.patch.object(codex, '_fetch_account') as http:
            result = codex.fetch_codex_quota(mock.Mock())
        self.assertEqual(result.unavailable_reason, 'timeout')
        http.assert_not_called()

    def test_large_valid_store_and_response_keep_upstream_behavior(self):
        from test_codex_accounts import auth_modules, mock_http, payload
        from quota_providers import builtin
        from io import BytesIO
        import sys
        modules = auth_modules([{'access_token': token('synthetic-a')}])
        home = modules['hermes_constants'].get_hermes_home()
        data = json.loads((home / 'auth.json').read_text())
        data['unrelated'] = 'x' * (1024 * 1024)
        (home / 'auth.json').write_text(json.dumps(data))
        response = {**payload(21), 'unrelated': 'x' * (1024 * 1024)}
        with mock.patch.dict(sys.modules, modules), mock_http(return_value=BytesIO(json.dumps(response).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.windows[0].used_percent, 21)

    def test_provider_level_pool_fallback_and_legacy_precedence(self):
        def pool(name):
            return {'credential_pool': {'openai-codex': [{'access_token': token(name)}]}}
        def legacy(name):
            return {'providers': {'openai-codex': {'tokens': {'access_token': token(name)}}}}
        cases = [
            (pool('local'), pool('global'), 'local'),
            ({}, pool('global'), 'global'),
            ({'credential_pool': {'openai-codex': [], 'other': [1]}}, pool('global'), 'global'),
            ({'credential_pool': {'openai-codex': 'invalid'}}, pool('global'), 'global'),
            (legacy('local'), pool('global'), 'global'),
            (legacy('local'), legacy('global'), 'local'),
            ({}, legacy('global'), 'global'),
            ({'providers': {'openai-codex': {'access_token': token('flat')}}}, {}, 'flat'),
            ({'credential_pool': {'openai-codex': [{}]}}, pool('global'), None),
            ({'providers': {'openai-codex': {}}}, legacy('global'), None),
        ]
        for local, root, expected in cases:
            with self.subTest(expected=expected, local=local):
                self.save(self.local, local)
                self.save(self.root, root)
                rows = codex._credentials()
                self.assertEqual([r[1] for r in rows], [expected] if expected else [])
        self.auth.read_credential_pool.assert_not_called()
        self.auth.get_provider_auth_state.assert_not_called()

    def test_absent_local_uses_global_and_corrupt_global_is_ignored(self):
        self.save(self.root, {'credential_pool': {'openai-codex': [{'access_token': token('global')}]}})
        self.assertEqual(codex._credentials()[0][1], 'global')
        self.save(self.local, {'providers': {'openai-codex': {'access_token': token('local')}}})
        (self.root / 'auth.json').write_bytes(b'{invalid')
        self.assertEqual(codex._credentials()[0][1], 'local')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['auth.json'])

    def test_nonobject_and_invalid_utf8_local_fail_closed(self):
        for body in (b'[]', b'\xff'):
            with self.subTest(size=len(body)):
                (self.local / 'auth.json').write_bytes(body)
                with mock.patch.object(codex, '_fetch_account') as http:
                    result = codex.fetch_codex_quota(mock.Mock())
                self.assertEqual(result.unavailable_reason, 'fetcher-unavailable')
                http.assert_not_called()
                self.assertEqual((self.local / 'auth.json').read_bytes(), body)
                self.assertEqual(sorted(p.name for p in self.local.iterdir()), ['auth.json'])

    def test_discovery_inherits_context_local_profile(self):
        from contextvars import ContextVar
        home = ContextVar('synthetic_home', default=self.root)
        import sys
        sys.modules['hermes_constants'].get_hermes_home = home.get
        self.save(self.local, {'credential_pool': {'openai-codex': [{'access_token': token('local', exp=1)}]}})
        handle = home.set(self.local)
        try:
            from test_codex_accounts import mock_http
            with mock_http() as http:
                result = codex.fetch_codex_quota(mock.Mock())
            http.assert_not_called()
            self.assertEqual(result.unavailable_reason, 'reauth-required')
        finally:
            home.reset(handle)

    def save(self, home, data):
        (home / 'auth.json').write_text(json.dumps(data))

    def test_corrupt_local_fails_closed_without_backup_or_write(self):
        path = self.local / 'auth.json'
        path.write_bytes(b'{invalid')
        self.save(self.root, {'credential_pool': {'openai-codex': [{'access_token': token('global')}]}})
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(self.temp.name).rglob('*') if p.is_file()}
        with self.assertRaises((ValueError, UnicodeError)):
            codex._credentials()
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(self.temp.name).rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.auth.read_credential_pool.assert_not_called()
        self.auth.get_provider_auth_state.assert_not_called()
