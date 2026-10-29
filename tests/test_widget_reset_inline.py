"""Each pane ring shows its reset time in both pane-detail modes (#77).

Renders the real ProviderRow offline; synthetic windows, no network.
"""
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from widget_harness import nodes, render


def iso(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def resets(tree):
    return {n['props']['children'] for n in nodes(tree) if 'data-window-reset' in n.get('props', {})}


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class WindowResetInBothModesTests(unittest.TestCase):
    def test_reset_under_every_ring_in_clean_and_dense(self):
        provider = {'windows': [
            {'label': '5h', 'used_percent': 25, 'reset_at': iso(2 * 3600 + 600)},
            {'label': 'Weekly', 'used_percent': 80, 'reset_at': iso(5 * 86400 + 3600)}]}
        for mode in ('clean', 'dense'):
            with self.subTest(mode=mode):
                shown = resets(render(component='row', mode=mode, id='anthropic', provider=provider))
                self.assertEqual(len(shown), 2, shown)
                self.assertTrue(all(s and '—' not in s for s in shown), shown)

    def test_window_without_reset_renders_no_reset_line(self):
        provider = {'windows': [{'label': 'Monthly', 'used_percent': 40}]}
        for mode in ('clean', 'dense'):
            with self.subTest(mode=mode):
                self.assertEqual(resets(render(component='row', mode=mode, id='anthropic', provider=provider)), set())


if __name__ == '__main__':
    unittest.main()
