"""Layout A for the monitor surfaces: Live, Health, Usage, Kanban, Settings.

Real desktop/plugin.js through tests/widget_monitor_renderer.cjs, SYNTHETIC
payloads only (the same fixtures as test_widget_monitor). Asserts the approved
"compact rows" grammar: flat KPI strip, quiet section headers, hairline rows
with a right-aligned status word, secondary charts/diagnostics behind
disclosure, and Settings groups as an accordion. Data and actions must survive.
"""
import json
import shutil
import unittest

from test_widget_monitor import NOW, quota_payload, run, snapshot
from widget_harness import nodes, text


def attr(tree, name):
    return [n for n in nodes(tree) if name in (n.get('props') or {})]


def style(node):
    return (node.get('props') or {}).get('style') or {}


def pane(view, storage=None, monitor=None):
    return run(component='pane', data=quota_payload(), monitor=monitor or snapshot(),
               storage={'paneView': view, **(storage or {})})['tree']


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class SharedGrammarTests(unittest.TestCase):
    def test_kpis_are_a_flat_strip_not_cards(self):
        kpis = attr(pane('live'), 'data-kpi')
        self.assertGreaterEqual(len(kpis), 3)
        for k in kpis:
            self.assertNotIn('background', style(k))
            self.assertNotIn('border', style(k))

    def test_status_words_have_no_fill(self):
        tree = pane('health')
        states = attr(tree, 'data-status-word')
        self.assertTrue(states)
        for s in states:
            self.assertNotIn('background', style(s))

    def test_rows_have_no_gradient_wash(self):
        for view in ('live', 'health', 'kanban'):
            with self.subTest(view=view):
                for n in attr(pane(view), 'data-row'):
                    self.assertNotIn('gradient', json.dumps(style(n)))

    def test_summary_is_one_quiet_line(self):
        strip = attr(pane('live'), 'data-summary-strip')[0]
        self.assertIn('1 working', text(strip))
        self.assertIn('lowest 4% left', text(strip))
        for item in attr(strip, 'data-summary'):
            fills = [style(n).get('background') for n in nodes(item)]
            self.assertTrue(all(f in (None, 'transparent') for f in fills), fills)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class LiveTests(unittest.TestCase):
    def test_charts_move_behind_disclosure(self):
        tree = pane('live')
        disc = [n for n in attr(tree, 'data-disclosure') if n['props']['data-disclosure'] == 'activity']
        self.assertEqual(len(disc), 1)
        self.assertEqual(disc[0]['type'], 'details')
        self.assertFalse(disc[0]['props'].get('open'))
        # The exact chart data is still there, inside the disclosure.
        self.assertTrue([n for n in nodes(disc[0]) if n.get('props', {}).get('data-sparkline')])

    def test_attention_comes_before_sessions(self):
        s = text(pane('live'))
        self.assertLess(s.index('Needs attention (1)'), s.index('Refactor the parser'))

    def test_every_session_names_its_state_on_the_first_line(self):
        tree = pane('live')
        heads = attr(tree, 'data-session-head')
        self.assertGreaterEqual(len(heads), 3)
        states = sorted(n['props']['data-state'] for h in heads for n in attr(h, 'data-state'))
        self.assertIn('working', states)
        self.assertIn('needs_approval', states)
        self.assertIn('waiting', states)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class HealthTests(unittest.TestCase):
    def test_models_before_accounts_and_four_counts(self):
        tree = pane('health')
        titles = [text(n) for n in attr(tree, 'data-section-title')]
        self.assertLess(titles.index('Models'), titles.index('Accounts'))
        labels = [text(k) for k in attr(tree, 'data-kpi')]
        self.assertEqual(len(labels), 4)
        self.assertTrue(any('idle' in label for label in labels))


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class UsageTests(unittest.TestCase):
    def tree(self):
        return pane('usage', storage={'budgetDaily': 10, 'budgetMonthly': 40})

    def test_kpis_lead_then_budget(self):
        s = text(self.tree())
        self.assertLess(s.index('This month'), s.index('Budget'))

    def test_exact_chart_values_behind_disclosure(self):
        tree = self.tree()
        table = [n for n in attr(tree, 'data-disclosure') if n['props']['data-disclosure'] == 'chart-values']
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]['type'], 'details')
        s = text(table[0])
        for value in ('$12.50', '$30.00', '$7.50'):
            self.assertIn(value, s)

    def test_model_and_profile_breakdowns_share_a_grid(self):
        grid = attr(self.tree(), 'data-usage-breakdown')
        self.assertEqual(len(grid), 1)
        self.assertEqual(style(grid[0]).get('display'), 'grid')
        s = text(grid[0])
        self.assertIn('Top models · month', s)
        self.assertIn('Top profiles · month', s)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class KanbanTests(unittest.TestCase):
    def test_lanes_are_a_grid_with_plain_headers(self):
        tree = pane('kanban')
        lanes = attr(tree, 'data-kanban-lanes')
        self.assertEqual(len(lanes), 1)
        self.assertEqual(style(lanes[0]).get('display'), 'grid')
        for head in attr(tree, 'data-lane-head'):
            self.assertNotIn('background', style(head))

    def test_copy_action_only_where_a_command_exists(self):
        tree = pane('kanban')
        copies = [n['props']['data-card-copy'] for n in attr(tree, 'data-card-copy')]
        self.assertEqual(sorted(copies), ['t_blk1', 't_run1'])


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class SettingsTests(unittest.TestCase):
    def test_groups_are_an_accordion_with_the_first_open(self):
        tree = run(component='settings', data=quota_payload())['tree']
        groups = attr(tree, 'data-settings-card')
        self.assertGreaterEqual(len(groups), 5)
        self.assertTrue(all(g['type'] == 'details' for g in groups))
        self.assertTrue(groups[0]['props'].get('open'))
        self.assertFalse(any(g['props'].get('open') for g in groups[1:]))

    def test_every_preference_survives(self):
        s = text(run(component='settings', data=quota_payload())['tree'])
        for label in ('Show status bar indicator', 'Status bar mode', 'Show docked quota pane', 'Pane detail',
                      'Reset format', 'Refresh interval', 'Enabled providers', 'Agent monitor', 'Accounts tab',
                      'History range', 'Live refresh', 'Daily', 'Monthly', 'Notifications', 'Budget 80% / 100%',
                      'Agent finished — your turn', 'Context window filling up'):
            self.assertIn(label, s)


if __name__ == '__main__':
    unittest.main()
