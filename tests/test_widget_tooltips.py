"""Contract for SDK tooltip labels; browser hover lives in widget_hover.cjs."""
import unittest
from widget_harness import render, nodes, text, tooltip_text


class WidgetTooltipTests(unittest.TestCase):
    def test_fractional_generic_window_never_claims_limit_reached(self):
        data = {'age_s': 0, 'providers': {'openai-codex': {'accounts': [
            {'windows': [{'label': 'Session', 'used_percent': 99.9},
                         {'label': 'Weekly', 'used_percent': 42}]}]}}}
        tip = tooltip_text(render(component='status', data=data))
        self.assertIn('● Account 1 · <1% left', tip)
        self.assertIn('Session · <1% left', tip)
        self.assertNotIn('limit reached', tip)

    def test_compact_hover_keeps_details_in_pane_and_other_provider_info(self):
        data = {'age_s': 0, 'providers': {
            'openai-codex': {'accounts': [
                {'plan': 'pro', 'details': ['Banked resets: 2'], 'windows': [
                    {'label': 'Session', 'used_percent': 21},
                    {'label': 'Weekly', 'used_percent': 10},
                    {'label': '5.3 Codex Spark · Weekly', 'used_percent': 100}]}]},
            'anthropic': {'windows': [{'label': 'Weekly', 'used_percent': 60}]}}}
        for mode in ('all', 'worst'):
            tip = tooltip_text(render(component='status', statusMode=mode, data=data))
            for part in ('Session · 79% left', 'Weekly · 90% left', 'Weekly · 40% left'):
                self.assertIn(part, tip)
            for part in ('Plan:', 'Banked resets', 'Spark', 'priority-eligible', 'inference routing'):
                self.assertNotIn(part, tip)
        pane = text(render(data=data))
        for part in ('pro', 'Banked resets: 2', '5.3 Codex Spark'):
            self.assertIn(part, pane)

    def test_both_status_modes_use_multiline_sdk_tip_not_native_title(self):
        data = {'age_s': 0, 'providers': {'openai-codex': {'accounts': [
            {'windows': [{'label': 'Session', 'used_percent': 21, 'reset_at': '2099-01-01T00:00:00Z'}]},
            {'windows': [{'label': 'Weekly', 'used_percent': 42, 'reset_at': '2099-01-02T00:00:00Z'}]},
        ]}}}
        for mode in ('all', 'worst'):
            with self.subTest(mode=mode):
                tree = render(component='status', statusMode=mode, data=data)
                buttons = [n for n in nodes(tree) if n.get('type') == 'button']
                self.assertEqual(len(buttons), 1)
                tips = [n for n in nodes(tree) if n.get('type') == 'Tip']
                self.assertEqual(len(tips), 1, 'Status chip must mount SDK Tip')
                self.assertNotIn('title', buttons[0]['props'])
                self.assertTrue(buttons[0]['props'].get('aria-label'))
                self.assertIn('Account 1', text(buttons[0]))
                self.assertNotIn('Account 2', text(buttons[0]))
                props = tips[0]['props']
                self.assertEqual(props['side'], 'top')
                self.assertEqual(props['align'], 'start')
                self.assertEqual(props['boundary'], 'viewport')
                style = props['label']['props']['style']
                self.assertIn(style['whiteSpace'], ('pre-line', 'pre-wrap'))
                self.assertTrue(style['maxHeight'])
                self.assertEqual(style['overflowY'], 'auto')
                tip = tooltip_text(tree)
                self.assertLess(tip.index('Account 1'), tip.index('Account 2'))
                for part in ('● Account 1 · 79% left', '○ Account 2 · 58% left', 'Session · resets in', 'Weekly · resets in'):
                    self.assertIn(part, tip)
                for old in ('OpenAI Codex · remaining', 'Shown in status bar', 'Click for details', 'priority-eligible', 'inference routing', 'selected for display', 'Generic Session', 'Plan:', '2099-', 'Click to open Quota pane'):
                    self.assertNotIn(old, tip)
                self.assertEqual(tip.count('79% left'), 1)
                self.assertEqual(tip.count('58% left'), 1)
                self.assertLessEqual(len(tip.splitlines()), 7)
