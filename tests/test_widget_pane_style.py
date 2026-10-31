"""Pane style toggle: rings (default) vs bars in the provider pane.

Renders the real ProviderRow offline; synthetic windows, no network. The style
is a persisted setting (`paneStyle`), applied by `applyStoredPaneStyle()`; the
harness sets `paneStyleAtom` before rendering.
"""
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from widget_harness import nodes, render, text

LABELS = ('Session', 'Weekly')


def iso(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def provider():
    return {'windows': [
        {'label': 'Session', 'used_percent': 25, 'reset_at': iso(2 * 3600 + 600)},
        {'label': 'Weekly', 'used_percent': 80, 'reset_at': iso(5 * 86400 + 3600)}]}


def with_prop(tree, name):
    return [n for n in nodes(tree) if name in n.get('props', {})]


def bar_labels(tree):
    return [n['props']['data-window-bar'] for n in with_prop(tree, 'data-window-bar')]


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class PaneStyleTests(unittest.TestCase):
    def row(self, **options):
        return render(component='row', id='anthropic', provider=provider(), **options)

    def assert_rings_only(self, tree):
        self.assertTrue(with_prop(tree, 'data-ring'), 'rings style rendered no data-ring gauge')
        self.assertEqual(with_prop(tree, 'data-bars'), [], 'rings style must not render the bars container')
        self.assertEqual(with_prop(tree, 'data-window-bar'), [], 'rings style must not render bar rows')

    def assert_bars(self, tree):
        self.assertTrue(
            with_prop(tree, 'data-bars'),
            'no data-bars container: is paneStyleAtom wired into ProviderRow yet?',
        )
        self.assertEqual(with_prop(tree, 'data-ring'), [], 'bars style must not render ring gauges')
        self.assertEqual(
            sorted(bar_labels(tree)), sorted(LABELS),
            'bars style needs exactly one data-window-bar row per quota window',
        )

    def test_default_style_is_rings(self):
        self.assert_rings_only(self.row())

    def test_bars_style_renders_one_row_per_window(self):
        tree = self.row(paneStyle='bars')
        self.assert_bars(tree)
        shown = text(tree)
        for label in LABELS:
            self.assertIn(label, shown, f'{label} window label missing from the bars pane')

    def test_rings_style_matches_default(self):
        default = self.row()
        explicit = self.row(paneStyle='rings')
        self.assert_rings_only(explicit)
        self.assertEqual(
            len(with_prop(explicit, 'data-ring')), len(with_prop(default, 'data-ring')),
            'explicit rings style must render the same gauges as the default',
        )

    def test_bars_rows_keep_reset_time(self):
        tree = self.row(paneStyle='bars')
        rows = with_prop(tree, 'data-window-bar')
        self.assertEqual(len(rows), len(LABELS), 'expected one bar row per window')
        resets = with_prop(tree, 'data-window-reset')
        self.assertEqual(
            len(resets), len(LABELS),
            'every window with reset_at needs a data-window-reset node (#77)',
        )
        for row in rows:
            inner = [n['props']['children'] for n in nodes(row) if 'data-window-reset' in n.get('props', {})]
            self.assertEqual(
                len(inner), 1,
                f'{row["props"]["data-window-bar"]} bar row must carry its own reset time',
            )
            self.assertTrue(inner[0] and '—' not in inner[0], f'blank reset time: {inner[0]!r}')


if __name__ == '__main__':
    unittest.main()
