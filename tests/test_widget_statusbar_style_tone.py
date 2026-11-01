"""Status-bar chip style: where the severity colour lives.

`statusbarStyle` decides what a provider chip shows: 'both' (gauge AND number,
the default), 'rings' (gauge only) or 'values' (number only). The severity tone
is a colour, so only one element may carry it:

- 'both': the ring (or the tone dot) carries the tone; the number stays
  untinted, otherwise the same severity is encoded twice on one chip.
- 'values': the ring is gone, so the number itself must be readable and tinted
  by the tone -- `toneText(tone)`, which maps muted to a TEXT colour
  (var(--ui-text-tertiary)). It used to use `toneColor(tone)`, whose muted
  value is the 5%-mix STROKE token var(--ui-stroke-tertiary): invisible as a
  label, so a balance-only provider's amount effectively vanished.
- 'rings': there is no number node at all.

Rendered offline through `tests/widget_harness.render(component='status')`
against synthetic providers; no network, no saved state. Assertions read the
implementation's DOM markers (`data-sb-value`, `data-ring`, `data-sb-dot`) and
the `color` key of the value node's inline style, never the layout.
"""
import re
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from widget_harness import nodes, render, text

# (used_percent, expected tone, expected colour): Session left = 100 - used.
SEVERITIES = (
    (96, 'bad', 'var(--ui-red)'),
    (70, 'warn', 'var(--ui-yellow)'),
    (25, 'good', 'var(--ui-green)'),
)
MUTED_TEXT = 'var(--ui-text-tertiary)'      # readable label colour for 'muted'
MUTED_STROKE = 'var(--ui-stroke-tertiary)'  # 5 %-mix stroke token: invisible as text
BALANCE = {'account_balances': [{'currency': 'USD', 'total_balance': '12.00'}]}


def iso(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def provider(used_percent):
    return {'windows': [{'label': 'Session', 'used_percent': used_percent, 'reset_at': iso(3600)}]}


def status_data(record):
    return {'age_s': 0, 'providers': {'anthropic': record}}


def with_prop(tree, name):
    return [n for n in nodes(tree) if name in n.get('props', {})]


def value_nodes(tree):
    return with_prop(tree, 'data-sb-value')


def value_color(tree):
    """The `color` of the data-sb-value node's inline style, or None."""
    vals = value_nodes(tree)
    if not vals:
        return None
    return (vals[0].get('props', {}).get('style') or {}).get('color')


def ring_arc_stroke(ring):
    """The coloured arc of a Ring: its only circle that is not the track."""
    strokes = [
        c['props'].get('stroke') for c in nodes(ring)
        if c.get('type') == 'circle' and c.get('props', {}).get('stroke') != 'var(--ui-stroke-secondary)'
    ]
    return strokes[0] if strokes else None


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class StatusbarStyleToneTests(unittest.TestCase):
    def chip(self, record, style):
        return render(component='status', data=status_data(record), statusbarStyle=style)

    def test_both_style_does_not_tint_the_number(self):
        # The ring already carries the severity: tinting the number as well
        # would encode one tone twice on the same chip.
        for used, tone, colour in SEVERITIES:
            with self.subTest(used_percent=used):
                tree = self.chip(provider(used), 'both')
                self.assertTrue(value_nodes(tree), "'both' style must still show the number")
                self.assertIsNone(
                    value_color(tree),
                    "'both' style must leave the number untinted: the ring carries the severity "
                    "(found color=%r on data-sb-value)" % value_color(tree),
                )
                rings = with_prop(tree, 'data-ring')
                self.assertEqual(
                    [r['props'].get('data-ring') for r in rings], [tone],
                    "'both' style is where the ring must carry the tone %r" % tone,
                )
                self.assertEqual(
                    ring_arc_stroke(rings[0]), colour,
                    "the ring arc must be tinted with the %r severity colour" % tone,
                )
        # A balance-only provider has no severity: its dot is the gauge slot and
        # the amount stays plain text.
        balance = self.chip(BALANCE, 'both')
        self.assertTrue(with_prop(balance, 'data-sb-dot'), "'both' style needs the tone dot for a balance-only provider")
        self.assertIsNone(
            value_color(balance),
            "'both' style must leave a balance amount untinted too (found color=%r)" % value_color(balance),
        )

    def test_rings_style_has_no_number_node(self):
        for record, what in ((provider(25), 'a percentage provider'), (BALANCE, 'a balance-only provider')):
            with self.subTest(case=what):
                tree = self.chip(record, 'rings')
                self.assertEqual(
                    value_nodes(tree), [],
                    "'rings' style must not render a data-sb-value node at all (%s)" % what,
                )
                self.assertTrue(
                    with_prop(tree, 'data-ring') + with_prop(tree, 'data-sb-dot'),
                    "'rings' style must still render its gauge (%s)" % what,
                )

    def test_values_style_tints_a_real_severity_number(self):
        # No ring left: the number is the only severity signal, so it must be
        # tinted with the severity colour.
        for used, tone, colour in SEVERITIES:
            with self.subTest(used_percent=used):
                tree = self.chip(provider(used), 'values')
                self.assertTrue(value_nodes(tree), "'values' style must render a data-sb-value node")
                self.assertTrue(
                    text(value_nodes(tree)[0]).strip(),
                    "'values' style must render a non-empty number, got %r" % text(value_nodes(tree)[0]),
                )
                self.assertEqual(
                    value_color(tree), colour,
                    "'values' style must tint the %r severity number with %r" % (tone, colour),
                )
                self.assertEqual(with_prop(tree, 'data-ring'), [], "'values' style must not render a ring")

    def test_values_style_keeps_a_muted_number_readable(self):
        # The regression: a provider with no severity (balance only) is muted,
        # and toneColor('muted') is a 5 %-mix STROKE token -- invisible as a
        # label. The text helper maps muted to a real text colour.
        tree = self.chip(BALANCE, 'values')
        self.assertTrue(value_nodes(tree), "'values' style must render the balance amount")
        self.assertIn('$12.00', text(tree), "'values' style must show the balance text")
        colour = value_color(tree)
        self.assertNotEqual(
            colour, MUTED_STROKE,
            "'values' style tinted a muted number with the stroke token %r: it is a 5%%-mix "
            "stroke colour and invisible as a label (use toneText, not toneColor)" % MUTED_STROKE,
        )
        self.assertEqual(
            colour, MUTED_TEXT,
            "a muted number must use the readable text colour %r, got %r" % (MUTED_TEXT, colour),
        )

    def test_values_style_never_tints_the_number_with_a_stroke_token(self):
        cases = [('used %d%%' % used, provider(used)) for used, _, _ in SEVERITIES]
        cases.append(('balance only', BALANCE))
        for what, record in cases:
            with self.subTest(case=what):
                colour = value_color(self.chip(record, 'values'))
                self.assertTrue(colour, "'values' style must give the number a colour (%s)" % what)
                self.assertIsNone(
                    re.search(r'--ui-(stroke|border)', colour or ''),
                    "'values' style must tint the number with a text colour, never a stroke/border "
                    "token (%s): %r" % (what, colour),
                )


if __name__ == '__main__':
    unittest.main()
