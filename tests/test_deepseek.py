"""Offline DeepSeek account balance contracts, using synthetic data only."""
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class DeepSeekTests(unittest.TestCase):
    def module(self):
        import quota_providers
        self.assertIn('deepseek', quota_providers.PROVIDER_FETCHERS)
        return importlib.import_module('quota_providers.deepseek')

    def payload(self):
        return {'is_available': True, 'balance_infos': [
            {'currency': 'USD', 'total_balance': '12.50', 'granted_balance': '2.50', 'topped_up_balance': '10.00'},
            {'currency': 'CNY', 'total_balance': '25.125', 'granted_balance': '5', 'topped_up_balance': '20.125'},
        ]}

    def fetch(self, payload):
        mod = self.module()
        with mock.patch.object(mod, 'resolve_api_key', return_value='SYNTHETIC_SECRET'), mock.patch.object(mod, 'get_json', return_value=(payload, None)) as get:
            result = mod.fetch_deepseek_quota()
        self.assertEqual(get.call_args.args, ('https://api.deepseek.com/user/balance', 'SYNTHETIC_SECRET'))
        self.assertNotIn('SYNTHETIC_SECRET', repr(result))
        return result

    def test_registered_without_replacing_builtin_openrouter(self):
        self.module()
        from quota_providers import PROVIDER_FETCHERS
        self.assertEqual(PROVIDER_FETCHERS['openrouter'].__module__, 'quota_providers.builtin')
        self.assertIn('cursor', PROVIDER_FETCHERS)

    def test_account_balances_preserve_currency_and_precision(self):
        result = self.fetch(self.payload())
        self.assertIsNone(result.unavailable_reason)
        self.assertIsNone(result.plan)
        self.assertEqual(result.windows, [])
        self.assertIs(result.api_calls_available, True)
        self.assertEqual([(b.currency, b.total_balance) for b in result.account_balances], [('USD', '12.50'), ('CNY', '25.125')])
        self.assertIn('USD total: 12.50; granted: 2.50; topped-up: 10.00', result.details)
        self.assertIn('CNY total: 25.125; granted: 5; topped-up: 20.125', result.details)

    def test_zero_balance_is_data_not_failure(self):
        payload = {'is_available': False, 'balance_infos': [
            {'currency': 'USD', 'total_balance': '0', 'granted_balance': '0', 'topped_up_balance': '0'}]}
        result = self.fetch(payload)
        self.assertTrue(result.has_data())
        self.assertIs(result.api_calls_available, False)
        self.assertIn('API calls available: no', result.details)

    def test_missing_credentials_never_fetch(self):
        mod = self.module()
        with mock.patch.object(mod, 'resolve_api_key', return_value=None), mock.patch.object(mod, 'get_json') as get:
            self.assertEqual(mod.fetch_deepseek_quota().unavailable_reason, 'no-credentials')
            get.assert_not_called()

    def test_malformed_responses_fail_open(self):
        for payload in ([], {}, {'is_available': 'true', 'balance_infos': []}, {'is_available': True, 'balance_infos': []}):
            with self.subTest(payload=payload):
                self.assertEqual(self.fetch(payload).unavailable_reason, 'parse-pending')
        for field, value in [('currency', 'EUR'), ('total_balance', 'NaN'), ('granted_balance', True), ('topped_up_balance', None)]:
            payload = self.payload()
            payload['balance_infos'][0][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(self.fetch(payload).unavailable_reason, 'parse-pending')
        payload = self.payload()
        payload['balance_infos'].append(payload['balance_infos'][0])
        self.assertEqual(self.fetch(payload).unavailable_reason, 'parse-pending')

    def test_errors_are_sanitized(self):
        mod = self.module()
        with mock.patch.object(mod, 'resolve_api_key', side_effect=RuntimeError('SECRET')):
            result = mod.fetch_deepseek_quota()
        self.assertEqual(result.unavailable_reason, 'fetch-error')
        self.assertNotIn('SECRET', repr(result))
        for reason in ('auth-failed', 'http-429', 'timeout', 'bad-json'):
            with mock.patch.object(mod, 'resolve_api_key', return_value='SECRET'), mock.patch.object(mod, 'get_json', return_value=(None, reason)):
                self.assertEqual(mod.fetch_deepseek_quota().unavailable_reason, reason)

    def test_native_api_helper_only_no_pool_or_runtime_selection(self):
        self.module()
        from quota_providers import api_keys
        auth = types.ModuleType('hermes_cli.auth')
        auth.resolve_api_key_provider_credentials = mock.Mock(return_value={'api_key': ' SYNTHETIC_KEY '})
        parent = types.ModuleType('hermes_cli')
        parent.auth = auth
        with mock.patch.dict(sys.modules, {'hermes_cli': parent, 'hermes_cli.auth': auth}):
            self.assertEqual(api_keys.resolve_api_key('deepseek'), 'SYNTHETIC_KEY')
            auth.resolve_api_key_provider_credentials.assert_called_once_with('deepseek')
            for key in (None, '', '  ', True):
                auth.resolve_api_key_provider_credentials.return_value = {'api_key': key}
                self.assertIsNone(api_keys.resolve_api_key('deepseek'))

    def test_http_success_bounds_and_failures(self):
        self.module()
        from quota_providers import api_keys
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(self.payload()).encode()
        with mock.patch.object(api_keys, 'urlopen', return_value=response) as opener:
            self.assertEqual(api_keys.get_json('https://api.deepseek.com/user/balance', 'SECRET'), (self.payload(), None))
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer SECRET')
        self.assertLessEqual(opener.call_args.kwargs['timeout'], 15)
        for exc, reason in [(HTTPError('https://test', 401, 'SECRET', {}, None), 'auth-failed'), (HTTPError('https://test', 429, 'SECRET', {}, None), 'http-429'), (TimeoutError('SECRET'), 'timeout'), (RuntimeError('SECRET'), 'fetch-error')]:
            with self.subTest(reason=reason), mock.patch.object(api_keys, 'urlopen', side_effect=exc):
                self.assertEqual(api_keys.get_json('https://api.deepseek.com/user/balance', 'SECRET'), (None, reason))
        for body, reason in [(b'not-json SECRET', 'bad-json'), (b'x' * (1024 * 1024 + 1), 'response-too-large')]:
            response.__enter__.return_value.read.return_value = body
            with mock.patch.object(api_keys, 'urlopen', return_value=response):
                self.assertEqual(api_keys.get_json('https://api.deepseek.com/user/balance', 'SECRET'), (None, reason))
        self.assertIsNone(api_keys._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere'))

    def test_decimal_validation(self):
        self.module()
        from quota_providers import api_keys
        for value in (True, 'NaN', 'Infinity', [], '1e999999', '1e-999999', '0e-999999', '1e9999999999', '9' * 65):
            self.assertIsNone(api_keys.amount(value))
        self.assertEqual(str(api_keys.amount('12.5000')), '12.5000')
        self.assertEqual(str(api_keys.amount('-1.25')), '-1.25')

    def test_cli_renders_details_without_windows(self):
        pkg = types.ModuleType('quota_balance_tests')
        pkg.__path__ = [str(ROOT)]
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: ROOT
        with mock.patch.dict(sys.modules, {'quota_balance_tests': pkg, 'hermes_constants': constants}):
            cmd = importlib.import_module('quota_balance_tests.commands')
        record = {'label': 'deepseek', 'windows': [], 'details': ['USD total: 12.50'], 'unavailable_reason': None}
        with mock.patch.object(cmd, 'read_quota_cache', return_value={'providers': {'deepseek': record}}), mock.patch.object(cmd, '_age_label', return_value='now'):
            text = cmd._render_quota('deepseek')
        self.assertIn('USD total: 12.50', text)
        self.assertNotIn('no window data', text)


if __name__ == '__main__':
    unittest.main()
