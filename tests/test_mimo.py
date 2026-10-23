"""Offline Xiaomi MiMo quota contracts, using synthetic payloads only."""
import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class MimoTests(unittest.TestCase):
    def module(self):
        import quota_providers
        self.assertIn('xiaomi', quota_providers.PROVIDER_FETCHERS)
        return importlib.import_module('quota_providers.mimo')

    def usage_payload(self):
        return {'code': 0, 'message': '', 'data': {
            'monthUsage': {'percent': 0.3935, 'items': [
                {'name': 'month_total_token', 'used': 14954038035,
                 'limit': 38000000000, 'percent': 0.3935}]},
            'usage': {'items': [
                {'name': 'plan_total_token', 'used': 14954038035,
                 'limit': 38000000000, 'percent': 0.39},
                {'name': 'compensation_total_token', 'used': 0, 'limit': 0, 'percent': 0}]}}}

    def detail_payload(self):
        return {'code': 0, 'data': {'planCode': 'pro', 'planName': 'Pro',
                                    'currentPeriodEnd': '2026-11-05 23:59:59',
                                    'expired': False, 'enableAutoRenew': True}}

    def balance_payload(self):
        return {'code': 0, 'data': {'balance': '25.51', 'currency': 'USD'}}

    def fetch(self, payloads=None, cookie=('api-platform_serviceToken=SYNTHETIC_TOKEN; userId=42', None)):
        """Run the fetcher with canned per-URL responses; returns the result."""
        mod = self.module()
        by_url = payloads if payloads is not None else {
            mod._USAGE_URL: self.usage_payload(),
            mod._DETAIL_URL: self.detail_payload(),
            mod._BALANCE_URL: self.balance_payload(),
        }

        def fake_get_json(url, cookie_header, timeout):
            value = by_url[url]
            return value if isinstance(value, tuple) else (value, None)

        with mock.patch.object(mod, '_mimo_enabled', return_value=True), \
             mock.patch.object(mod, '_load_cookie', return_value=cookie), \
             mock.patch.object(mod, '_get_json', side_effect=fake_get_json) as get:
            result = mod.fetch_mimo_quota()
        self.assertNotIn('SYNTHETIC_TOKEN', repr(result))
        return result, get

    def test_registered_without_taking_over_another_provider(self):
        self.module()
        from quota_providers import PROVIDER_FETCHERS
        self.assertEqual(PROVIDER_FETCHERS['xiaomi'].__module__, 'quota_providers.mimo')
        self.assertNotEqual(PROVIDER_FETCHERS['openrouter'].__module__, 'quota_providers.mimo')
        self.assertIn('deepseek', PROVIDER_FETCHERS)

    def test_opt_in_disabled_by_default(self):
        mod = self.module()
        fake_cfg = types.ModuleType('hermes_cli.config')
        setattr(fake_cfg, 'load_config_readonly', lambda: {})
        fake_pkg = types.ModuleType('hermes_cli')
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.dict(sys.modules, {'hermes_cli': fake_pkg, 'hermes_cli.config': fake_cfg}), \
             mock.patch.object(mod, '_load_cookie') as load, \
             mock.patch.object(mod, '_get_json') as get:
            self.assertFalse(mod._mimo_enabled())
            self.assertEqual(mod.fetch_mimo_quota().unavailable_reason, 'opt-in-disabled')
        load.assert_not_called()
        get.assert_not_called()

    def test_config_setting_enables_the_fetch(self):
        mod = self.module()
        fake_cfg = types.ModuleType('hermes_cli.config')
        setattr(fake_cfg, 'load_config_readonly', lambda: {
            'plugins': {'entries': {'quota': {'settings': {'mimoEnabled': True}}}}})
        fake_pkg = types.ModuleType('hermes_cli')
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.dict(sys.modules, {'hermes_cli': fake_pkg, 'hermes_cli.config': fake_cfg}):
            self.assertTrue(mod._mimo_enabled())

    def test_missing_cookie_never_fetches(self):
        mod = self.module()
        with mock.patch.object(mod, '_mimo_enabled', return_value=True), \
             mock.patch.object(mod, '_load_cookie', return_value=(None, 'no-session-cookies')), \
             mock.patch.object(mod, '_get_json') as get:
            result = mod.fetch_mimo_quota()
        self.assertEqual(result.unavailable_reason, 'no-session-cookies')
        get.assert_not_called()

    def test_incomplete_cookie_header_is_truthful(self):
        mod = self.module()
        with mock.patch.dict(os.environ, {'XIAOMI_MIMO_COOKIE': 'api-platform_serviceToken=ONLY_ONE'}):
            self.assertEqual(mod._load_cookie(), (None, 'incomplete-cookies'))

    def test_cookie_requires_both_names_and_never_leaks(self):
        mod = self.module()
        header = 'api-platform_serviceToken=SYNTHETIC_TOKEN; userId=42; other=x'
        with mock.patch.dict(os.environ, {'XIAOMI_MIMO_COOKIE': header}):
            loaded, reason = mod._load_cookie()
        self.assertIsNone(reason)
        self.assertIn('api-platform_serviceToken=SYNTHETIC_TOKEN', loaded)
        self.assertIn('userId=42', loaded)

    def test_happy_path_token_plan(self):
        result, get = self.fetch()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.plan, 'Pro')
        self.assertEqual([w.label for w in result.windows], ['Monthly tokens'])
        window = result.windows[0]
        self.assertAlmostEqual(window.used_percent, round(100 * 14954038035 / 38000000000, 2))
        self.assertEqual(window.reset_at, '2026-11-05T23:59:59+00:00')
        self.assertEqual([(b.currency, b.total_balance) for b in result.account_balances],
                         [('USD', '25.51')])
        self.assertIn('Plan period ends 2026-11-05T23:59:59+00:00', result.details)
        self.assertEqual(get.call_count, 3)
        for call in get.call_args_list:
            self.assertEqual(call.args[1], 'api-platform_serviceToken=SYNTHETIC_TOKEN; userId=42')

    def test_month_usage_absent_falls_back_to_plan_items(self):
        mod = self.module()
        usage = {'code': 0, 'data': {'usage': {'items': [
            {'name': 'plan_total_token', 'used': 5, 'limit': 100, 'percent': 0.05}]}}}
        payloads = {mod._USAGE_URL: usage, mod._DETAIL_URL: self.detail_payload(),
                    mod._BALANCE_URL: self.balance_payload()}
        result, _ = self.fetch(payloads)
        self.assertEqual([w.label for w in result.windows], ['Plan credits'])
        self.assertEqual(result.windows[0].used_percent, 5.0)

    def test_percent_only_items_use_the_server_percent(self):
        mod = self.module()
        usage = {'code': 0, 'data': {'monthUsage': {'items': [
            {'name': 'month_total_token', 'percent': 0.25}]}}}
        payloads = {mod._USAGE_URL: usage, mod._DETAIL_URL: self.detail_payload(),
                    mod._BALANCE_URL: self.balance_payload()}
        result, _ = self.fetch(payloads)
        self.assertEqual(result.windows[0].used_percent, 25.0)

    def test_expired_plan_is_reported_not_hidden(self):
        mod = self.module()
        detail = {'data': {'planName': 'Lite', 'currentPeriodEnd': '2026-01-01 23:59:59',
                           'expired': True}}
        payloads = {mod._USAGE_URL: self.usage_payload(), mod._DETAIL_URL: detail,
                    mod._BALANCE_URL: self.balance_payload()}
        result, _ = self.fetch(payloads)
        self.assertIn('Token Plan expired', result.details)

    def test_unparsable_period_end_yields_no_reset(self):
        mod = self.module()
        detail = {'data': {'planName': 'Pro', 'currentPeriodEnd': 'someday'}}
        payloads = {mod._USAGE_URL: self.usage_payload(), mod._DETAIL_URL: detail,
                    mod._BALANCE_URL: self.balance_payload()}
        result, _ = self.fetch(payloads)
        self.assertIsNone(result.windows[0].reset_at)

    def test_garbage_responses_fail_open(self):
        mod = self.module()
        empty = {mod._USAGE_URL: ({}, None), mod._DETAIL_URL: ({}, None),
                 mod._BALANCE_URL: ({}, None)}
        self.assertEqual(self.fetch(empty)[0].unavailable_reason, 'parse-pending')
        bad = {mod._USAGE_URL: (None, 'bad-json'), mod._DETAIL_URL: (None, 'bad-json'),
               mod._BALANCE_URL: (None, 'bad-json')}
        self.assertEqual(self.fetch(bad)[0].unavailable_reason, 'bad-json')

    def test_auth_failure_maps_to_auth_failed(self):
        mod = self.module()
        denied = {u: (None, 'auth-failed') for u in
                  (mod._USAGE_URL, mod._DETAIL_URL, mod._BALANCE_URL)}
        self.assertEqual(self.fetch(denied)[0].unavailable_reason, 'auth-failed')

    def test_empty_usage_items_are_data_not_a_crash(self):
        mod = self.module()
        usage = {'code': 0, 'data': {'monthUsage': {'items': []}}}
        payloads = {mod._USAGE_URL: usage, mod._DETAIL_URL: self.detail_payload(),
                    mod._BALANCE_URL: self.balance_payload()}
        result, _ = self.fetch(payloads)
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.windows, [])
        self.assertEqual(result.plan, 'Pro')

    def test_parsers_reject_malformed_numbers(self):
        mod = self.module()
        self.assertIsNone(mod._int(True))
        self.assertIsNone(mod._int('NaN'))
        self.assertIsNone(mod._int(-5))
        self.assertIsNone(mod._server_pct('high'))
        self.assertIsNone(mod._server_pct(float('nan')))
        self.assertEqual(mod._server_pct(0.5), 50.0)
        self.assertEqual(mod._server_pct(25), 25.0)


if __name__ == '__main__':
    unittest.main()
