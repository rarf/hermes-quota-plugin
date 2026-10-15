"""Pane multi-card display: per-card buttons, tone dots, and detail switching.

Runs the real desktop/plugin.js through tests/widget_multicard.cjs — the
shared harness with a stateful useState and a click step — against cache
fixtures shaped exactly like the rows the backend emits for a saved pool.
Offline: every value is synthetic, no credential and no network is touched.

The light contract mirrors the task book: remaining >40 green, 15-40 yellow,
<=15 red, a read failure grey.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from widget_harness import text, tooltip_text

ROOT = Path(__file__).resolve().parent.parent
DRIVER = Path(__file__).resolve().parent / 'widget_multicard.cjs'

LABELS = ('alpha.gmail', 'beta.gmail', 'gamma.gmail', 'delta.mail')

# toneColor() values of the shared light contract.
GOOD, WARN, BAD = 'var(--ui-accent)', 'var(--ui-yellow)', 'var(--ui-red)'
GREY = 'var(--ui-stroke-tertiary)'  # toneColor('muted')


def render(**options):
    p = subprocess.run(
        ['node', str(DRIVER)], input=json.dumps(options), text=True,
        capture_output=True, timeout=20,
    )
    if p.returncode:
        raise AssertionError(p.stderr)
    return json.loads(p.stdout)


def card(account_label, used=None, *, account_id=None, unavailable=None):
    """One cache record in the shape quota_cache emits for an account row."""
    record = {
        'label': f'commandcode · {account_label}',
        'account_label': account_label,
        'windows': [] if used is None else [
            {'label': '5h', 'used_percent': used, 'reset_at': '2099-01-01T00:00:00Z'}],
    }
    if account_id:
        record['provider'] = 'commandcode'
        record['account_id'] = account_id
    if unavailable:
        record['unavailable_reason'] = unavailable
    return record


def pool_data(percents, labels=LABELS):
    """A four-row saved-pool provider: base row + three account rows."""
    providers = {}
    for index, (used, label) in enumerate(zip(percents, labels)):
        record = card(
            label, used,
            account_id=None if index == 0 else f'r{index + 1}',
            unavailable='fetch-error:OSError' if used is None else None,
        )
        if index == 0:
            record['label'] = 'commandcode'
        providers['commandcode' if index == 0 else f'commandcode:r{index + 1}'] = record
    return {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': providers}


def single_row_data(unavailable='fetch-error:OSError', provider='deepseek'):
    """One lone provider row carrying an unavailable reason."""
    return {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': {
        provider: {'windows': [{'label': 'Weekly', 'used_percent': 10.0}],
                   'unavailable_reason': unavailable}}}


def pool_data_with_unconfigured_sibling():
    """Three-row pool: base + one healthy sibling + one no-credentials row."""
    providers = {}
    base = card('alpha.gmail', 50.0)
    base['label'] = 'commandcode'
    providers['commandcode'] = base
    providers['commandcode:r2'] = card(
        'beta.gmail', None, account_id='r2', unavailable='no-credentials')
    providers['commandcode:r3'] = card('gamma.gmail', 90.0, account_id='r3')
    return {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': providers}


def flat(tree):
    """Every node in document order; scalar leaves (text) included."""
    out = []

    def visit(node):
        if isinstance(node, dict):
            out.append(node)
            visit(node.get('props', {}).get('children'))
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif node is not None and not isinstance(node, bool):
            out.append(node)

    visit(tree)
    return out


def status_parts(tree):
    """(buttons, lights) of a rendered status-bar tree, document order."""
    nodes = [n for n in flat(tree) if isinstance(n, dict)]
    buttons = [n for n in nodes if n.get('type') == 'button']
    lights = [n for n in nodes if n.get('type') == 'span'
              and 'h-1.5 w-1.5 rounded-full' in n.get('props', {}).get('className', '')]
    return buttons, lights


def light_colors(lights):
    return [light['props']['style']['background'] for light in lights]


def card_buttons(tree):
    """Pane card buttons: a button whose children carry a status-dot tone."""
    found = []
    for n in flat(tree):
        if not isinstance(n, dict) or n.get('type') != 'button':
            continue
        children = n.get('props', {}).get('children') or []
        if any(isinstance(c, dict) and c.get('props', {}).get('data-tone') for c in children):
            found.append(n)
    return found


def box_with_text(tree, marker):
    """The elevated card box of the provider whose display name is marker."""
    for n in flat(tree):
        if (isinstance(n, dict) and n.get('type') == 'div'
                and 'bg-(--ui-bg-elevated)' in n.get('props', {}).get('className', '')
                and marker in text(n)):
            return n
    raise AssertionError(f'no elevated card box for {marker!r}')


def path_types(tree, marker):
    """Types from the root down to the dict whose children is the marker string."""
    def walk(node, types):
        if isinstance(node, dict):
            here = types + [node.get('type')]
            children = node.get('props', {}).get('children')
            if isinstance(children, str) and children == marker:
                return here
            return walk(children, here)
        if isinstance(node, list):
            for item in node:
                found = walk(item, types)
                if found is not None:
                    return found
        return None

    return walk(tree, [])


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class MultiCardPaneTests(unittest.TestCase):
    def test_every_card_gets_a_button_with_its_pool_label_and_tone_dot(self):
        out = render(data=pool_data((50.0, 70.0, 90.0, None)))
        self.assertEqual(out['labels'], list(LABELS))
        # remaining 50 -> good; 30 -> warn; 10 -> bad; read failure -> muted.
        self.assertEqual(out['tones'], ['good', 'warn', 'bad', 'muted'])

    def test_tone_boundaries_follow_the_shared_thresholds(self):
        # remaining 41 -> good, 40 -> warn, 16 -> warn, 15 -> bad.
        out = render(data=pool_data((59.0, 60.0, 84.0, 85.0)))
        self.assertEqual(out['tones'], ['good', 'warn', 'warn', 'bad'])

    def test_clicking_a_card_button_switches_the_detail_card(self):
        out = render(data=pool_data((50.0, 70.0, 90.0, None)), click='gamma.gmail')
        self.assertIsNotNone(out['after'])
        before, after = json.dumps(out['before']), json.dumps(out['after'])
        # Default selection is the first card; the click moves the detail to
        # the clicked card (its window value appears; the first one goes).
        self.assertIn('50% left', before)
        self.assertNotIn('10% left', before)
        self.assertIn('10% left', after)
        self.assertNotIn('50% left', after)
        self.assertEqual(out['pressed'], [True, False, False, False])
        self.assertEqual(out['pressedAfter'], [False, False, True, False])

    def test_a_read_failure_stays_a_clickable_muted_card(self):
        out = render(data=pool_data((50.0, 70.0, 90.0, None)), click='delta.mail')
        after = json.dumps(out['after'])
        self.assertIn('fetch-error:OSError', after)
        # The failed card's detail is shown instead of the first card's value.
        self.assertNotIn('50% left', after)

    def test_single_card_provider_keeps_the_plain_card_path(self):
        data = {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': {
            'deepseek': {'windows': [{'label': 'Weekly', 'used_percent': 10.0}]}}}
        out = render(data=data)
        self.assertEqual(out['labels'], [])
        rendered = json.dumps(out['before'])
        self.assertIn('90% left', rendered)
        self.assertIn('DeepSeek', rendered)

    def test_grouping_is_generic_not_hard_coded_to_commandcode(self):
        data = {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': {
            'anthropic': {'windows': [{'label': 'Current session', 'used_percent': 10.0}]},
            'anthropic:work': {'label': 'anthropic · Work', 'provider': 'anthropic',
                               'account_id': 'work', 'account_label': 'Work',
                               'windows': [{'label': 'Current session', 'used_percent': 60.0}]}}}
        out = render(data=data)
        # The primary row has no saved label: falls back to its display name.
        self.assertEqual(out['labels'], ['Anthropic', 'Work'])
        # remaining 90 -> good; 40 -> warn.
        self.assertEqual(out['tones'], ['good', 'warn'])

    # ---- correction round: merged status cell + in-frame card buttons ------

    def test_status_bar_merges_sibling_cards_into_one_cell_with_lights(self):
        # Fix round (2/5): sibling cards collapse into ONE cell; one light per
        # card in row order, tinted by that card's remaining quota; no value
        # text; the cell itself never wraps.
        out = render(component='status', data=pool_data((50.0, 70.0, 90.0, 95.0)))
        tree = out['before']
        buttons, lights = status_parts(tree)
        self.assertEqual(len(buttons), 1)
        cell = buttons[0]
        # remaining 50/30/10/5% -> good/warn/bad/bad, row order.
        self.assertEqual(light_colors(lights), [GOOD, WARN, BAD, BAD])
        self.assertIn('CommandCode', text(cell))
        self.assertNotIn('%', text(cell))
        self.assertIn('whitespace-nowrap', cell['props']['className'])
        seq = flat(cell)
        name_at = seq.index('CommandCode')
        light_at = [i for i, n in enumerate(seq) if any(n is light for light in lights)]
        self.assertTrue(all(i < name_at for i in light_at),
                        'lights must sit left of the provider name')
        # One quota tip for the cell, built from every card's own tip in order.
        tips = [n for n in flat(tree) if isinstance(n, dict) and n.get('type') == 'Tip']
        self.assertEqual(len(tips), 1)
        tip = tooltip_text(tree)
        self.assertEqual(tip.count('Click to open Quota pane'), 1)
        self.assertIn('50% left', tip)
        self.assertIn('10% left', tip)
        self.assertLess(tip.index('50% left'), tip.index('10% left'))

    def test_status_bar_light_boundaries_follow_the_shared_thresholds(self):
        # remaining 41/40/16/15 -> good/warn/warn/bad (same contract as the
        # pane card dots).
        out = render(component='status', data=pool_data((59.0, 60.0, 84.0, 85.0)))
        buttons, lights = status_parts(out['before'])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(light_colors(lights), [GOOD, WARN, WARN, BAD])

    def test_status_bar_single_row_provider_keeps_the_plain_chip(self):
        # Guard (green before and after the fix): a lone row still renders
        # [dot, name, value] — never a multi-light row, never merged.
        data = {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': {
            'deepseek': {'windows': [{'label': 'Weekly', 'used_percent': 10.0}]}}}
        out = render(component='status', data=data)
        buttons, lights = status_parts(out['before'])
        self.assertEqual(len(buttons), 1)
        spans = buttons[0]['props']['children']
        self.assertEqual(len(spans), 3)
        self.assertIn('h-1.5 w-1.5 rounded-full', spans[0]['props']['className'])
        self.assertEqual(spans[1]['props']['children'], 'DeepSeek')
        self.assertEqual(spans[2]['props']['children'], '90%')
        self.assertEqual(len(lights), 1)

    def test_status_bar_merges_any_provider_with_siblings_not_just_commandcode(self):
        data = {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': {
            'anthropic': {'windows': [{'label': 'Current session', 'used_percent': 10.0}]},
            'anthropic:work': {'label': 'anthropic · Work', 'provider': 'anthropic',
                               'account_id': 'work', 'account_label': 'Work',
                               'windows': [{'label': 'Current session', 'used_percent': 60.0}]}}}
        out = render(component='status', data=data)
        buttons, lights = status_parts(out['before'])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(light_colors(lights), [GOOD, WARN])
        self.assertIn('Anthropic', text(buttons[0]))
        self.assertNotIn('%', text(buttons[0]))

    def test_status_bar_merge_keeps_a_failed_row_as_a_grey_light(self):
        # Round 2 rewrites the round-1 expectation: the failed row used to be
        # swallowed by the gate at plugin.js:1431, so four cards rendered
        # three lights. The failed row now keeps its own slot as a grey
        # light, and the healthy lights never shift.
        out = render(component='status', data=pool_data((50.0, 70.0, 90.0, None)))
        buttons, lights = status_parts(out['before'])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(light_colors(lights), [GOOD, WARN, BAD, GREY])

    def test_group_card_buttons_live_inside_the_selected_card_header(self):
        # Fix round (2/5): the per-card buttons moved into the selected card
        # frame, right of the provider name; compact classes keep them light.
        out = render(data=pool_data((50.0, 70.0, 90.0, None)))
        tree = out['before']
        box = box_with_text(tree, 'CommandCode')
        buttons = card_buttons(tree)
        self.assertEqual(len(buttons), 4)
        in_box = [n for n in flat(box) if isinstance(n, dict)]
        for button in buttons:
            self.assertTrue(any(button is n for n in in_box),
                            'card button rendered outside the card frame')
            cls = button['props']['className']
            for part in ('rounded-md', 'px-2', 'py-0.5', 'text-[0.6875rem]'):
                self.assertIn(part, cls)
        seq = flat(box)
        name_at = next(i for i, n in enumerate(seq)
                       if isinstance(n, str) and n.startswith('CommandCode'))
        button_at = [i for i, n in enumerate(seq) if any(n is b for b in buttons)]
        self.assertTrue(button_at and all(name_at < i for i in button_at),
                        'buttons must sit right of the provider name')

    def test_switching_a_group_only_changes_that_provider(self):
        # Guard-plus (green before and after the fix): a click inside one
        # provider's group never re-renders another provider's card.
        data = pool_data((50.0, 70.0, 90.0, None))
        data['providers']['deepseek'] = {'windows': [{'label': 'Weekly', 'used_percent': 10.0}]}
        out = render(data=data, click='gamma.gmail')
        self.assertIsNotNone(out['after'])
        before_cc = box_with_text(out['before'], 'CommandCode')
        after_cc = box_with_text(out['after'], 'CommandCode')
        self.assertNotEqual(before_cc, after_cc)
        self.assertIn('10% left', json.dumps(after_cc))
        self.assertEqual(box_with_text(out['before'], 'DeepSeek'),
                         box_with_text(out['after'], 'DeepSeek'),
                         'another provider was re-rendered by the click')

    # ---- correction round 2: no-data rows keep a grey light ----------------

    def test_status_bar_failed_row_keeps_a_grey_light_in_place(self):
        # Round 2: a read failure at the second card keeps its own grey light
        # in row order; the siblings after it never shift left.
        out = render(component='status', data=pool_data((50.0, None, 90.0, 95.0)))
        buttons, lights = status_parts(out['before'])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(light_colors(lights), [GOOD, GREY, BAD, BAD])

    def test_status_bar_single_failed_provider_gets_a_grey_chip(self):
        # Round 2: a lone provider that failed to fetch still renders a chip
        # with a grey light and the "unavailable" value (no longer dropped).
        out = render(component='status', data=single_row_data('fetch-error:OSError'))
        buttons, lights = status_parts(out['before'])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(light_colors(lights), [GREY])
        spans = buttons[0]['props']['children']
        self.assertEqual(len(spans), 3)
        self.assertIn('h-1.5 w-1.5 rounded-full', spans[0]['props']['className'])
        self.assertEqual(spans[1]['props']['children'], 'DeepSeek')
        self.assertEqual(spans[2]['props']['children'], 'unavailable')

    def test_status_bar_full_pool_of_read_failures_keeps_all_grey_lights(self):
        # Round 2: when every row of a saved pool failed, the cell keeps one
        # grey light per card instead of falling back to the global dot.
        out = render(component='status', data=pool_data((None, None, None, None)))
        buttons, lights = status_parts(out['before'])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(light_colors(lights), [GREY, GREY, GREY, GREY])
        self.assertIn('CommandCode', text(buttons[0]))

    def test_pane_single_failed_provider_stays_in_the_main_flow(self):
        # Round 2 (red before the fix: folded under "No data"): a lone row
        # whose fetch failed keeps its muted card in the main list.
        out = render(data=single_row_data('fetch-error:OSError'))
        tree = out['before']
        chain = path_types(tree, 'DeepSeek')
        self.assertIsNotNone(chain, 'the failed provider card is missing')
        self.assertNotIn('details', chain)
        self.assertIn('fetch-error:OSError', json.dumps(tree))

    def test_status_bar_single_unconfigured_provider_stays_hidden(self):
        # Guard (green before and after the fix): no-credentials is not
        # "no data" — an unconfigured provider keeps the bar empty.
        out = render(component='status', data=single_row_data('no-credentials'))
        buttons, lights = status_parts(out['before'])
        self.assertEqual(buttons, [])
        self.assertEqual(lights, [])

    # ---- correction round 3: unconfigured rows stay hidden everywhere ------

    # Round 3 rewrite (was ..._stays_in_the_no_data_section): the round-2
    # expectation kept a no-credentials row in the pane's collapsed "No data"
    # section. The distinction is per reason: never-configured rows leave
    # both surfaces entirely; only fetch failures keep their grey slot.
    def test_pane_single_unconfigured_provider_is_hidden_entirely(self):
        out = render(data=single_row_data('no-credentials'))
        tree = out['before']
        self.assertIsNone(path_types(tree, 'DeepSeek'))
        self.assertNotIn('details', json.dumps(tree))

    def test_status_bar_single_opt_in_disabled_provider_stays_hidden(self):
        # Round 3 (red before the fix: probe S2 rendered one grey chip): a
        # subscription that was never enabled counts as unconfigured, not as
        # a failed fetch — it contributes no button and no light.
        out = render(component='status',
                     data=single_row_data('opt-in-disabled', provider='antigravity'))
        buttons, lights = status_parts(out['before'])
        self.assertEqual(buttons, [])
        self.assertEqual(lights, [])

    def test_pane_single_opt_in_disabled_provider_stays_hidden(self):
        # Round 3 (red before the fix: probe S2p carded it in the main area):
        # the pane drops a never-configured row entirely — no card, no
        # "No data" fold entry, no reason text.
        out = render(data=single_row_data('opt-in-disabled', provider='antigravity'))
        tree = out['before']
        self.assertIsNone(path_types(tree, 'Antigravity'))
        self.assertNotIn('details', json.dumps(tree))
        self.assertNotIn('opt-in-disabled', json.dumps(tree))

    def test_pane_and_status_agree_on_an_unconfigured_pool_sibling(self):
        # Round 3 (red before the fix: probe S8p kept 3 pane cards against
        # the 2 status lights): the no-credentials sibling is dropped from
        # BOTH surfaces; the pane keeps only the two real cards.
        data = pool_data_with_unconfigured_sibling()
        pane = render(data=data)
        self.assertEqual(len(card_buttons(pane['before'])), 2)
        self.assertEqual(pane['tones'], ['good', 'bad'])
        status = render(component='status', data=data)
        buttons, lights = status_parts(status['before'])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(light_colors(lights), [GOOD, BAD])


if __name__ == '__main__':
    unittest.main(verbosity=2)
