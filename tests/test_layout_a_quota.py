"""Layout A (compact rows with bars): every saved account and window at once.

Real desktop/plugin.js through the existing harnesses (widget_multicard.cjs,
widget_renderer.cjs, widget_monitor_renderer.cjs). SYNTHETIC payloads only:
labels like alpha.gmail / Synthetic Amber, no cached or live account data.
"""
import json
import shutil
import unittest

from test_multicard_pane import LABELS, pool_data, render as render_pane
from widget_harness import nodes, render, text, tooltip_text


def attr_nodes(tree, name):
    return [n for n in nodes(tree) if name in (n.get('props') or {})]


def codex(accounts, age=0):
    return {'age_s': age, 'providers': {'openai-codex': {'accounts': accounts}}}


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class QuotaPaneAllAccountsTests(unittest.TestCase):
    def test_pool_shows_every_account_and_window_without_selection(self):
        out = render_pane(data=pool_data((50.0, 70.0, 90.0, None)))
        tree = out['before']
        s = text(tree)
        for value in ('50% left', '30% left', '10% left'):
            self.assertIn(value, s)
        self.assertIn('fetch-error:OSError', json.dumps(tree))
        accounts = [n['props']['data-account'] for n in attr_nodes(tree, 'data-account')]
        self.assertEqual(accounts, list(LABELS))
        tones = [n['props']['data-account-tone'] for n in attr_nodes(tree, 'data-account')]
        self.assertEqual(tones, ['good', 'warn', 'bad', 'muted'])
        # No one-account-at-a-time tab bar survives.
        self.assertFalse([n for n in nodes(tree) if 'aria-pressed' in (n.get('props') or {})])

    def test_windows_render_as_bars_with_exact_remaining(self):
        out = render_pane(data=pool_data((50.0, 70.0, 90.0, None)))
        bars = attr_nodes(out['before'], 'data-window-bar')
        self.assertEqual([b['props']['data-window-bar'] for b in bars], [50, 30, 10])

    def test_unknown_window_is_not_drawn_as_zero(self):
        data = {'age_s': 3, 'providers': {'deepseek': {'windows': [
            {'label': 'Weekly', 'used_percent': None}, {'label': 'Daily', 'used_percent': 100}]}}}
        tree = render(data=data)
        bars = {b['props']['data-window']: b['props'].get('data-window-bar')
                for b in attr_nodes(tree, 'data-window')}
        self.assertEqual(bars, {'Weekly': None, 'Daily': 0})
        self.assertIn('—', text(tree))

    def test_codex_accounts_are_all_visible_in_the_pane(self):
        data = codex([
            {'label': 'Synthetic Amber', 'windows': [{'label': 'Session', 'used_percent': 21}]},
            {'label': 'Synthetic Blue', 'windows': [{'label': 'Weekly', 'used_percent': 42}]}])
        s = text(render(data=data))
        self.assertIn('79% left', s)
        self.assertIn('58% left', s)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class StatusBarNamedLimitTests(unittest.TestCase):
    def test_group_chip_names_limiting_account_window_value_and_overflow(self):
        out = render_pane(component='status', data=pool_data((50.0, 70.0, 90.0, 95.0)))
        button = next(n for n in nodes(out['before']) if n.get('type') == 'button')
        s = text(button)
        self.assertIn('CommandCode', s)
        self.assertIn('delta.mail', s)
        self.assertIn('5h', s)
        self.assertIn('5%', s)
        self.assertIn('+3', s)
        self.assertIn('delta.mail', button['props']['aria-label'])

    def test_group_chip_with_no_known_value_says_unknown_not_zero(self):
        out = render_pane(component='status', data=pool_data((None, None, None, None)))
        button = next(n for n in nodes(out['before']) if n.get('type') == 'button')
        self.assertIn('unknown', text(button))
        self.assertNotIn('0%', text(button))
        self.assertIn('+3', text(button))

    def test_codex_chip_counts_other_saved_accounts(self):
        data = codex([{'label': 'Synthetic Amber', 'windows': [{'label': 'Session', 'used_percent': 21}]},
                      {'label': 'Synthetic Blue', 'windows': [{'label': 'Weekly', 'used_percent': 42}]}])
        for mode in ('all', 'worst'):
            tree = render(component='status', statusMode=mode, data=data)
            chip = next(n for n in nodes(tree) if n.get('type') == 'button')
            self.assertIn('Synthetic Amber', text(chip))
            self.assertIn('+1', text(chip))
            self.assertIn('Synthetic Blue', tooltip_text(tree))

    def test_single_codex_account_has_no_overflow(self):
        data = {'age_s': 0, 'providers': {'openai-codex': {'windows': [{'label': 'Session', 'used_percent': 21}]}}}
        chip = next(n for n in nodes(render(component='status', data=data)) if n.get('type') == 'button')
        self.assertNotIn('+', text(chip))


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class QuotaPopoverAllAccountsTests(unittest.TestCase):
    def run_card(self, data):
        from test_widget_monitor import run, snapshot
        expr = '''(() => {
          const tree = QuotaCard({close: () => {}});
          return tree;
        })()'''
        return run(expr=expr, data=data, monitor=snapshot())['value']

    def test_popover_keeps_failed_saved_accounts_with_reason(self):
        data = pool_data((50.0, 70.0, 90.0, None))
        tree = self.run_card(data)
        providers = [n['props']['data-card-provider'] for n in attr_nodes(tree, 'data-card-provider')]
        self.assertEqual(providers, ['commandcode', 'commandcode:r2', 'commandcode:r3', 'commandcode:r4'])
        self.assertIn('fetch-error:OSError', text(tree))

    def test_popover_drops_never_configured_accounts(self):
        data = pool_data((50.0, 70.0))
        data['providers']['commandcode:r2']['unavailable_reason'] = 'no-credentials'
        tree = self.run_card(data)
        providers = [n['props']['data-card-provider'] for n in attr_nodes(tree, 'data-card-provider')]
        self.assertEqual(providers, ['commandcode'])

    def test_popover_windows_are_bars_and_unknown_is_not_zero(self):
        data = {'age_s': 3, 'providers': {'deepseek': {'windows': [
            {'label': 'Weekly', 'used_percent': None}, {'label': 'Daily', 'used_percent': 75}]}}}
        tree = self.run_card(data)
        bars = {b['props']['data-card-window']: b['props'].get('data-window-bar')
                for b in attr_nodes(tree, 'data-card-window')}
        self.assertEqual(bars, {'Weekly': None, 'Daily': 25})


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class AccountsAllWindowsTests(unittest.TestCase):
    def test_account_row_lists_every_window_as_a_bar(self):
        from test_widget_accounts import accounts_payload
        from test_widget_monitor import run
        data = accounts_payload()
        data['pools'][0]['rows'][0]['windows'] = [
            {'label': '5h', 'used': 40}, {'label': 'Weekly', 'used': None}]
        expr = ('AccountsView({page: false, accountsOverride: {data: DATA, scope: {connectionId: "local", '
                'profile: "work"}, isFetching: false, isLoading: false}})').replace('DATA', json.dumps(data))
        tree = run(expr=expr)['value']
        rows = {b['props']['data-account-window']: b['props'].get('data-window-bar')
                for b in attr_nodes(tree, 'data-account-window')}
        self.assertEqual(rows.get('5h'), 60)
        self.assertIn('Weekly', rows)
        self.assertIsNone(rows['Weekly'])  # unknown is not zero


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class CodexCreditLineTests(unittest.TestCase):
    """The credits balance is live quota state, not historical trivia."""

    DETAIL = 'Credits: 62,149'
    BANKED = 'You have 2 resets banked - use /usage reset to activate'

    def _data(self):
        return codex([{'label': 'Synthetic Amber', 'plan': 'Pro 200',
                       'windows': [{'label': 'Weekly', 'used_percent': 100}],
                       'details': [self.BANKED, self.DETAIL]}])

    def test_credit_line_is_visible_in_the_pane_card(self):
        self.assertIn(self.DETAIL, text(render(data=self._data())))

    def test_credit_line_stays_visible_in_clean_pane_mode(self):
        # The pane's default clean mode still has to show the balance: it is what
        # carries the account past a spent window.
        body = text(render(data=self._data(), mode='clean'))
        self.assertIn(self.DETAIL, body)
        self.assertNotIn('usage reset', body)

    def test_credit_line_is_visible_in_the_status_bar_tip(self):
        tip = tooltip_text(render(component='status', data=self._data()))
        self.assertIn(self.DETAIL, tip)
        # Banked-reset guidance stays pane-only, so the chip keeps its size.
        self.assertNotIn('usage reset', tip)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class CodexCreditInPopoverCardTests(unittest.TestCase):
    """The status-bar popover card has no dense mode: it shows the balance."""

    def run_card(self, data):
        from test_widget_monitor import run, snapshot
        expr = '(() => QuotaCard({close: () => {}}))()'
        return run(expr=expr, data=data, monitor=snapshot())['value']

    def test_credit_only_payload_is_visible_in_the_popover(self):
        from dataclasses import asdict
        from quota_providers.builtin import _parse_codex_payload
        result = _parse_codex_payload({
            'plan_type': 'pro',
            'credits': {'has_credits': True, 'balance': '62148.5742430000'},
        })
        record = asdict(result)
        self.assertEqual(record['windows'], [])
        self.assertIsNone(record['unavailable_reason'])
        data = {'age_s': 0, 'providers': {'openai-codex': record}}
        body = text(self.run_card(data))
        self.assertIn('Credits: 62,149', body)
        self.assertNotIn('No quota data yet', body)

    def test_credit_line_is_visible_in_the_popover_card(self):
        data = codex([{'label': 'Synthetic Amber', 'plan': 'Pro 200',
                       'windows': [{'label': 'Weekly', 'used_percent': 100}],
                       'details': ['You have 2 resets banked - use /usage reset to activate',
                                   'Credits: 62,149']}])
        tree = self.run_card(data)
        self.assertIn('Credits: 62,149', text(tree))
        # Only the balance crosses over; the pane keeps the reset guidance.
        self.assertNotIn('usage reset', text(tree))

    def test_credit_states_without_windows_survive_multi_account_filtering(self):
        from dataclasses import asdict
        from quota_providers.builtin import _parse_codex_payload
        cases = [
            ({'has_credits': True, 'balance': '0'}, 'Credits: 0'),
            ({'has_credits': True}, 'Credits: available'),
            ({'unlimited': True}, 'Credits: unlimited'),
            ({'has_credits': False, 'overage_limit_reached': True},
             'Credits overage limit reached'),
        ]
        accounts = []
        for index, (credits, _) in enumerate(cases):
            account = asdict(_parse_codex_payload({'credits': credits}))
            account['label'] = 'Synthetic credit account ' + str(index)
            accounts.append(account)
        tree = self.run_card(codex(accounts))
        body = text(tree)
        for index, (_, detail) in enumerate(cases):
            self.assertIn('Synthetic credit account ' + str(index), body)
            self.assertEqual(body.count(detail), 1)
        self.assertEqual(len(attr_nodes(tree, 'data-card-credit')), len(cases))

    def test_unrelated_details_do_not_admit_empty_popover_rows(self):
        data = codex([{'label': 'Synthetic Amber', 'windows': [],
                       'details': ['You have 2 resets banked - use /usage reset to activate']}])
        self.assertEqual(attr_nodes(self.run_card(data), 'data-card-provider'), [])

    def test_no_credentials_stays_hidden_even_with_credit_details(self):
        data = codex([{'label': 'Synthetic Amber', 'windows': [],
                       'unavailable_reason': 'no-credentials', 'details': ['Credits: 10']}])
        self.assertEqual(attr_nodes(self.run_card(data), 'data-card-provider'), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
