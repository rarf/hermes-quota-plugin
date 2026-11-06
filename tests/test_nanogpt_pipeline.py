"""NanoGPT responses through cache serialization, CLI and Desktop rendering."""
import importlib
from io import BytesIO
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

from widget_harness import render, text

ROOT = Path(__file__).resolve().parent.parent
_LOADED = None


def modules():
    global _LOADED
    if _LOADED is None:
        name = 'quota_nanogpt_pipeline_tests'
        package = types.ModuleType(name)
        package.__path__ = [str(ROOT)]
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: ROOT
        with mock.patch.dict(sys.modules, {name: package, 'hermes_constants': constants}):
            cache = importlib.import_module(name + '.quota_cache')
            commands = importlib.import_module(name + '.commands')
            provider = importlib.import_module(name + '.quota_providers.nanogpt')
        _LOADED = cache, commands, provider
    return _LOADED


def record(payload):
    cache, _, provider = modules()
    responses = [BytesIO(json.dumps(value).encode()) for value in
                 (payload, {'usd_balance': '27.30'})]
    with mock.patch.object(provider, '_api_key', return_value='SYNTHETIC_SECRET'), \
            mock.patch.object(provider, 'urlopen_no_redirect', side_effect=responses):
        result = cache._fetch_one('nano-gpt', provider.fetch_nanogpt_quota)
    return json.loads(json.dumps(result, allow_nan=False))


def cli(provider):
    _, commands, _ = modules()
    with mock.patch.object(commands, 'read_quota_cache', return_value={'providers': {'nano-gpt': provider}}):
        return commands._render_quota('nano-gpt')


class NanoGPTPipelineTests(unittest.TestCase):
    def test_negative_remaining_never_displays_a_full_allowance(self):
        for fields in ({'percentUsed': 1.001}, {}, {'percentUsed': None}):
            payload = {'active': True, 'weeklyInputTokens': {'used': 1001, 'remaining': -1, **fields}}
            with self.subTest(fields=fields):
                provider = record(payload)
                self.assertAlmostEqual(provider['windows'][0]['used_percent'], 100.1)
                self.assertIn('0% left', cli(provider))
                self.assertNotIn('100% left', cli(provider))
                for component in ('row', 'chip'):
                    output = text(render(component=component, id='nano-gpt', provider=provider))
                    self.assertIn('0%', output)
                    self.assertNotIn('100%', output)

    def test_degraded_quota_is_unknown_instead_of_a_full_allowance(self):
        provider = record({'active': True, 'weeklyInputTokens': {
            'used': None, 'remaining': None, 'percentUsed': None, 'degraded': True}})
        self.assertIsNone(provider['windows'][0]['used_percent'])
        self.assertIn('Weekly tokens', cli(provider))
        self.assertNotIn('100%', cli(provider))
        for mode in ('clean', 'dense'):
            output = text(render(component='row', id='nano-gpt', provider=provider, mode=mode))
            self.assertIn('Weekly tokens', output)
            self.assertNotIn('100%', output)
            self.assertNotIn('0% left', output)

    def test_paid_balance_does_not_change_subscription_quota(self):
        provider = record({'active': True, 'allowOverage': True,
                           'routing': {'recommendedMode': 'unavailable', 'paidSpendPolicyAllowsBalance': False},
                           'weeklyInputTokens': {'percentUsed': 1.001}})
        self.assertAlmostEqual(provider['windows'][0]['used_percent'], 100.1)
        self.assertEqual(provider['account_balances'][0]['total_balance'], '27.30')
        self.assertNotIn('may be charged', cli(provider))
        self.assertIn('0% left', cli(provider))


if __name__ == '__main__':
    unittest.main()
