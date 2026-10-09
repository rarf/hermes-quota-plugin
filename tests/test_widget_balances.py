"""Render real widget components in Node; no network or credentials."""
import shutil
import unittest

from widget_harness import nodes, render, text, tooltip_text  # noqa: F401  (used by the tests below)

BALANCE = {'windows': [], 'details': [], 'account_balances': [
    {'currency': 'USD', 'total_balance': '12.50', 'granted_balance': '0', 'topped_up_balance': '12.50'}], 'api_calls_available': True}


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class WidgetBalanceTests(unittest.TestCase):
    def test_balance_headline_in_both_modes_without_windows(self):
        for mode in ('clean', 'dense'):
            with self.subTest(mode=mode):
                tree = render(component='row', mode=mode, provider=BALANCE)
                self.assertIn('$12.50 USD', text(tree))
                self.assertIn('Account balance', text(tree))
                self.assertIn('API calls available: yes', text(tree))
                self.assertNotIn('%', text(tree))
                self.assertNotIn('no window data', text(tree))
                self.assertIn('good', [n['props'].get('data-tone') for n in nodes(tree)])

    def test_scroll_region_and_cards_cannot_shrink_into_footer(self):
        tree = render(data={'providers': {'deepseek': BALANCE}, 'fetched_at': '2025-01-15T12:34:56Z'})
        regions = [n for n in nodes(tree) if n['props'].get('data-quota-scroll')]
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]['props']['style']['overflowY'], 'auto')
        self.assertEqual(regions[0]['props']['style']['minHeight'], 0)
        row = render(component='row', provider=BALANCE)
        self.assertEqual(row['props']['style']['flexShrink'], 0)

    def test_balance_chip_not_dash(self):
        tree = render(component='chip', provider=BALANCE)
        self.assertIn('$12.50 USD', text(tree))
        self.assertIn('Account balance', tooltip_text(tree))
        self.assertNotIn('%', text(tree))

    def test_zero_and_unavailable_calls_not_missing_data(self):
        p = dict(BALANCE, api_calls_available=False, account_balances=[dict(BALANCE['account_balances'][0], total_balance='0')])
        tree = render(component='row', provider=p)
        self.assertIn('$0.00 USD', text(tree))
        self.assertIn('API calls available: no', text(tree))
        self.assertIn('bad', [n['props'].get('data-tone') for n in nodes(tree)])

    def test_small_nonzero_balance_keeps_reported_precision(self):
        # Regression: a nonzero total must not be rounded down into "$0.00".
        for value, expected in (('0.00000001', '0.00000001'), ('0.5', '0.50')):
            with self.subTest(value=value):
                p = dict(BALANCE, account_balances=[dict(BALANCE['account_balances'][0], total_balance=value)])
                for component in ('row', 'chip'):
                    rendered = text(render(component=component, provider=p))
                    self.assertIn(expected, rendered)
                    self.assertNotIn('$0.00 USD', rendered)

    def test_failure_does_not_claim_healthy_stale_balance(self):
        p = dict(BALANCE, unavailable_reason='http-429')
        for component in ('row', 'chip'):
            tree = render(component=component, provider=p)
            self.assertNotIn('$12.50', text(tree))
            self.assertIn('http-429', text(tree) + tooltip_text(tree))

    def test_no_balance_is_not_zero(self):
        for value in (None, '', True, 'NaN', 'Infinity'):
            p = dict(BALANCE, account_balances=[{'currency': 'USD', 'total_balance': value}])
            self.assertNotIn('$0.00', text(render(component='row', provider=p)))

    def test_full_checked_time_and_timezone(self):
        tree = render(data={'providers': {'deepseek': BALANCE}, 'fetched_at': '2025-01-15T12:34:56Z', 'age_s': 7})
        s = text(tree)
        self.assertIn('Checked', s)
        self.assertIn('2025', s)
        self.assertRegex(s, r'(GMT|UTC)')
        self.assertNotIn('today', s)
        self.assertIn('poll 600s', s)  # default refresh interval is 600 s

    def test_missing_age_does_not_claim_just_checked(self):
        tree = render(data={'providers': {'deepseek': BALANCE}, 'fetched_at': '2025-01-15T12:34:56Z', 'age_s': None})
        self.assertNotIn('0s old', text(tree))

    def test_multiple_currencies_remain_separate(self):
        p = dict(BALANCE, account_balances=BALANCE['account_balances'] + [
            {'currency': 'CNY', 'total_balance': '25.125'}])
        for component in ('row', 'chip'):
            tree = render(component=component, provider=p)
            self.assertIn('$12.50 USD', text(tree))
            self.assertIn('25.125 CNY', text(tree))
            self.assertNotIn('%', text(tree))

    def test_existing_percentages_keep_priority_over_money(self):
        tree = render(component='worst', data={'providers': {
            'deepseek': BALANCE, 'anthropic': {'windows': [{'used_percent': 80}]}}})
        self.assertIn('Anthropic 20%', text(tree))

    def test_no_percent_worst_mode_can_show_balance(self):
        tree = render(component='worst', data={'providers': {'deepseek': BALANCE}})
        self.assertIn('$12.50 USD', text(tree))
        self.assertNotIn('%', text(tree))


if __name__ == '__main__':
    unittest.main()
