"""Pane style and the 'Pane detail' hint must agree.

The 'Pane detail' setting row's clean-mode hint describes what the pane draws,
so it has to follow the separate `paneStyle` toggle: rings show donut gauges,
bars show horizontal bar rows. It used to say "Gauges and reset times" even
with `paneStyle = 'bars'`, telling the user to look for a gauge the pane does
not draw.

Rendered offline through `tests/widget_monitor_renderer.cjs`: the synchronous
`widget_harness.render()` has no `settings` component (it falls back to the
pane), so `run(component='settings', storage={...})` is used and the tree is
read with `widget_harness.nodes()`. `storage` is the boot path -- the widget
applies the saved `paneStyle` / `paneDetail` itself at register time, so these
tests never poke an atom directly.

A SettingRow renders as `{data-setting-row}` with two children: a column
[label, hint] and the control. The row is identified by its control's option
ids (`clean` / `dense`), never by translated text, so the same helper works in
every bundle; the English label is asserted once, on the en bundle.
"""
import re
import shutil
import unittest

from test_widget_monitor import run, snapshot
from widget_harness import nodes, text

# Every bundle the widget ships (tests/test_widget_i18n.py LOCALES).
LOCALES = ('en', 'tr', 'de', 'es', 'fr', 'ru', 'ja', 'zh', 'zh-hant', 'ar')
PANE_DETAIL_LABEL = 'Pane detail'  # I18N_EN.paneDetailLabel
PANE_DETAIL_OPTIONS = ['clean', 'dense']


def setting_rows(tree):
    """Every SettingRow: {'label', 'hint', 'options'} (options = control ids)."""
    rows = []
    for node in nodes(tree):
        if 'data-setting-row' not in node.get('props', {}):
            continue
        children = node['props'].get('children') or []
        column = (children[0] if children else None) or {}
        parts = column.get('props', {}).get('children') or []
        if not parts:
            continue
        options = []
        for inner in nodes(children[1] if len(children) > 1 else None):
            options.extend(
                o.get('id') for o in inner.get('props', {}).get('options') or [] if isinstance(o, dict)
            )
        rows.append({
            'label': text(parts[0]),
            'hint': text(parts[1]) if len(parts) > 1 else '',
            'options': options,
        })
    return rows


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class PaneStyleHintTests(unittest.TestCase):
    def row(self, pane_style, detail='clean', locale='en'):
        storage = {'paneDetail': detail}
        if pane_style is not None:
            storage['paneStyle'] = pane_style
        out = run(component='settings', monitor=snapshot(), storage=storage, locale=locale)
        rows = [r for r in setting_rows(out['tree']) if r['options'] == PANE_DETAIL_OPTIONS]
        self.assertEqual(
            len(rows), 1,
            'expected exactly one SettingRow whose control offers %r (%s), found %r'
            % (PANE_DETAIL_OPTIONS, pane_style, [r['options'] for r in setting_rows(out['tree'])][:6]),
        )
        return rows[0]

    def hint(self, pane_style, detail='clean', locale='en'):
        return self.row(pane_style, detail, locale)['hint']

    def test_english_row_is_labelled_pane_detail(self):
        self.assertEqual(
            self.row('rings')['label'], PANE_DETAIL_LABEL,
            'the clean/dense row must be the "Pane detail" setting',
        )

    def test_rings_clean_hint_names_the_gauge(self):
        hint = self.hint('rings')
        self.assertRegex(
            hint, r'(?i)\b(gauge|ring)s?\b',
            "with paneStyle='rings' the clean hint must name the gauge the pane draws, got %r" % hint,
        )

    def test_bars_clean_hint_names_the_bars(self):
        hint = self.hint('bars')
        self.assertRegex(
            hint, r'(?i)\bbars?\b',
            "with paneStyle='bars' the clean hint must name the bars the pane draws, got %r" % hint,
        )

    def test_bars_clean_hint_never_describes_a_gauge(self):
        # The exact regression: bars pane, hint still said "Gauges and reset
        # times". A gauge word here sends the user looking for rings that the
        # selected style never renders.
        hint = self.hint('bars')
        self.assertIsNone(
            re.search(r'(?i)gauges?', hint),
            "with paneStyle='bars' the clean hint must never mention a gauge, got %r" % hint,
        )
        self.assertIsNone(
            re.search(r'(?i)\brings?\b', hint),
            "with paneStyle='bars' the clean hint must never mention rings, got %r" % hint,
        )

    def test_clean_hint_changes_with_the_pane_style(self):
        rings, bars = self.hint('rings'), self.hint('bars')
        self.assertTrue(rings.strip() and bars.strip(), 'clean hints must not be empty: %r / %r' % (rings, bars))
        self.assertNotEqual(
            rings, bars,
            'the clean hint must follow paneStyle: rings and bars both read %r' % rings,
        )

    def test_default_pane_style_uses_the_rings_hint(self):
        self.assertEqual(
            self.hint(None), self.hint('rings'),
            "the default paneStyle is 'rings', so the default clean hint must be the rings one",
        )

    def test_clean_and_dense_hints_describe_different_content(self):
        for style in ('rings', 'bars'):
            with self.subTest(paneStyle=style):
                clean, dense = self.hint(style, 'clean'), self.hint(style, 'dense')
                self.assertTrue(clean.strip(), 'clean hint is empty for %r' % style)
                self.assertTrue(dense.strip(), 'dense hint is empty for %r' % style)
                self.assertNotEqual(
                    clean, dense,
                    'clean and dense describe different content, so their hints must differ '
                    '(both %r for %r)' % (clean, style),
                )

    def test_every_locale_changes_the_hint_with_the_pane_style(self):
        # Language-agnostic: no wording is assumed, only that the bars entry
        # exists and is not the rings sentence copied over. A missing key also
        # surfaces here as the raw key name.
        for locale in LOCALES:
            with self.subTest(locale=locale):
                rings = self.hint('rings', locale=locale)
                bars = self.hint('bars', locale=locale)
                self.assertTrue(rings.strip(), '%s: empty rings clean hint' % locale)
                self.assertTrue(bars.strip(), '%s: empty bars clean hint' % locale)
                self.assertNotIn('paneDetail', rings, '%s: raw i18n key in the clean hint: %r' % (locale, rings))
                self.assertNotIn('paneDetail', bars, '%s: raw i18n key in the clean hint: %r' % (locale, bars))
                self.assertNotEqual(
                    rings, bars,
                    '%s: the bars pane must not reuse the rings clean hint %r' % (locale, rings),
                )


if __name__ == '__main__':
    unittest.main()
