"""Reset-cycle time markers on real quota widget components, offline."""
from datetime import datetime, timezone
import json
import unittest

from test_widget_monitor import run
from widget_harness import nodes

NOW = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def window(label='Weekly Limit', hours_left=84, used=4):
    reset = datetime.fromtimestamp(NOW / 1000 + hours_left * 3600, tz=timezone.utc).isoformat()
    return {'label': label, 'used_percent': used, 'reset_at': reset}


def tile(w, provider='grok'):
    props = {'w': w, 'providerId': provider, 'size': 50, 'resetFormat': 'relative'}
    return run(expr='WindowTile(' + json.dumps(props) + ')', now=NOW, enroll=False)['value']


def markers(tree):
    return [node for node in nodes(tree) if 'data-reset-pace' in node.get('props', {})]


class ResetPaceTests(unittest.TestCase):
    def test_five_hour_and_daily_windows(self):
        cases = [('Session', 'ollama', 2.5), ('Current session', 'anthropic', 2.5),
                 ('Session', 'openai-codex', 2.5), ('MiniMax-M2 · Session', 'minimax', 2.5),
                 ('Session', 'ollama-cloud', 2.5), ('5-hour', 'opencode-go', 2.5),
                 ('Spark · 5h', 'openai-codex', 2.5), ('Gemini · 5h', 'antigravity', 2.5),
                 ('Daily images', 'nano-gpt', 12), ('Daily tokens', 'nano-gpt', 12)]
        for label, provider, hours_left in cases:
            with self.subTest(label=label, provider=provider):
                tick = markers(tile(window(label, hours_left), provider))
                self.assertEqual(len(tick), 1)
                self.assertAlmostEqual(tick[0]['props']['data-reset-pace'], 50)

    def test_tick_is_present_in_quota_popover_and_account_gauges(self):
        w = window()
        expressions = [
            'QuotaCardWindow(' + json.dumps({'w': w, 'providerId': 'grok', 'nowMs': NOW}) + ')',
            'AccountRing(' + json.dumps({'windows': [{'label': w['label'], 'used': 4, 'reset': datetime.fromisoformat(w['reset_at']).timestamp()}], 'providerId': 'grok', 'nowMs': NOW}) + ')',
            'ProviderChip(' + json.dumps({'pid': 'grok', 'provider': {'windows': [w]}}) + ')',
        ]
        for expression in expressions:
            with self.subTest(component=expression.split('(', 1)[0]):
                tree = run(expr=expression, now=NOW, enroll=False)['value']
                tick = markers(tree)
                self.assertEqual(len(tick), 1)
                self.assertAlmostEqual(tick[0]['props']['data-reset-pace'], 50)

    def test_worst_chip_tick_uses_the_window_that_supplies_its_quota(self):
        data = {'age_s': 0, 'providers': {
            'grok': {'windows': [window()]},
            'ollama': {'windows': [window('Session', hours_left=1, used=20)]},
        }}
        tree = run(expr='QuotaChipWithBar()', data=data, now=NOW, enroll=False)['value']
        tick = markers(tree)
        self.assertEqual(len(tick), 1)
        self.assertAlmostEqual(tick[0]['props']['data-reset-pace'], 20)

    def test_unknown_cadences_and_unusable_resets_have_no_tick(self):
        cases = [(window('Monthly', 12), 'copilot'), (window('Session', 2), 'unknown'),
                 (window('Weekly 5h', 2), 'grok'), (window(hours_left=169), 'grok'),
                 (window(hours_left=0), 'grok'), (window(hours_left=-1), 'grok')]
        cases.extend([(dict(window(), reset_at=value), 'grok') for value in (None, '', 'bad-time')])
        for w, provider in cases:
            with self.subTest(window=w):
                self.assertEqual(markers(tile(w, provider)), [])

    def test_unknown_or_invalid_usage_has_no_tick(self):
        for value in (None, True, '4', -1):
            with self.subTest(used=value):
                self.assertEqual(markers(tile(window(used=value))), [])

    def test_local_clock_moves_the_tick_without_provider_requests(self):
        props = {'w': window('Session', 2.5), 'providerId': 'ollama', 'size': 50}
        expression = 'WindowTile(' + json.dumps(props) + ')'
        before = run(expr=expression, now=NOW, enroll=False)
        after = run(expr=expression, now=NOW + 3600 * 1000, enroll=False)
        self.assertEqual(markers(before['value'])[0]['props']['data-reset-pace'], 50)
        self.assertEqual(markers(after['value'])[0]['props']['data-reset-pace'], 30)
        self.assertEqual(before['cli'], [])
        self.assertEqual(after['cli'], [])

    def test_under_and_over_pace_do_not_change_the_remaining_quota_bar(self):
        for used, left in ((4, 96), (50, 50), (80, 20), (101, 0)):
            with self.subTest(used=used):
                tree = tile(window(used=used))
                tick = markers(tree)[0]
                self.assertEqual(tick['props']['data-reset-pace'], 50)
                rows = [node for node in nodes(tree) if 'data-window-bar' in node.get('props', {})]
                self.assertEqual(rows[0]['props']['data-window-bar'], left)

    def test_tick_coordinates_share_the_quota_bars_direction(self):
        for hours_left, expected in ((42, 25), (84, 50), (126, 75), (168, 100)):
            with self.subTest(expected=expected):
                tick = markers(tile(window(hours_left=hours_left)))[0]
                self.assertEqual(tick['props']['data-reset-pace'], expected)
                # The tick sits at its own percentage along the same track the
                # quota fill grows from, so the two are read against each other.
                self.assertEqual(tick['props']['style']['left'], f'calc({expected}% - 1px)')
                self.assertEqual(tick['props']['style']['width'], 2)
                self.assertLess(tick['props']['style']['top'], 0)
                self.assertLess(tick['props']['style']['bottom'], 0)

    def test_tick_tooltip_explains_the_time_remaining_scale(self):
        tree = tile(window())
        self.assertIn('50% of reset window remaining', tree['props']['title'])

    def test_stale_provider_and_suppressed_popover_have_no_tick(self):
        w = window()
        expressions = [
            'ProviderRow(' + json.dumps({'id': 'grok', 'provider': {'windows': [w], 'display_stale': True}}) + ')',
            'QuotaCardWindow(' + json.dumps({'w': w, 'providerId': 'grok', 'nowMs': NOW, 'showPace': False}) + ')',
        ]
        for expression in expressions:
            with self.subTest(component=expression.split('(', 1)[0]):
                tree = run(expr=expression, now=NOW, enroll=False)['value']
                self.assertEqual(markers(tree), [])

    def test_fractional_codex_remaining_selects_the_correct_window(self):
        provider = {'display_accounts': [], 'display_remaining': 79.875,
                    'windows': [window('Session', 2.5, used=20.125), window(used=10)]}
        expression = 'ProviderChip(' + json.dumps({'pid': 'openai-codex', 'provider': provider}) + ')'
        tree = run(expr=expression, now=NOW, enroll=False)['value']
        self.assertEqual(len(markers(tree)), 1)
        self.assertEqual(markers(tree)[0]['props']['data-reset-pace'], 50)

    def test_rounding_ties_keep_the_window_selected_by_the_quota_gauge(self):
        provider = {'windows': [window('Current week', 151.2, used=20.4),
                                window('Current session', 0.5, used=20)]}
        expressions = [
            'ProviderChip(' + json.dumps({'pid': 'anthropic', 'provider': provider}) + ')',
            'QuotaChipWithBar()',
        ]
        for expression in expressions:
            with self.subTest(component=expression.split('(', 1)[0]):
                tree = run(expr=expression, data={'age_s': 0, 'providers': {'anthropic': provider}},
                           now=NOW, enroll=False)['value']
                self.assertEqual(len(markers(tree)), 1)
                self.assertAlmostEqual(markers(tree)[0]['props']['data-reset-pace'], 90)

    def test_minimax_video_session_has_a_five_hour_tick(self):
        tree = tile(window('Video Session', 2.5, used=40), 'minimax')
        self.assertEqual(len(markers(tree)), 1)
        self.assertEqual(markers(tree)[0]['props']['data-reset-pace'], 50)
        self.assertEqual(markers(tile(window('Video Session', 2.5), 'unknown')), [])

    def test_weekly_tick_preserves_remaining_quota(self):
        tree = tile(window())
        tick = markers(tree)
        self.assertEqual(len(tick), 1)
        self.assertAlmostEqual(tick[0]['props']['data-reset-pace'], 50)
        rows = [node for node in nodes(tree) if 'data-window-bar' in node.get('props', {})]
        self.assertEqual(rows[0]['props']['data-window-bar'], 96)

    def test_saved_account_window_rows_get_ticks(self):
        windows = [{'label': 'Weekly Limit', 'used': 4,
                    'reset': datetime.fromisoformat(window()['reset_at']).timestamp()}]
        tree = run(expr='AccountWindows(' + json.dumps({'windows': windows, 'providerId': 'grok'}) + ')',
                   now=NOW, enroll=False)['value']
        tick = markers(tree)
        self.assertEqual(len(tick), 1)
        self.assertAlmostEqual(tick[0]['props']['data-reset-pace'], 50)
        rows = [node for node in nodes(tree) if 'data-account-window' in node.get('props', {})]
        self.assertIn('50% of reset window remaining', rows[0]['props']['title'])


if __name__ == '__main__':
    unittest.main()
