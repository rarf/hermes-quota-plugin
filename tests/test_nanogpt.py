"""Offline NanoGPT quota contracts with synthetic responses."""
import importlib
import json
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class NanoGPTTests(unittest.TestCase):
    def module(self):
        import quota_providers
        self.assertIn('nano-gpt', quota_providers.PROVIDER_FETCHERS)
        return importlib.import_module('quota_providers.nanogpt')

    def payload(self):
        return {
            'active': True, 'allowOverage': True,
            'limits': {'weeklyInputTokens': 1000, 'dailyImages': 100},
            'weeklyInputTokens': {'used': 250, 'remaining': 750,
                                  'percentUsed': 0.25, 'resetAt': 1794182400000},
            'dailyImages': {'used': 37, 'remaining': 63,
                            'percentUsed': 0.37, 'resetAt': 1793664000000},
            'stripeSubscriptionId': 'PRIVATE_ID',
        }

    def fetch(self, payload, balance=None):
        mod = self.module()
        responses = [BytesIO(json.dumps(p).encode()) for p in
                     (payload, {'usd_balance': '27.30314728', 'nanoDepositAddress': 'PRIVATE_ADDRESS'}
                      if balance is None else balance)]
        with mock.patch.object(mod, '_api_key', return_value='SYNTHETIC_SECRET'), \
                mock.patch.object(mod, 'urlopen_no_redirect', side_effect=responses) as opener:
            result = mod.fetch_nanogpt_quota()
        self.assertNotIn('SYNTHETIC_SECRET', repr(result))
        self.assertNotIn('PRIVATE_', repr(result))
        return result, opener

    def test_subscription_and_balance(self):
        result, opener = self.fetch(self.payload())
        self.assertIsNone(result.unavailable_reason)
        self.assertIsNone(result.plan)
        self.assertEqual([(w.label, w.used_percent, w.reset_at) for w in result.windows], [
            ('Weekly tokens', 25.0, '2026-11-09T00:00:00+00:00'),
            ('Daily images', 37.0, '2026-11-03T00:00:00+00:00'),
        ])
        self.assertEqual(result.account_balances[0].total_balance, '27.30314728')
        self.assertFalse(any('paid balance may be charged' in detail for detail in result.details))
        calls = opener.call_args_list
        self.assertEqual(calls[0].args[0].full_url, 'https://api.nano-gpt.com/api/v1/subscription/usage')
        self.assertEqual(calls[0].args[0].get_header('Authorization'), 'Bearer SYNTHETIC_SECRET')
        self.assertEqual(calls[1].args[0].full_url, 'https://api.nano-gpt.com/api/check-balance')
        self.assertEqual(calls[1].args[0].get_method(), 'POST')
        self.assertEqual(calls[1].args[0].get_header('X-api-key'), 'SYNTHETIC_SECRET')
        for call in calls:
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertLessEqual(call.kwargs['timeout'], 7)

    def test_reported_fraction_takes_precedence_over_counter_calculation(self):
        payload = self.payload()
        payload['weeklyInputTokens'].update(used=1000, remaining=-1, percentUsed=1.001)
        result, _ = self.fetch(payload)
        self.assertAlmostEqual(result.windows[0].used_percent, 100.1)
        self.assertEqual(result.windows[0].remaining_pct(), 0)

    def test_negative_remaining_without_limit_or_fraction_preserves_over_100(self):
        result, _ = self.fetch({'active': True, 'weeklyInputTokens': {'used': 1001, 'remaining': -1}})
        self.assertEqual(result.windows[0].used_percent, 100.1)
        self.assertEqual(result.windows[0].remaining_pct(), 0)

    def test_negative_remaining_with_limit_or_fraction_does_not_roll_over(self):
        payload = self.payload()
        payload['weeklyInputTokens'].update(used=1001, remaining=-1, percentUsed=1.001)
        for has_limit in (True, False):
            with self.subTest(has_limit=has_limit):
                if not has_limit:
                    payload.pop('limits')
                result, _ = self.fetch(payload)
                self.assertAlmostEqual(result.windows[0].used_percent, 100.1)
                self.assertEqual(result.windows[0].remaining_pct(), 0)

    def test_fraction_fallback_always_scales_including_over_100(self):
        for fraction, expected in ((0.25, 25), (1.01, 101), (0, 0)):
            payload = {'active': True, 'weeklyInputTokens': {'percentUsed': fraction}}
            result, _ = self.fetch(payload)
            self.assertAlmostEqual(result.windows[0].used_percent, expected)

    def test_counter_fallback_without_fraction(self):
        result, _ = self.fetch({'active': True, 'dailyImages': {'used': 37, 'remaining': 63}})
        self.assertEqual(result.windows[0].used_percent, 37)

    def test_daily_token_limit_is_optional(self):
        payload = self.payload()
        payload['limits']['dailyInputTokens'] = 100
        payload['dailyInputTokens'] = {'used': 10}
        result, _ = self.fetch(payload)
        self.assertEqual(result.windows[0].label, 'Daily tokens')
        self.assertEqual(result.windows[0].used_percent, 10)

    def test_token_based_trial_uses_its_reported_quota(self):
        payload = {'active': True, 'usageUnits': 'tokens', 'tokenLimits': {'total': 1000},
                   'tokens': {'used': 250, 'remaining': 750, 'percentUsed': 0.25,
                              'resetAt': 1794182400000}}
        result, _ = self.fetch(payload)
        self.assertEqual([(window.label, window.used_percent) for window in result.windows],
                         [('Trial tokens', 25.0)])

    def test_null_limit_is_not_an_applicable_quota(self):
        payload = self.payload()
        payload['limits']['weeklyInputTokens'] = None
        result, _ = self.fetch(payload)
        self.assertEqual([window.label for window in result.windows], ['Daily images'])

    def test_degraded_window_remains_visible_as_unknown(self):
        payload = self.payload()
        payload['weeklyInputTokens'] = {'used': None, 'remaining': None, 'percentUsed': None,
                                         'degraded': True, 'resetAt': 1794182400000}
        result, _ = self.fetch(payload)
        windows = {window.label: window for window in result.windows}
        self.assertIn('Weekly tokens', windows)
        self.assertIsNone(windows['Weekly tokens'].used_percent)
        self.assertIsNone(windows['Weekly tokens'].remaining_pct())
        self.assertEqual(windows['Daily images'].used_percent, 37)

    def test_invalid_counters_are_not_interpreted_as_usage(self):
        for row in ({}, {'percentUsed': True}, {'percentUsed': 'NaN'},
                    {'percentUsed': -0.2}, {'used': -1, 'remaining': 10},
                    {'used': None, 'remaining': None, 'percentUsed': None}):
            with self.subTest(row=row):
                result, _ = self.fetch({'active': True, 'weeklyInputTokens': row}, {})
                self.assertEqual(result.windows, [])
                self.assertEqual(result.unavailable_reason, 'no-data')
                self.assertIn('Subscription quota unavailable (unknown)', result.details)

    def test_invalid_reset_does_not_discard_valid_usage(self):
        for reset in (True, 'NaN', -1, '1e100', 1e15, None):
            result, _ = self.fetch({'active': True, 'dailyImages': {'percentUsed': 0.25, 'resetAt': reset}})
            self.assertEqual(result.windows[0].used_percent, 25)
            self.assertIsNone(result.windows[0].reset_at)

    def test_paygo_only_account_keeps_balance(self):
        result, opener = self.fetch({'active': False})
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.windows, [])
        self.assertEqual(len(opener.call_args_list), 2)
        self.assertTrue(result.account_balances)

    def test_zero_balance_is_data(self):
        result, _ = self.fetch({'active': False}, {'usd_balance': '0'})
        self.assertTrue(result.has_data())
        self.assertEqual(result.account_balances[0].total_balance, '0')
        self.assertIsNone(result.api_calls_available)

    def test_invalid_balance_preserves_subscription(self):
        for usd in (None, True, 'NaN', 'Infinity', '1e999999', '0e-99999'):
            result, _ = self.fetch(self.payload(), {'usd_balance': usd})
            self.assertIsNone(result.unavailable_reason)
            self.assertEqual(len(result.windows), 2)
            self.assertEqual(result.account_balances, [])
            self.assertIn('Prepaid balance unavailable (parse-pending)', result.details)

    def test_missing_key_does_not_send_requests(self):
        mod = self.module()
        with mock.patch.object(mod, '_api_key', return_value=None), \
                mock.patch.object(mod, 'urlopen_no_redirect') as opener:
            self.assertEqual(mod.fetch_nanogpt_quota().unavailable_reason, 'no-credentials')
            opener.assert_not_called()

    def test_key_resolution_uses_profile_helper(self):
        import types
        mod = self.module()
        config = types.ModuleType('hermes_cli.config')
        config.get_env_value_prefer_dotenv = mock.Mock(return_value=' SYNTHETIC_SECRET ')
        parent = types.ModuleType('hermes_cli')
        parent.config = config
        with mock.patch.dict(sys.modules, {'hermes_cli': parent, 'hermes_cli.config': config}):
            self.assertEqual(mod._api_key(), 'SYNTHETIC_SECRET')
        config.get_env_value_prefer_dotenv.assert_called_once_with('NANOGPT_API_KEY')

    def test_http_failures_are_not_missing_subscriptions(self):
        from urllib.error import HTTPError
        mod = self.module()
        for code, reason in ((401, 'auth-failed'), (403, 'auth-failed'),
                             (429, 'http-429'), (500, 'http-500'), (302, 'http-302')):
            with self.subTest(code=code), mock.patch.object(mod, '_api_key', return_value='SYNTHETIC_SECRET'), \
                    mock.patch.object(mod, 'urlopen_no_redirect', side_effect=HTTPError('https://test', code, 'PRIVATE_ERROR', {}, None)) as opener:
                result = mod.fetch_nanogpt_quota()
                self.assertEqual(result.unavailable_reason, reason)
                self.assertNotIn('PRIVATE_ERROR', repr(result))
                self.assertEqual(opener.call_count, 1)

    def test_balance_failure_preserves_quota(self):
        mod = self.module()
        with mock.patch.object(mod, '_api_key', return_value='SYNTHETIC_SECRET'), \
                mock.patch.object(mod, 'urlopen_no_redirect', side_effect=[BytesIO(json.dumps(self.payload()).encode()), TimeoutError('PRIVATE_ERROR')]):
            result = mod.fetch_nanogpt_quota()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(result.windows), 2)
        self.assertIn('Prepaid balance unavailable (timeout)', result.details)

    def test_malformed_or_oversized_body_fails_open(self):
        mod = self.module()
        for body, reason in ((b'not json', 'bad-json'), (b'x' * (1024 * 1024 + 1), 'response-too-large'),
                             (b'{}', 'parse-pending'), (b'[]', 'parse-pending'),
                             (b'{"active":"true"}', 'parse-pending')):
            with self.subTest(reason=reason), mock.patch.object(mod, '_api_key', return_value='SYNTHETIC_SECRET'), \
                    mock.patch.object(mod, 'urlopen_no_redirect', return_value=BytesIO(body)) as opener:
                self.assertEqual(mod.fetch_nanogpt_quota().unavailable_reason, reason)
                self.assertEqual(opener.call_count, 1)

    def test_exceptions_do_not_expose_secrets(self):
        mod = self.module()
        for exception, expected in ((TimeoutError('PRIVATE_ERROR'), 'timeout'),
                                    (RuntimeError('PRIVATE_ERROR'), 'fetch-error')):
            with mock.patch.object(mod, '_api_key', return_value='SYNTHETIC_SECRET'), \
                    mock.patch.object(mod, 'urlopen_no_redirect', side_effect=exception):
                result = mod.fetch_nanogpt_quota()
                self.assertEqual(result.unavailable_reason, expected)
                self.assertNotIn('PRIVATE_ERROR', repr(result))

    def test_spent_budget_does_not_start_balance_request(self):
        mod = self.module()
        with mock.patch.object(mod, '_api_key', return_value='SYNTHETIC_SECRET'), \
                mock.patch.object(mod, 'urlopen_no_redirect', return_value=BytesIO(json.dumps(self.payload()).encode())) as opener, \
                mock.patch.object(mod.Deadline, 'expired', side_effect=[False, True]):
            result = mod.fetch_nanogpt_quota()
        self.assertEqual(opener.call_count, 1)
        self.assertIsNone(result.unavailable_reason)
        self.assertIn('Prepaid balance unavailable (timeout)', result.details)

    def test_redirects_are_refused(self):
        from quota_providers import base
        mod = self.module()
        self.assertIs(mod.urlopen_no_redirect, base.urlopen_no_redirect)
        self.assertIsNone(base.NoRedirectHandler().redirect_request(None, None, 302, '', {}, 'https://elsewhere'))


if __name__ == '__main__':
    unittest.main()
