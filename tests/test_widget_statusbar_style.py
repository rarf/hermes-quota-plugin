"""Status-bar chip style: 'both' (default) | 'rings' | 'values'.

Renders the real `desktop/plugin.js` offline against synthetic providers, no
network. The style is a persisted setting (`statusbarStyle`) that decides what
each quota chip shows: the gauge, the percentage/balance text, or both. The
provider NAME label is always rendered.

Assertions use the implementation's DOM markers, never its layout:
`data-ring` (ring gauge) or `data-sb-dot` (tone dot for providers with no
percentage) mark a gauge; `data-sb-value` marks the percentage/balance text.
"""
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from test_widget_monitor import run, snapshot
from widget_harness import nodes, render, text

STYLES = ('both', 'rings', 'values')
VALUE = '75%'  # 25 % of the Session window used, and the only remaining number
NAME = 'Anthropic'


def iso(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def provider():
    """One percentage chip: Session 25 % used -> '75%' left (the Weekly window
    is less used, so `worstWindow` keeps 75 % as the chip value)."""
    return {'windows': [
        {'label': 'Session', 'used_percent': 25, 'reset_at': iso(2 * 3600 + 600)},
        {'label': 'Weekly', 'used_percent': 10, 'reset_at': iso(5 * 86400 + 360)}]}


BALANCE = {'account_balances': [{'currency': 'USD', 'total_balance': '12.00'}]}


def bar_data(record=None, pid='anthropic'):
    return {'age_s': 0, 'providers': {pid: provider() if record is None else record}}


def with_prop(tree, name):
    return [n for n in nodes(tree) if name in n.get('props', {})]


def gauges(tree):
    """A gauge is a ring (percentage providers) or a plain tone dot (balance)."""
    return with_prop(tree, 'data-ring') + with_prop(tree, 'data-sb-dot')


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class StatusbarStyleTests(unittest.TestCase):
    def bar(self, record=None, **options):
        return render(component='status', data=bar_data(record), **options)

    def worst(self, **options):
        return render(component='status', data=bar_data(), statusMode='worst', **options)

    def test_default_style_is_both(self):
        tree = self.bar()
        self.assertTrue(
            gauges(tree),
            'default style rendered no gauge: expected a data-ring (or data-sb-dot) node',
        )
        self.assertTrue(
            with_prop(tree, 'data-sb-value'),
            "default style rendered no data-sb-value node: is the 'both' style wired into the status bar yet?",
        )
        self.assertIn(VALUE, text(tree), 'default style must show the percentage text')

    def test_rings_style_hides_the_value_text(self):
        tree = self.bar(statusbarStyle='rings')
        self.assertTrue(gauges(tree), "'rings' style must still render the gauge (data-ring / data-sb-dot)")
        self.assertEqual(
            with_prop(tree, 'data-sb-value'), [],
            "'rings' style must not render a data-sb-value node: is statusbarStyleAtom wired into the chip yet?",
        )
        self.assertNotIn(VALUE, text(tree), "'rings' style must not render the percentage text")

    def test_values_style_hides_the_gauge(self):
        tree = self.bar(statusbarStyle='values')
        self.assertTrue(
            with_prop(tree, 'data-sb-value'),
            "'values' style must render a data-sb-value node: is statusbarStyleAtom wired into the chip yet?",
        )
        self.assertEqual(with_prop(tree, 'data-ring'), [], "'values' style must not render a ring gauge")
        self.assertEqual(with_prop(tree, 'data-sb-dot'), [], "'values' style must not render a tone dot")
        self.assertIn(VALUE, text(tree), "'values' style must show the percentage text")

    def test_explicit_both_matches_default(self):
        default = self.bar()
        explicit = self.bar(statusbarStyle='both')
        self.assertEqual(
            [n['props']['data-ring'] for n in with_prop(explicit, 'data-ring')],
            [n['props']['data-ring'] for n in with_prop(default, 'data-ring')],
            "explicit 'both' must render the same ring gauges as the default",
        )
        self.assertEqual(
            len(with_prop(explicit, 'data-sb-value')), len(with_prop(default, 'data-sb-value')),
            "explicit 'both' must render the same value nodes as the default",
        )
        self.assertEqual(text(explicit), text(default), "explicit 'both' must render the same text as the default")

    def test_worst_mode_honours_the_style(self):
        rings = self.worst(statusbarStyle='rings')
        self.assertTrue(gauges(rings), "'worst' mode 'rings' style must render its gauge")
        self.assertEqual(
            with_prop(rings, 'data-sb-value'), [],
            "'worst' mode 'rings' style must not render a data-sb-value node",
        )
        self.assertNotIn(VALUE, text(rings), "'worst' mode 'rings' style must not render the percentage text")
        shown = self.worst(statusbarStyle='values')
        self.assertTrue(
            with_prop(shown, 'data-sb-value'),
            "'worst' mode 'values' style must render a data-sb-value node",
        )
        self.assertEqual(with_prop(shown, 'data-ring'), [], "'worst' mode 'values' style must not render a ring gauge")
        self.assertIn(VALUE, text(shown), "'worst' mode 'values' style must show the percentage text")

    def test_provider_name_label_is_always_rendered(self):
        for style in STYLES:
            with self.subTest(style=style):
                self.assertIn(
                    NAME, text(self.bar(statusbarStyle=style)),
                    f'"{style}" style dropped the provider name label',
                )
            with self.subTest(mode='worst', style=style):
                self.assertIn(
                    NAME, text(self.worst(statusbarStyle=style)),
                    f'"worst" mode with "{style}" style dropped the provider name label: '
                    'only the percentage should be conditional on the style, never the name',
                )

    def test_balance_only_provider_uses_the_tone_dot(self):
        both = self.bar(BALANCE)
        self.assertTrue(with_prop(both, 'data-sb-dot'), "'both' style needs a data-sb-dot gauge for a balance-only provider")
        self.assertEqual(with_prop(both, 'data-ring'), [], 'a provider with no percentage must not render a ring')
        self.assertTrue(with_prop(both, 'data-sb-value'), "'both' style must also show the balance text")
        self.assertIn('$12.00', text(both), "'both' style must show the balance value")
        rings = self.bar(BALANCE, statusbarStyle='rings')
        self.assertTrue(with_prop(rings, 'data-sb-dot'), "'rings' style must keep the tone dot")
        self.assertEqual(with_prop(rings, 'data-sb-value'), [], "'rings' style must hide the balance text")
        shown = self.bar(BALANCE, statusbarStyle='values')
        self.assertTrue(with_prop(shown, 'data-sb-value'), "'values' style must render the balance text")
        self.assertEqual(with_prop(shown, 'data-sb-dot'), [], "'values' style must not render the tone dot")

    def test_values_style_keeps_the_number_readable(self):
        # The ring's colour is the severity signal. 'values' mode drops the
        # ring, so the number carries it -- but only for real severities: the
        # muted tone's token is a 5%-mix stroke colour, invisible as a label.
        # A balance-only provider is muted, so its amount must stay readable.
        for record, expected in ((provider(), 'var(--ui-green)'), (BALANCE, 'var(--ui-text-tertiary)')):
            with self.subTest(expected=expected):
                tree = self.bar(record, statusbarStyle='values')
                value = with_prop(tree, 'data-sb-value')[0]
                self.assertEqual(
                    (value.get('props', {}).get('style') or {}).get('color'),
                    expected,
                    "'values' mode must tint a severity number and keep a muted number readable",
                )

    def test_stored_style_is_read_on_load(self):
        # Boot path, not the test seam: the widget reads ctx.storage itself.
        out = run(component='status', data=bar_data(), monitor=snapshot(),
                  storage={'statusbarStyle': 'rings'})
        tree = out['tree']
        self.assertEqual(
            with_prop(tree, 'data-sb-value'), [],
            "the saved 'statusbarStyle' must be applied on load: 'rings' still rendered a data-sb-value node",
        )
        self.assertNotIn(VALUE, text(tree), "the saved 'statusbarStyle' = 'rings' must hide the percentage text")
        self.assertTrue(gauges(tree), "the saved 'statusbarStyle' = 'rings' must still render the gauge")

    def test_settings_expose_the_style_control(self):
        tree = run(component='settings', data=bar_data(), monitor=snapshot())['tree']
        offered = [
            {o.get('id') for o in n['props'].get('options') or []}
            for n in nodes(tree) if n.get('type') == 'segmented'
        ]
        wanted = set(STYLES)
        self.assertTrue(
            any(ids >= wanted for ids in offered),
            f'Quota Settings needs a SegmentedControl offering {sorted(wanted)}; found {[sorted(i) for i in offered]}',
        )


if __name__ == '__main__':
    unittest.main()
