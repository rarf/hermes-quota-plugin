"""Quota card follows the persisted pane style: rings (default) vs bars.

Clicking a quota chip in the status bar opens `QuotaCard`; it has no style
switch of its own, so it must render whatever "Pane style" the pane uses --
otherwise picking bars leaves donut rings in the popup. Renders the real card
offline through `tests/widget_renderer.cjs` (component='card').

The two styles lay the window tiles out differently, and both directions are
pinned here: rings are small 32 px gauges and stay LEFT TO RIGHT in the card's
wrapping row (fixed 84 px tiles), while bars must stack TOP TO BOTTOM in a
full-width column, exactly like the pane's `WindowBarRow`. A card that flipped
either mode fails.
"""
import re
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from widget_harness import nodes, render, text

LABELS = ('Session', 'Weekly')
# 25 % and 80 % used -> 75 % and 20 % left.
PERCENTS = ('75%', '20%')
# Pane bars read "75% left"; the card bars must read the same.
PANE_PERCENTS = ('75% left', '20% left')
TONE_COLORS = ('var(--ui-green)', 'var(--ui-yellow)')


def iso(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def payload():
    return {
        'age_s': 12,
        'providers': {'anthropic': {
            'plan': 'Pro',
            'windows': [
                {'label': 'Session', 'used_percent': 25, 'reset_at': iso(2 * 3600 + 600)},
                {'label': 'Weekly', 'used_percent': 80, 'reset_at': iso(5 * 86400 + 3600)},
            ],
            'details': [],
        }},
    }


def with_prop(tree, name):
    return [n for n in nodes(tree) if name in n.get('props', {})]


def prop_values(tree, name):
    return [n['props'][name] for n in with_prop(tree, name)]


def style_of(node):
    return (node.get('props') or {}).get('style') or {}


def windows_container(tree):
    """The card div that owns the per-window tiles (it carries no marker)."""
    for node in nodes(tree):
        children = (node.get('props') or {}).get('children')
        if isinstance(children, list) and any(
            isinstance(child, dict) and 'data-card-window' in (child.get('props') or {})
            for child in children
        ):
            return node
    raise AssertionError('the card rendered no container around its data-card-window tiles')


def label_row(tile):
    """The label / % row of a bar tile: its first child."""
    return tile['props']['children'][0]


def reset_texts(tree):
    """The reset line of each card tile, as rendered."""
    out = []
    for node in with_prop(tree, 'data-window-reset'):
        out.append(text(node['props']['children']))
    return out


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class QuotaCardPaneStyleTests(unittest.TestCase):
    def card(self, **options):
        return render(component='card', id='anthropic', data=payload(), **options)

    def assert_rings(self, tree):
        tiles = with_prop(tree, 'data-card-window')
        self.assertEqual(
            [t['props']['data-card-window'] for t in tiles], list(LABELS),
            'the card must still render one data-card-window tile per window',
        )
        self.assertEqual(
            len(with_prop(tree, 'data-ring')), len(LABELS),
            'rings style must render one data-ring gauge per window in the card',
        )
        for tile in tiles:
            self.assertTrue(
                with_prop(tile, 'data-ring'),
                f'{tile["props"]["data-card-window"]} tile lost its ring gauge',
            )
            self.assertEqual(with_prop(tile, 'data-window-bar'), [], 'rings tile must not be a bar tile')
        self.assertEqual(with_prop(tree, 'data-window-bar'), [], 'rings style must not render card bar tiles')
        shown = text(tree)
        for label in LABELS + PERCENTS:
            self.assertIn(label, shown, f'{label} missing from the rings card')

    def assert_bars(self, tree):
        tiles = with_prop(tree, 'data-card-window')
        self.assertEqual(
            [t['props']['data-card-window'] for t in tiles], list(LABELS),
            'bars style must keep the data-card-window tile attribute (#90)',
        )
        self.assertEqual(
            with_prop(tree, 'data-ring'), [],
            'bars style must not render the hardcoded rings: is paneStyleAtom wired into QuotaCardWindow?',
        )
        self.assertEqual(
            sorted(prop_values(tree, 'data-window-bar')), sorted(LABELS),
            'bars style needs exactly one data-window-bar tile per quota window',
        )
        for tile in tiles:
            self.assertTrue(
                with_prop(tile, 'data-window-bar'),
                f'{tile["props"]["data-card-window"]} tile did not become a bar tile',
            )
            self.assertEqual(with_prop(tile, 'data-ring'), [], 'bars tile must not contain a ring')
        shown = text(tree)
        for label in LABELS + PERCENTS:
            self.assertIn(label, shown, f'{label} missing from the bars card')

    def test_default_style_is_rings(self):
        # Non-vacuous: a card that always drew bars would fail here.
        self.assert_rings(self.card())

    def test_explicit_rings_matches_default(self):
        self.assert_rings(self.card(paneStyle='rings'))
        self.assertEqual(
            len(with_prop(self.card(paneStyle='rings'), 'data-ring')),
            len(with_prop(self.card(), 'data-ring')),
            'explicit rings style must render the same gauges as the default',
        )

    def test_bars_style_replaces_rings_with_bars(self):
        self.assert_bars(self.card(paneStyle='bars'))

    def test_ring_gauge_geometry_is_untouched(self):
        # This task is a layout fix in the bars branch: the rings (size 32,
        # stroke 3) must not be resized or restyled while adding the column.
        rings = with_prop(self.card(paneStyle='rings'), 'data-ring')
        self.assertEqual(len(rings), len(LABELS))
        for ring in rings:
            self.assertEqual(style_of(ring).get('width'), 32, f'ring size changed: {style_of(ring)}')
            self.assertEqual(style_of(ring).get('height'), 32, f'ring size changed: {style_of(ring)}')
            circles = [n for n in nodes(ring) if n.get('type') == 'circle']
            self.assertTrue(circles, 'the ring lost its svg circles')
            for circle in circles:
                self.assertEqual(circle['props'].get('strokeWidth'), 3, 'ring stroke width changed')

    def test_rings_windows_stay_left_to_right(self):
        # The user explicitly wants rings side by side in the popup: small
        # gauges in the card's wrapping row, one fixed 84 px column each.
        for pane_style in (None, 'rings'):
            tree = self.card() if pane_style is None else self.card(paneStyle=pane_style)
            container = windows_container(tree)
            style = style_of(container)
            self.assertEqual(style.get('display'), 'flex',
                             f'rings card window container lost its flex row: {style}')
            self.assertEqual(style.get('flexWrap'), 'wrap',
                             f'rings card tiles must still wrap left to right: {style}')
            self.assertEqual(style.get('gap'), '8px 4px',
                             f'rings card window gap changed: {style}')
            self.assertNotEqual(
                style.get('flexDirection'), 'column',
                'rings must stay LEFT TO RIGHT -- the vertical column belongs to bars only',
            )
            tiles = with_prop(container, 'data-card-window')
            self.assertEqual([t['props']['data-card-window'] for t in tiles], list(LABELS))
            for tile in tiles:
                self.assertEqual(
                    style_of(tile).get('width'), 84,
                    f'{tile["props"]["data-card-window"]} rings tile must keep its fixed 84 px column',
                )

    def test_bars_windows_stack_top_to_bottom(self):
        # Regression test for the complaint: the popup laid its bars out left
        # to right in 84 px columns while the pane stacked them top to bottom.
        container = windows_container(self.card(paneStyle='bars'))
        style = style_of(container)
        self.assertEqual(style.get('display'), 'flex', f'bars container is not a flex box: {style}')
        self.assertEqual(
            style.get('flexDirection'), 'column',
            'bars must stack TOP TO BOTTOM like the pane, not wrap left to right '
            f'(and not left to right in 84 px columns): {style}',
        )
        self.assertNotIn('flexWrap', style,
                         f'a bars column must not wrap into side-by-side columns: {style}')
        self.assertEqual(style.get('gap'), 9,
                         f'bars column gap must match the pane bars container: {style}')

    def test_bars_tiles_fill_the_card_width(self):
        # No fixed 84 px column: the tile must fill the column so the bar
        # spans the card, exactly like WindowBarRow in the pane.
        for tile in with_prop(self.card(paneStyle='bars'), 'data-window-bar'):
            style = style_of(tile)
            self.assertNotIn(
                'width', style,
                f'{tile["props"]["data-window-bar"]} bars tile is still a fixed-width column: {style}',
            )
        # Non-vacuity: the rings tiles in the same card keep their 84 px column.
        for tile in with_prop(self.card(paneStyle='rings'), 'data-card-window'):
            self.assertEqual(style_of(tile).get('width'), 84,
                             'the rings tiles are the ones that stay 84 px wide')

    def test_bars_tile_keeps_label_and_percent(self):
        tree = self.card(paneStyle='bars')
        for tile in with_prop(tree, 'data-window-bar'):
            label = tile['props']['data-window-bar']
            self.assertIn(label, text(tile['props']['children']), f'{label} bar tile lost its label')
            self.assertRegex(
                text(tile['props']['children']), r'\d+%',
                f'{label} bar tile lost its remaining % value',
            )

    def test_bars_row_reads_like_the_pane_window_bar_row(self):
        # The card's bars tile is a compact copy of the pane's WindowBarRow:
        # UI.meta type, "75% left" on the right, toneColor for the %.
        tiles = with_prop(self.card(paneStyle='bars'), 'data-window-bar')
        self.assertEqual(len(tiles), len(LABELS))
        for tile, label, expected, color in zip(tiles, LABELS, PANE_PERCENTS, TONE_COLORS):
            label_cell, pct_cell = label_row(tile)['props']['children']
            self.assertEqual(label_cell['props']['children'], label)
            self.assertEqual(
                pct_cell['props']['children'], expected,
                f'{label} must read "{expected}" like the pane, not a bare percentage',
            )
            self.assertEqual(
                style_of(pct_cell).get('color'), color,
                f'{label} % must be coloured with toneColor(tone), like the pane bars row',
            )
            # UI.meta is 0.6875rem; the dimmer UI.tiny (0.625rem) was the old card scale.
            self.assertEqual(style_of(label_cell).get('fontSize'), '0.6875rem',
                             f'{label} label must use the pane row type scale (UI.meta)')
            self.assertEqual(style_of(pct_cell).get('fontSize'), '0.6875rem',
                             f'{label} % must use the pane row type scale (UI.meta)')

    def test_bars_tiles_keep_relative_reset_and_title(self):
        tree = self.card(paneStyle='bars')
        tiles = with_prop(tree, 'data-window-bar')
        self.assertEqual(len(tiles), len(LABELS))
        for tile in tiles:
            title = tile['props']['title']
            self.assertIn('% left', title, f'title lost its "% left" text: {title!r}')
            # The card counts down from `nowMs`; an absolute reset would read
            # "in 5 days" / a date instead of a span.
            self.assertRegex(title, r'resets in (?:<|\d)', f'title lost its relative countdown: {title!r}')
            resets = reset_texts(tile)
            self.assertEqual(len(resets), 1, f'{tile["props"]["data-window-bar"]} needs its reset line')
            # The pane's WindowBarRow renders the bare countdown; the card bars
            # row now matches it (the rings branch keeps its own "↻ " form).
            self.assertFalse(resets[0].startswith('↻ '), f'card bars reset must match the pane: {resets[0]!r}')
            self.assertRegex(resets[0], r'^\d', f'reset line is not a countdown: {resets[0]!r}')

    def test_bars_tile_fills_to_the_remaining_percent(self):
        # A marker alone proves nothing: the tile must paint a bar whose width is
        # the remaining share and whose fill follows the severity tone.
        expected = [('75%', 'var(--ui-green)'), ('20%', 'var(--ui-yellow)')]
        tiles = with_prop(self.card(paneStyle='bars'), 'data-window-bar')
        self.assertEqual(len(tiles), len(expected))
        for tile, (width, color) in zip(tiles, expected):
            fills = [
                n for n in nodes(tile)
                if (n.get('props', {}).get('style') or {}).get('width') == width
            ]
            self.assertEqual(
                len(fills), 1,
                f'{tile["props"]["data-window-bar"]} needs one bar filled to {width}',
            )
            self.assertEqual(fills[0]['props']['style']['background'], color,
                             'bar fill must follow toneColor(tone), like the pane bars')


if __name__ == '__main__':
    unittest.main()
