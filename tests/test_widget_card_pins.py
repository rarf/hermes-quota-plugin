"""Pinned quota cards: edit mode, two independent orders, sorting and reset.

Runs the real `desktop/plugin.js` through `tests/widget_card_layout.cjs` (the
real tree, stateful clicks, ctx.storage backed by a map) against synthetic
providers. The pinned shelf is its own sticky band above the list's scrollport,
so these tests also pin the structural seam that keeps the two real native
scrollbars in their own bands while the content scrolls, plus the resizer's
accessibility and saved-height loading. Real Chromium geometry, wheel
containment, drag and resize behaviour live in `tests/test_widget_card_browser.py`.
"""
import itertools
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / 'widget_card_layout.cjs'
EXTRACT = Path(__file__).resolve().parent / 'widget_extract.cjs'

LOCALES = ['en', 'tr', 'de', 'es', 'fr', 'ru', 'ja', 'zh', 'zh-hant', 'ar']


def run(**options):
    p = subprocess.run(
        ['node', str(HARNESS)], input=json.dumps(options), text=True,
        capture_output=True, timeout=30,
        env={**os.environ, 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TZ': 'UTC'},
    )
    if p.returncode:
        raise AssertionError(p.stderr)
    out = json.loads(p.stdout)
    if out.get('error'):
        raise AssertionError(out['error'])
    return out


def run_batch(items, **options):
    """Boot many independent panes in one node process (matrix tests)."""
    payload = dict(options, batch=items)
    p = subprocess.run(
        ['node', str(HARNESS)], input=json.dumps(payload), text=True,
        capture_output=True, timeout=180,
        env={**os.environ, 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TZ': 'UTC'},
    )
    if p.returncode:
        raise AssertionError(p.stderr)
    out = json.loads(p.stdout)
    results = out.get('results')
    if not isinstance(results, list):
        raise AssertionError(out.get('error') or 'batch harness returned no results')
    for result in results:
        if result.get('error'):
            raise AssertionError(result['error'])
    return results


def run_js(calls):
    """Evaluate pure card-layout helpers extracted from the real plugin."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / 'in.json'
        fixture.write_text(json.dumps(calls))
        p = subprocess.run(
            ['node', str(EXTRACT), str(ROOT), str(fixture)],
            capture_output=True, text=True, timeout=60,
        )
        if p.returncode:
            raise AssertionError(p.stderr)
        return json.loads(p.stdout)


def provider(used=50, **extra):
    record = {
        'plan': 'Pro',
        'windows': [{'label': 'Session', 'used_percent': used, 'reset_at': '2099-01-01T00:00:00Z'}],
        'details': [],
    }
    record.update(extra)
    return record


def data(providers=None):
    return {
        'fetched_at': '2026-01-01T00:00:00Z',
        'age_s': 5,
        'providers': providers if providers is not None else {
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        },
    }


def layout(pinned=None, order=None):
    return {'cardLayout': {'pinned': pinned or [], 'order': order or []}}


DROP_RING = '0 0 0 2px color-mix(in srgb, var(--ui-accent) 45%, transparent)'
DROP_BEFORE = f'inset 0 4px 0 0 var(--ui-accent), {DROP_RING}'
DROP_AFTER = f'inset 0 -4px 0 0 var(--ui-accent), {DROP_RING}'


class EditModeTests(unittest.TestCase):
    def test_top_right_toggle_is_icon_only_and_accessible(self):
        idle = run(data=data())['snapshots'][0]
        self.assertEqual(idle['edit']['label'], 'Edit card layout')
        self.assertIs(idle['edit']['pressed'], False)
        self.assertEqual(idle['controls'], {}, 'non-edit mode must stay uncluttered')
        self.assertEqual(idle['shells'], [])

        editing = run(data=data(), editing=True, page=True)['snapshots'][0]
        self.assertEqual(editing['edit']['label'], 'Done editing card layout')
        self.assertIs(editing['edit']['pressed'], True)
        self.assertEqual(
            editing['hint'],
            'Drag a card, or use the arrows, to reorder it within its section; scroll while dragging to reach cards beyond the fold, and pinned cards stay visible while you scroll.',
        )
        self.assertIsNone(
            run(data=data(), editing=True)['snapshots'][0]['hint'],
            'the docked pane keeps the hint out of the short scroll viewport',
        )
        self.assertEqual(set(editing['controls']), {'anthropic', 'deepseek', 'openai', 'gemini'})
        for card_id, controls in editing['controls'].items():
            self.assertEqual(set(controls), {'pin', 'up', 'down'}, card_id)
            for control in controls.values():
                self.assertTrue(control['label'], 'every control needs an accessible label')
            self.assertEqual(
                editing['pinControlCounts'][card_id], 1,
                'edit mode must expose exactly one pin control per card',
            )
            self.assertIn(card_id, editing['dragHandles'])
        # A pointer drag starts anywhere on the card shell; the grip is still
        # the visual handle and the arrows remain the keyboard path.
        self.assertIs(editing['pointerDrag'], True)
        for card_id, shell in editing['shellProps'].items():
            self.assertIs(shell['draggable'], False, card_id)
            self.assertEqual(shell['style']['cursor'], 'grab', card_id)
            self.assertEqual(shell['style']['userSelect'], 'none', card_id)
            self.assertIs(editing['dragHandleProps'][card_id]['draggable'], False, card_id)
            self.assertEqual(editing['dragHandleProps'][card_id]['cursor'], 'grab', card_id)

    def test_edit_toggle_click_opens_and_closes_the_controls(self):
        out = run(data=data(), actions=[{'control': 'edit'}, {'control': 'edit'}])
        self.assertEqual(out['snapshots'][0]['shells'], [])
        self.assertTrue(out['snapshots'][1]['shells'])
        self.assertIs(out['snapshots'][1]['edit']['pressed'], True)
        self.assertEqual(out['snapshots'][2]['shells'], [])
        self.assertIs(out['snapshots'][2]['edit']['pressed'], False)

    def test_edges_of_each_band_disable_the_arrows(self):
        snap = run(
            data=data(), editing=True,
            storage=layout(pinned=['anthropic', 'deepseek']),
        )['snapshots'][0]
        self.assertEqual(snap['pinned'], ['anthropic', 'deepseek'])
        self.assertEqual(snap['unpinned'], ['openai', 'gemini'])
        self.assertIs(snap['controls']['anthropic']['up']['unavailable'], True)
        self.assertIs(snap['controls']['anthropic']['down']['unavailable'], False)
        self.assertIs(snap['controls']['deepseek']['up']['unavailable'], False)
        self.assertIs(snap['controls']['deepseek']['down']['unavailable'], True)
        self.assertIs(snap['controls']['openai']['up']['unavailable'], True)
        self.assertIs(snap['controls']['openai']['down']['unavailable'], False)
        self.assertIs(snap['controls']['gemini']['down']['unavailable'], True)
        # An edge arrow is inert but still focusable; disabling it would
        # throw keyboard focus away exactly when a reorder reaches the edge.
        for card_id in ('anthropic', 'deepseek', 'openai', 'gemini'):
            for direction in ('up', 'down'):
                self.assertIs(
                    snap['controls'][card_id][direction]['disabled'], False,
                    f'{card_id} {direction} must not use the disabled attribute',
                )


class PinTests(unittest.TestCase):
    def test_pin_moves_a_card_to_the_shelf_and_persists(self):
        out = run(data=data(), editing=True, actions=[{'control': 'pin', 'id': 'deepseek'}])
        before, after = out['snapshots']
        self.assertEqual(before['unpinned'], ['anthropic', 'deepseek', 'openai', 'gemini'])
        self.assertEqual(after['pinned'], ['deepseek'])
        self.assertEqual(after['unpinned'], ['anthropic', 'openai', 'gemini'])
        self.assertEqual(
            out['storage']['cardLayout'],
            {
                'pinned': ['deepseek'],
                # The pin records the card's index in the unpinned band and the
                # neighbour identities around it, not a copy of the whole
                # default order (which would freeze the list and look like a
                # custom layout to Reset).
                'order': [],
                'pinSlots': {'deepseek': 1},
                'pinAnchors': {'deepseek': {'prev': 'anthropic', 'next': 'openai'}},
            },
        )
        self.assertEqual(after['controls']['deepseek']['pin']['pressed'], True)
        self.assertIn('Unpin DeepSeek', after['controls']['deepseek']['pin']['label'])

        # A fresh mount with the persisted map restores the shelf.
        remount = run(data=data(), editing=True, storage=out['storage'])['snapshots'][0]
        self.assertEqual(remount['pinned'], ['deepseek'])
        self.assertEqual(remount['unpinned'], ['anthropic', 'openai', 'gemini'])

    def test_unpin_restores_the_old_unpinned_slot(self):
        out = run(
            data=data(), editing=True,
            storage=layout(pinned=['deepseek'], order=['deepseek', 'anthropic', 'openai', 'gemini']),
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        self.assertEqual(out['snapshots'][0]['pinned'], ['deepseek'])
        self.assertEqual(set(out['snapshots'][0]['unpinned']), {'anthropic', 'openai', 'gemini'})
        self.assertEqual(out['snapshots'][1]['pinned'], [])
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['deepseek', 'anthropic', 'openai', 'gemini'],
            'unpinning must return the card to its stored unpinned position',
        )
        self.assertEqual(
            out['storage']['cardLayout'],
            {'pinned': [], 'order': ['deepseek', 'anthropic', 'openai', 'gemini']},
        )

    def test_pin_appends_after_existing_pins(self):
        out = run(
            data=data(), editing=True,
            storage=layout(pinned=['anthropic']),
            actions=[{'control': 'pin', 'id': 'gemini'}],
        )
        self.assertEqual(out['snapshots'][1]['pinned'], ['anthropic', 'gemini'])
        self.assertEqual(out['storage']['cardLayout']['pinned'], ['anthropic', 'gemini'])

    def test_pin_then_unpin_without_reordering_restores_the_exact_slot(self):
        out = run(
            data=data(), editing=True,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(out['snapshots'][0]['unpinned'], ['anthropic', 'deepseek', 'openai', 'gemini'])
        self.assertEqual(out['snapshots'][1]['pinned'], ['deepseek'])
        self.assertEqual(
            out['snapshots'][2]['unpinned'],
            ['anthropic', 'deepseek', 'openai', 'gemini'],
        )
        # The pin recorded only a slot; unpinning removed it and left the
        # default order unclaimed, so Reset has nothing to clear.
        self.assertIs(
            out['snapshots'][2]['layoutTools']['reset']['disabled'], True,
            'a pin followed by its unpin must not count as a custom layout',
        )
        self.assertEqual(
            out['storage']['cardLayout'], {'pinned': [], 'order': []},
            'the slot is dropped with the pin and no order is materialised',
        )

    def test_sort_while_pinned_unpins_into_the_sorted_band(self):
        # Review regression: the numeric slot was recorded before the sort, so
        # unpinning put the card at a stale index and broke the A-Z order even
        # though every visible unpinned card was sorted. The neighbour anchors
        # plus the sorted-band check must insert at the card's real rank.
        payload = data({
            'gemini': provider(30),
            'anthropic': provider(50),
            'openai': provider(70),
            'deepseek': provider(20),
        })
        out = run(
            data=payload, editing=True,
            actions=[
                {'control': 'pin', 'id': 'anthropic'},
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
                {'control': 'pin', 'id': 'anthropic'},
            ],
        )
        self.assertEqual(out['snapshots'][1]['pinned'], ['anthropic'])
        self.assertEqual(
            out['snapshots'][1]['unpinned'], ['gemini', 'openai', 'deepseek'],
        )
        self.assertEqual(
            out['snapshots'][3]['unpinned'], ['deepseek', 'gemini', 'openai'],
            'the sort itself must not move the pinned card back into the list',
        )
        self.assertEqual(
            out['snapshots'][4]['unpinned'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
            'unpinning must respect the A-Z sort, not the pre-sort index',
        )
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
        )

    def test_sort_while_pinned_moves_a_previously_ranked_card(self):
        # A card can already own a rank in `order` before it is pinned; that
        # rank used to win on unpin even after a sort rewrote the visible band
        # around it, leaving the card at its old (unsorted) position.
        seed = layout(
            pinned=[],
            order=['deepseek', 'anthropic', 'openai', 'gemini'],
        )
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(
            out['snapshots'][3]['unpinned'], ['anthropic', 'gemini', 'openai'],
        )
        self.assertEqual(
            out['snapshots'][4]['unpinned'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
            'the old rank must be lifted when a current sort owns the band',
        )

    def test_pinned_ranked_card_returns_between_its_pin_time_neighbours(self):
        # Review regression: A-Z ranks DeepSeek between Anthropic and Gemini,
        # so pinning it used to record no anchors (its rank was treated as the
        # position). A later hand reorder moves OpenAI to the front and
        # rewrites that rank slot, so unpinning restored DeepSeek at the stale
        # slot: after OpenAI and before Anthropic. A ranked card must still
        # remember the neighbours it was pinned between.
        pinned_only = run(
            data=data(), editing=True,
            actions=[
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(
            pinned_only['storage']['cardLayout'].get('pinAnchors'),
            {'deepseek': {'prev': 'anthropic', 'next': 'gemini'}},
            'a ranked pin must record the neighbours from the full band',
        )
        out = run(
            data=data(), editing=True, storage=pinned_only['storage'],
            actions=[
                {'control': 'up', 'id': 'openai'},
                {'control': 'up', 'id': 'openai'},
                {'control': 'up', 'id': 'openai'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(
            out['snapshots'][0]['unpinned'], ['anthropic', 'gemini', 'openai'],
            'the sorted band without the pinned card is the A-Z remainder',
        )
        self.assertEqual(
            out['snapshots'][-1]['unpinned'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
            'the pinned card must return between the neighbours it sat between',
        )
        self.assertEqual(
            out['storage']['cardLayout'],
            {'pinned': [], 'order': ['openai', 'anthropic', 'deepseek', 'gemini']},
        )

    def test_roster_insert_before_pinned_card_uses_neighbour_anchors(self):
        # A provider discovered before the pinned card shifts every index.
        # The anchors keep the card between its original neighbours.
        initial = data({
            'openai': provider(70),
            'deepseek': provider(20),
            'gemini': provider(30),
            'anthropic': provider(50),
        })
        grown = data({
            'cursor': provider(10),
            'openai': provider(70),
            'deepseek': provider(20),
            'gemini': provider(30),
            'anthropic': provider(50),
        })
        seed = layout(order=['cursor', 'openai'])
        pinned = run(
            data=initial, editing=True, storage=seed,
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        pin = pinned['storage']['cardLayout']
        self.assertEqual(pin['pinSlots'], {'deepseek': 1})
        self.assertEqual(
            pin['pinAnchors'],
            {'deepseek': {'prev': 'openai', 'next': 'gemini'}},
            'the pin must store identities, not just an index',
        )
        out = run(
            data=initial, editing=True, storage=seed,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'data', 'data': grown},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(
            out['snapshots'][3]['unpinned'],
            ['cursor', 'openai', 'deepseek', 'gemini', 'anthropic'],
            'the new leading provider must not shift the restored slot',
        )

    def test_roster_removal_before_pinned_card_keeps_the_next_neighbour(self):
        initial = data({
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        removed = data({
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        out = run(
            data=initial, editing=True,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'data', 'data': removed},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(
            out['snapshots'][3]['unpinned'],
            ['deepseek', 'openai', 'gemini'],
            'with the previous neighbour gone, the card returns before its next',
        )

    def test_disabled_neighbour_anchor_falls_back_to_the_next_one(self):
        # A disabled provider is absent from the drawn band but still in the
        # stored order. The anchor pair must not point the card at an id that
        # is not rendered; the surviving next neighbour positions it instead.
        storage = {
            'cardLayout': {
                'pinned': ['deepseek'],
                'order': ['openai', 'cursor', 'anthropic', 'gemini'],
                'pinSlots': {'deepseek': 2},
                'pinAnchors': {
                    'deepseek': {'prev': 'anthropic', 'next': 'openai'},
                },
            },
        }
        payload = data({
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
            'cursor': provider(10),
        })
        out = run(
            data=payload, editing=True, storage=storage, disabled=['anthropic'],
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['deepseek', 'openai', 'cursor', 'gemini'],
            'the disabled previous anchor must defer to the visible next anchor',
        )
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['deepseek', 'openai', 'cursor', 'anthropic', 'gemini'],
            'the disabled provider keeps its stored rank',
        )

    def test_pin_anchors_alone_restore_a_position(self):
        # Anchors are a complete restore record; the numeric slot is only a
        # fallback for layouts written before anchors existed.
        storage = {
            'cardLayout': {
                'pinned': ['deepseek'],
                'order': ['openai', 'gemini', 'anthropic'],
                'pinAnchors': {
                    'deepseek': {'prev': 'gemini', 'next': 'anthropic'},
                },
            },
        }
        out = run(
            data=data(), editing=True, storage=storage,
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['openai', 'gemini', 'deepseek', 'anthropic'],
        )
        self.assertEqual(
            out['storage']['cardLayout'],
            {'pinned': [], 'order': ['openai', 'gemini', 'deepseek', 'anthropic']},
            'the used anchor is dropped with the pin',
        )

    def test_legacy_pin_slots_still_restore_a_position(self):
        # Persistence compatibility: layouts written before pinAnchors existed
        # carry a numeric pinSlots map only. It remains the fallback when no
        # anchor can be used.
        storage = {
            'cardLayout': {
                'pinned': ['deepseek'],
                'order': ['openai', 'anthropic', 'gemini'],
                'pinSlots': {'deepseek': 1},
            },
        }
        out = run(
            data=data(), editing=True, storage=storage,
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
            'an older numeric slot must still be honoured',
        )

    def test_pin_unpin_normalization_keeps_absent_provider_slots(self):
        # A stored id that is not in today's payload is a rank for a
        # temporarily missing provider. The default-order normalizer must not
        # drop it, even when the visible ids line up with the payload.
        seed = layout(order=['ghost', 'openai', 'anthropic', 'deepseek', 'gemini'])
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertIs(out['snapshots'][2]['layoutTools']['reset']['disabled'], False)
        self.assertEqual(
            out['storage']['cardLayout']['order'], seed['cardLayout']['order'],
            'an absent provider keeps its stored slot and keeps Reset enabled',
        )

    def test_pin_unpin_normalization_keeps_disabled_provider_slots(self):
        seed = layout(order=['openai', 'anthropic', 'deepseek', 'gemini'])
        out = run(
            data=data(), editing=True, storage=seed, disabled=['gemini'],
            actions=[
                {'control': 'pin', 'id': 'openai'},
                {'control': 'pin', 'id': 'openai'},
            ],
        )
        self.assertIs(out['snapshots'][2]['layoutTools']['reset']['disabled'], False)
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
            'a disabled provider keeps its stored slot across pin/unpin',
        )

    def test_reordering_the_list_while_pinned_keeps_the_unpin_slot(self):
        # Review regression: pinning from an empty `order`, then reordering the
        # unpinned list, then unpinning used to append the card after every
        # ranked provider because no slot had been recorded at pin time.
        out = run(
            data=data(), editing=True,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'down', 'id': 'anthropic'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(out['snapshots'][1]['unpinned'], ['anthropic', 'openai', 'gemini'])
        self.assertEqual(out['snapshots'][2]['unpinned'], ['openai', 'anthropic', 'gemini'])
        self.assertEqual(
            out['snapshots'][3]['unpinned'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
            'the pinned card returns at its recorded slot, not at the end',
        )
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
        )

    def test_new_provider_added_while_pinned_keeps_its_natural_place(self):
        # Review regression: pinning used to copy the whole unpinned payload
        # order into `order`. A provider discovered later in a poll (here
        # `cursor`, first in the payload) then showed up after the frozen
        # snapshot, and the matching unpin left that copy behind as a "custom"
        # layout with Reset enabled.
        initial = data({
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        grown = data({
            'cursor': provider(10),
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        out = run(
            data=initial, editing=True,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'data', 'data': grown},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        pinned, changed, unpinned = out['snapshots'][1:]
        self.assertEqual(pinned['pinned'], ['deepseek'])
        self.assertEqual(
            changed['unpinned'],
            ['cursor', 'anthropic', 'openai', 'gemini'],
            'a new provider must keep its payload place while a card is pinned',
        )
        self.assertEqual(
            unpinned['unpinned'],
            ['cursor', 'anthropic', 'deepseek', 'openai', 'gemini'],
            'unpinning must not restore a stale copy of the old payload order',
        )
        self.assertIs(
            unpinned['layoutTools']['reset']['disabled'], True,
            'no order was ever customised, so Reset has nothing to clear',
        )
        self.assertEqual(out['storage']['cardLayout'], {'pinned': [], 'order': []})
        self.assertEqual(out['storage']['cardLayout'].get('pinSlots'), None)

    def test_pin_slot_persists_across_a_remount(self):
        initial = data({
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        pinned = run(
            data=initial, editing=True,
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        grown = data({
            'cursor': provider(10),
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        out = run(
            data=grown, editing=True, storage=pinned['storage'],
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        self.assertEqual(out['snapshots'][0]['pinned'], ['deepseek'])
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['cursor', 'anthropic', 'deepseek', 'openai', 'gemini'],
            'the saved slot must survive a fresh mount and a changed payload',
        )
        self.assertIs(out['snapshots'][1]['layoutTools']['reset']['disabled'], True)

    def test_new_provider_keeps_a_real_custom_order_across_pin_unpin(self):
        # The slot is only a fallback: a rank that already exists must win and
        # a missing provider's slot must survive untouched.
        initial = data({
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        grown = data({
            'cursor': provider(10),
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        seed = layout(order=['gemini', 'anthropic', 'ghost', 'openai'])
        out = run(
            data=initial, editing=True, storage=seed,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'data', 'data': grown},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        after = out['snapshots'][3]
        self.assertEqual(out['snapshots'][1]['unpinned'], ['gemini', 'anthropic', 'openai'])
        self.assertEqual(
            after['unpinned'],
            ['gemini', 'anthropic', 'openai', 'deepseek', 'cursor'],
            'a custom rank and the missing provider slot must both survive',
        )
        self.assertIs(after['layoutTools']['reset']['disabled'], False)
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['gemini', 'anthropic', 'ghost', 'openai', 'deepseek', 'cursor'],
            'the insertion replaces visible slots, never the absent provider rank',
        )

    def test_no_data_fold_card_records_its_whole_band_slot(self):
        # A failed card lives in the quiet "No data" fold, whose controls are
        # rendered with only the fold as their band. The pin slot must still
        # count the whole unpinned band, because that is the list unpinning
        # restores into.
        payload = data({
            'anthropic': provider(50),
            'deepseek': {'unavailable_reason': 'fetch-error:OSError', 'details': ['read failed']},
            'openai': provider(70),
            'gemini': provider(30),
        })
        out = run(
            data=payload, editing=True,
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'down', 'id': 'anthropic'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(out['snapshots'][1]['unpinned'], ['anthropic', 'openai', 'gemini'])
        self.assertEqual(out['snapshots'][2]['unpinned'], ['openai', 'anthropic', 'gemini'])
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
            'the fold card returns between the list cards it was pinned between',
        )

    def test_failed_and_no_data_cards_can_be_pinned(self):
        payload = data({
            'anthropic': provider(50),
            'deepseek': {'unavailable_reason': 'fetch-error:OSError', 'details': ['read failed']},
            'cursor': {'windows': [], 'details': []},  # no window data
        })
        snap = run(data=payload, editing=True)['snapshots'][0]
        self.assertEqual(set(snap['controls']), {'anthropic', 'deepseek', 'cursor'})
        self.assertIn('Pin DeepSeek', snap['controls']['deepseek']['pin']['label'])
        self.assertIn('Pin Cursor', snap['controls']['cursor']['pin']['label'])

        out = run(data=payload, editing=True, actions=[{'control': 'pin', 'id': 'deepseek'}])
        self.assertEqual(out['snapshots'][1]['pinned'], ['deepseek'])
        self.assertEqual(out['snapshots'][1]['unpinned'], ['anthropic', 'cursor'])

    def test_grouped_selection_survives_pin_and_unpin_remounts(self):
        payload = data({
            'anthropic': provider(50),
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': provider(60, account_label='beta.gmail'),
        })
        out = run(
            data=payload, editing=True,
            actions=[
                {'control': 'account', 'text': 'beta.gmail'},
                {'control': 'pin', 'id': 'commandcode'},
                {'control': 'pin', 'id': 'commandcode'},
            ],
        )
        self.assertEqual(
            [snap['providers'].get('commandcode') for snap in out['snapshots'][1:]],
            ['commandcode:beta'] * 3,
            'the selected account must survive the pin/unpin remounts',
        )
        self.assertEqual(out['snapshots'][1]['pinned'], [])
        self.assertEqual(out['snapshots'][2]['pinned'], ['commandcode'])
        self.assertEqual(out['snapshots'][3]['pinned'], [])

    def test_grouped_selection_survives_entering_and_leaving_edit_mode(self):
        payload = data({
            'anthropic': provider(50),
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': provider(60, account_label='beta.gmail'),
        })
        out = run(
            data=payload,
            actions=[
                {'control': 'account', 'text': 'beta.gmail'},
                {'control': 'edit'},
                {'control': 'edit'},
            ],
        )
        self.assertEqual(
            [snap['providers'].get('commandcode') for snap in out['snapshots'][1:]],
            ['commandcode:beta'] * 3,
            'the account selector must not reset when the card shell is added/removed',
        )

    def test_grouped_initial_selection_survives_a_backend_reorder(self):
        # The default account used to be "whatever row is first". A backend
        # reorder then silently flipped the card to the other login before the
        # user had picked one. Investigation: that flip was pre-existing (the
        # old component kept a positional useState index); the explicit
        # selection path already used provider ids. Pin the stable default
        # without changing an explicit pick.
        payload = data({
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': provider(60, account_label='beta.gmail'),
        })
        out = run(
            data=payload, editing=True,
            actions=[{'control': 'reorderAccounts'}],
        )
        self.assertEqual(
            out['snapshots'][0]['providers']['commandcode'],
            'commandcode:alpha',
        )
        self.assertEqual(
            out['snapshots'][1]['providers']['commandcode'],
            'commandcode:alpha',
            'a reorder before an explicit choice must not flip the shown login',
        )

    def test_grouped_selection_follows_provider_identity_across_backend_reorder(self):
        payload = data({
            'anthropic': provider(50),
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': provider(60, account_label='beta.gmail'),
        })
        out = run(
            data=payload, editing=True,
            actions=[
                {'control': 'account', 'text': 'beta.gmail'},
                {'control': 'reorderAccounts'},
            ],
        )
        self.assertEqual(out['snapshots'][1]['providers']['commandcode'], 'commandcode:beta')
        self.assertEqual(
            out['snapshots'][2]['providers']['commandcode'],
            'commandcode:beta',
            'a backend row reorder must not select a different login by index',
        )
        self.assertIn('commandcode', out['snapshots'][2]['controls'])

    def test_grouped_accounts_use_one_base_card_identity(self):
        payload = data({
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': provider(60, account_label='beta.gmail'),
        })
        snap = run(data=payload, editing=True)['snapshots'][0]
        self.assertEqual(snap['unpinned'], ['commandcode'], 'both accounts must be one group card')
        self.assertIn('Pin CommandCode', snap['controls']['commandcode']['pin']['label'])
        for account in ('alpha.gmail', 'beta.gmail'):
            self.assertIn(account, snap['text'], 'edit mode must keep the account selector')

        switched = run(
            data=payload, editing=True,
            actions=[{'control': 'account', 'text': 'beta.gmail'}],
        )
        self.assertEqual(
            switched['snapshots'][1]['providers']['commandcode'], 'commandcode:beta',
            'account selection must still switch the card in edit mode',
        )
        self.assertIn('commandcode', switched['snapshots'][1]['controls'])

        out = run(data=payload, editing=True, actions=[{'control': 'pin', 'id': 'commandcode'}])
        self.assertEqual(out['snapshots'][1]['pinned'], ['commandcode'])
        self.assertEqual(out['snapshots'][1]['unpinned'], [])
        self.assertEqual(out['storage']['cardLayout']['pinned'], ['commandcode'])

        # Membership changes: one surviving account keeps the same base pin.
        lone = run(
            data=data({'commandcode:alpha': provider(40, account_label='alpha.gmail')}),
            editing=True, storage=out['storage'],
        )['snapshots'][0]
        self.assertEqual(lone['pinned'], ['commandcode'])
        self.assertEqual(lone['unpinned'], [])

    def test_grouped_card_with_a_failed_account_keeps_its_controls(self):
        payload = data({
            'commandcode': provider(40),
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': {'account_label': 'beta.gmail', 'unavailable_reason': 'fetch-error:OSError'},
        })
        snap = run(data=payload, editing=True)['snapshots'][0]
        self.assertEqual(snap['unpinned'], ['commandcode'])
        self.assertEqual(set(snap['controls']['commandcode']), {'pin', 'up', 'down'})

    def test_empty_data_has_no_edit_control(self):
        snap = run(data={'fetched_at': '2026-01-01T00:00:00Z', 'providers': {}})['snapshots'][0]
        self.assertIsNone(snap['edit'])
        self.assertIsNone(snap['shelf'])

    def test_unpin_keeps_a_custom_order_that_looks_sorted_without_its_card(self):
        # Review regression: with OpenAI pinned the remainder happened to be
        # alphabetical, and unpinning inferred "the band is sorted" from that
        # visible subset. A custom order is a preference; only an explicit
        # A-Z / Z-A action may sort a returning card.
        seed = layout(order=['openai', 'anthropic', 'deepseek', 'gemini'])
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[
                {'control': 'pin', 'id': 'openai'},
                {'control': 'pin', 'id': 'openai'},
            ],
        )
        self.assertEqual(
            out['snapshots'][2]['unpinned'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
            'the first custom card must return to the front, not to the end',
        )
        self.assertEqual(
            out['storage']['cardLayout'],
            {'pinned': [], 'order': ['openai', 'anthropic', 'deepseek', 'gemini']},
        )

    def test_adjacent_pins_return_to_their_custom_relative_order(self):
        # Review regression: two adjacent pins came back swapped depending on
        # which one was unpinned first. With both cards pinned the remaining
        # band was alphabetical, so the old inference ignored the stored ranks
        # and re-sorted the pair into the wrong order.
        order = ['anthropic', 'cursor', 'gemini', 'deepseek', 'openai', 'openrouter']
        natural = ['deepseek', 'gemini', 'anthropic', 'cursor', 'openai', 'openrouter']
        payload = data({name: provider(10 + i) for i, name in enumerate(natural)})
        seed = layout(order=order)
        for pins in (('gemini', 'deepseek'), ('deepseek', 'gemini')):
            for unpins in (('gemini', 'deepseek'), ('deepseek', 'gemini')):
                with self.subTest(pins=pins, unpins=unpins):
                    actions = [{'control': 'pin', 'id': p} for p in pins]
                    actions += [{'control': 'pin', 'id': p} for p in unpins]
                    out = run(data=payload, editing=True, storage=seed, actions=actions)
                    self.assertEqual(
                        out['snapshots'][-1]['unpinned'], order,
                        'unpin order must not decide the restored order',
                    )
                    self.assertEqual(out['storage']['cardLayout']['order'], order)

    def test_adjacent_pins_keep_their_order_under_a_partial_custom_order(self):
        # Review regression: when only some providers are ranked, pinning two
        # neighbours in turn used to record the same outer anchors for both.
        # Unpinning them then swapped the pair for one click order. The pin
        # now records its anchor against the band with the earlier pin put
        # back, so the two remember each other.
        seed = layout(order=['anthropic', 'openai'])
        expected = ['anthropic', 'openai', 'deepseek', 'gemini']
        for pins in (('deepseek', 'gemini'), ('gemini', 'deepseek')):
            for unpins in (('deepseek', 'gemini'), ('gemini', 'deepseek')):
                with self.subTest(pins=pins, unpins=unpins):
                    actions = [{'control': 'pin', 'id': p} for p in pins]
                    actions += [{'control': 'pin', 'id': p} for p in unpins]
                    out = run(data=data(), editing=True, storage=seed, actions=actions)
                    self.assertEqual(out['snapshots'][-1]['unpinned'], expected)

    def test_grouped_implicit_selection_survives_a_remount_after_a_reorder(self):
        # Review regression: the implicit default lived in a per-card useRef,
        # so the remount caused by edit mode or a pin re-read entries[0] and
        # flipped the card to the login that the backend reordered to the top.
        payload = data({
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': provider(60, account_label='beta.gmail'),
        })
        out = run(
            data=payload, editing=True,
            actions=[
                {'control': 'reorderAccounts'},
                {'control': 'remount'},
                {'control': 'pin', 'id': 'commandcode'},
                {'control': 'pin', 'id': 'commandcode'},
            ],
        )
        self.assertEqual(
            [snap['providers'].get('commandcode') for snap in out['snapshots'][1:]],
            ['commandcode:alpha'] * 4,
            'a remount must not re-pick the first row after a backend reorder',
        )


class OrderTests(unittest.TestCase):
    def test_pinned_and_unpinned_orders_are_independent(self):
        out = run(
            data=data(), editing=True,
            storage=layout(pinned=['anthropic', 'deepseek'], order=['openai', 'gemini']),
            actions=[
                {'control': 'down', 'id': 'anthropic'},   # only the pinned band moves
                {'control': 'down', 'id': 'openai'},      # only the unpinned band moves
            ],
        )
        first, second, third = out['snapshots']
        self.assertEqual(first['pinned'], ['anthropic', 'deepseek'])
        self.assertEqual(first['unpinned'], ['openai', 'gemini'])
        self.assertEqual(second['pinned'], ['deepseek', 'anthropic'])
        self.assertEqual(second['unpinned'], ['openai', 'gemini'])
        self.assertEqual(third['pinned'], ['deepseek', 'anthropic'])
        self.assertEqual(third['unpinned'], ['gemini', 'openai'])
        self.assertEqual(out['storage']['cardLayout']['pinned'], ['deepseek', 'anthropic'])
        self.assertEqual(out['storage']['cardLayout']['order'], ['gemini', 'openai'])

    def test_moving_a_card_never_crosses_the_pin_boundary(self):
        # First unpinned card up must stay disabled; a pinned card is never a
        # valid move target for an unpinned card.
        snap = run(
            data=data(), editing=True,
            storage=layout(pinned=['anthropic']),
        )['snapshots'][0]
        self.assertIs(snap['controls']['deepseek']['up']['unavailable'], True)
        self.assertIs(snap['controls']['deepseek']['pin']['pressed'], False)

    def test_missing_and_disabled_providers_keep_their_places(self):
        storage = layout(pinned=['anthropic'], order=['openai', 'gemini', 'deepseek'])
        hidden = run(data=data(), editing=True, storage=storage, disabled=['openai'])['snapshots'][0]
        self.assertEqual(hidden['unpinned'], ['gemini', 'deepseek'])
        shown = run(data=data(), editing=True, storage=storage)['snapshots'][0]
        self.assertEqual(shown['unpinned'], ['openai', 'gemini', 'deepseek'])

        # An unrelated pin does not rewrite the missing provider's order.
        out = run(
            data=data(), editing=True, storage=storage, disabled=['openai'],
            actions=[{'control': 'pin', 'id': 'gemini'}],
        )
        self.assertEqual(out['storage']['cardLayout']['order'], ['openai', 'gemini', 'deepseek'])
        self.assertEqual(out['snapshots'][1]['pinned'], ['anthropic', 'gemini'])
        self.assertEqual(out['snapshots'][1]['unpinned'], ['deepseek'])

    def test_unordered_new_providers_keep_payload_order(self):
        payload = data({
            'anthropic': provider(50),
            'openai': provider(60),
            'newprov': provider(70),
            'gemini': provider(30),
        })
        snap = run(data=payload, editing=True, storage=layout(order=['gemini', 'anthropic']))['snapshots'][0]
        self.assertEqual(
            snap['unpinned'], ['gemini', 'anthropic', 'openai', 'newprov'],
            'stored ranks first, then unseen providers in payload order',
        )


class SortAndResetTests(unittest.TestCase):
    SEED = ['anthropic', 'deepseek', 'openai', 'gemini']

    def test_layout_tools_exist_only_in_edit_mode(self):
        quiet = run(data=data())['snapshots'][0]
        self.assertIsNone(quiet['layoutTools'])

        editing = run(data=data(), editing=True)['snapshots'][0]
        tools = editing['layoutTools']
        self.assertTrue(tools['sortAz']['label'])
        self.assertTrue(tools['sortZa']['label'])
        self.assertTrue(tools['reset']['label'])
        self.assertEqual(tools['reset']['text'], 'Reset')
        self.assertIn('Clear every pin', tools['reset']['hint'])
        self.assertIs(tools['sortAz']['disabled'], False)
        self.assertIs(tools['reset']['disabled'], True, 'nothing custom to reset yet')

    def test_edge_arrow_clicks_do_not_persist_a_layout(self):
        out = run(
            data=data(), editing=True,
            actions=[
                {'control': 'up', 'id': 'anthropic'},
                {'control': 'down', 'id': 'gemini'},
            ],
        )
        self.assertEqual(
            out['storage'], {},
            'aria-disabled edge arrows must not write a card layout',
        )
        for snap in out['snapshots']:
            self.assertEqual(
                snap['unpinned'], ['anthropic', 'deepseek', 'openai', 'gemini'],
                'an unavailable move must leave the visible order alone',
            )
            self.assertIs(
                snap['layoutTools']['reset']['disabled'], True,
                'no edge click may enable Reset',
            )

    def test_sort_is_disabled_when_fewer_than_two_unpinned_cards(self):
        snap = run(
            data=data(), editing=True,
            storage=layout(pinned=['anthropic', 'deepseek', 'openai']),
        )['snapshots'][0]
        self.assertEqual(snap['unpinned'], ['gemini'])
        self.assertIs(snap['layoutTools']['sortAz']['disabled'], True)
        self.assertIs(snap['layoutTools']['sortZa']['disabled'], True)
        self.assertIs(snap['layoutTools']['reset']['disabled'], False)

    def test_sort_requires_confirmation_and_cancel_changes_nothing(self):
        seed = layout(pinned=['deepseek'], order=self.SEED)
        asked = run(
            data=data(), editing=True, storage=seed,
            actions=[{'control': 'sort', 'direction': 'asc'}],
        )
        before, prompt = asked['snapshots']
        self.assertEqual(before['unpinned'], ['anthropic', 'openai', 'gemini'])
        self.assertEqual(prompt['unpinned'], ['anthropic', 'openai', 'gemini'])
        self.assertEqual(prompt['pinned'], ['deepseek'])
        self.assertEqual(prompt['dialog']['title'], 'Replace custom unpinned order?')
        self.assertIn('custom order', prompt['dialog']['description'])
        self.assertIn('Pinned cards and their order stay unchanged', prompt['dialog']['description'])
        self.assertEqual(prompt['dialog']['confirmLabel'], 'Sort')
        self.assertEqual(prompt['dialog']['cancelLabel'], 'Cancel')
        self.assertEqual(
            asked['storage']['cardLayout'],
            seed['cardLayout'],
            'opening the warning must not touch storage',
        )

        cancelled = run(
            data=data(), editing=True, storage=seed,
            actions=[
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'cancelSort'},
            ],
        )
        self.assertIsNone(cancelled['snapshots'][2]['dialog'])
        self.assertEqual(cancelled['snapshots'][2]['pinned'], ['deepseek'])
        self.assertEqual(
            cancelled['snapshots'][2]['unpinned'],
            ['anthropic', 'openai', 'gemini'],
            'cancel leaves the custom order untouched',
        )
        self.assertEqual(cancelled['storage'], seed)

    def test_sort_ascending_changes_only_the_unpinned_band(self):
        seed = layout(pinned=['deepseek'], order=self.SEED)
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
            ],
        )
        after = out['snapshots'][2]
        self.assertIsNone(after['dialog'])
        self.assertEqual(after['pinned'], ['deepseek'], 'pinned membership/order must not move')
        self.assertEqual(after['unpinned'], ['anthropic', 'gemini', 'openai'])
        self.assertEqual(
            out['storage']['cardLayout'],
            {
                'pinned': ['deepseek'],
                # The pinned card keeps its old slot; only unpinned slots sort.
                'order': ['anthropic', 'deepseek', 'gemini', 'openai'],
                'sort': 'asc',
            },
        )
        remount = run(data=data(), editing=True, storage=out['storage'])['snapshots'][0]
        self.assertEqual(remount['pinned'], ['deepseek'])
        self.assertEqual(remount['unpinned'], ['anthropic', 'gemini', 'openai'])

    def test_sort_descending_changes_only_the_unpinned_band(self):
        seed = layout(pinned=['deepseek'], order=self.SEED)
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[
                {'control': 'sort', 'direction': 'desc'},
                {'control': 'confirmSort'},
            ],
        )
        after = out['snapshots'][2]
        self.assertEqual(after['pinned'], ['deepseek'])
        self.assertEqual(after['unpinned'], ['openai', 'gemini', 'anthropic'])
        self.assertEqual(
            out['storage']['cardLayout'],
            {
                'pinned': ['deepseek'],
                'order': ['openai', 'deepseek', 'gemini', 'anthropic'],
                'sort': 'desc',
            },
        )
        remount = run(data=data(), editing=True, storage=out['storage'])['snapshots'][0]
        self.assertEqual(remount['pinned'], ['deepseek'])
        self.assertEqual(remount['unpinned'], ['openai', 'gemini', 'anthropic'])

    def test_sort_with_no_pins_sorts_the_whole_list(self):
        out = run(
            data=data(), editing=True,
            actions=[
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
            ],
        )
        self.assertEqual(out['snapshots'][2]['pinned'], [])
        self.assertEqual(
            out['snapshots'][2]['unpinned'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
        )
        self.assertEqual(
            out['storage']['cardLayout'],
            {'pinned': [], 'order': ['anthropic', 'deepseek', 'gemini', 'openai'], 'sort': 'asc'},
        )

    def test_sort_keeps_disabled_provider_slots(self):
        seed = layout(pinned=['anthropic'], order=['gemini', 'openai', 'deepseek'])
        out = run(
            data=data(), editing=True, storage=seed, disabled=['openai'],
            actions=[
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
            ],
        )
        self.assertEqual(out['snapshots'][2]['unpinned'], ['deepseek', 'gemini'])
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['deepseek', 'openai', 'gemini'],
            'the disabled provider keeps its stored slot',
        )

    def test_unpin_after_a_roster_change_keeps_the_explicit_sort(self):
        # Review regression: the card was pinned, the band sorted, and a
        # provider was discovered while the sort was active. The new card made
        # the rendered order non-monotonic, so inferring "sorted" from the
        # visible subset failed and unpinning appended the card. The stored
        # direction now sorts the whole visible band, new provider included.
        initial = data({
            'gemini': provider(30),
            'anthropic': provider(50),
            'openai': provider(70),
            'deepseek': provider(20),
        })
        grown = data({
            'cursor': provider(10),
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        expected = {
            'asc': ['anthropic', 'cursor', 'deepseek', 'gemini', 'openai'],
            'desc': ['openai', 'gemini', 'deepseek', 'cursor', 'anthropic'],
        }
        for direction, order in expected.items():
            with self.subTest(direction=direction):
                out = run(
                    data=initial, editing=True,
                    actions=[
                        {'control': 'pin', 'id': 'openai'},
                        {'control': 'sort', 'direction': direction},
                        {'control': 'confirmSort'},
                        {'control': 'data', 'data': grown},
                        {'control': 'pin', 'id': 'openai'},
                    ],
                )
                self.assertEqual(
                    out['snapshots'][-2]['unpinned'],
                    [name for name in order if name != 'openai'],
                    'the visible band must stay sorted after a provider appears',
                )
                self.assertEqual(out['snapshots'][-1]['unpinned'], order)
                self.assertEqual(
                    out['storage']['cardLayout']['sort'], direction,
                    'the explicit sort direction must be persisted',
                )
                self.assertEqual(
                    [name for name in out['storage']['cardLayout']['order']
                     if name != 'openai'],
                    [name for name in order if name != 'openai'],
                )

    def test_sort_direction_survives_a_remount_and_governs_the_next_unpin(self):
        first = run(
            data=data(), editing=True,
            actions=[{'control': 'sort', 'direction': 'asc'}, {'control': 'confirmSort'}],
        )
        self.assertEqual(first['storage']['cardLayout']['sort'], 'asc')
        out = run(
            data=data(), editing=True, storage=first['storage'],
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(
            out['snapshots'][-1]['unpinned'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
            'a remount must not forget that the band is explicitly sorted',
        )

    def test_manual_reorder_clears_the_sort_direction(self):
        sorted_run = run(
            data=data(), editing=True,
            actions=[
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
                {'control': 'down', 'id': 'anthropic'},
            ],
        )
        self.assertNotIn(
            'sort', sorted_run['storage']['cardLayout'],
            'dragging or arrowing a card is a custom order, not an A-Z sort',
        )
        out = run(
            data=data(), editing=True, storage=sorted_run['storage'],
            actions=[
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'pin', 'id': 'deepseek'},
            ],
        )
        self.assertEqual(
            out['snapshots'][-1]['unpinned'],
            ['deepseek', 'anthropic', 'gemini', 'openai'],
            'unpinning must restore the hand-made order, not re-sort it',
        )

    def test_reset_clears_the_sort_direction(self):
        # A completed sort whose order happened to match the payload keeps the
        # direction but collapses the array; Reset must still be offered and
        # must clear that preference.
        seed = {'cardLayout': {'pinned': [], 'order': [], 'sort': 'desc'}}
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[{'control': 'reset'}, {'control': 'confirmReset'}],
        )
        self.assertIs(
            out['snapshots'][0]['layoutTools']['reset']['disabled'], False,
            'an explicit sort direction is a preference Reset can clear',
        )
        self.assertEqual(out['storage']['cardLayout'], {'pinned': [], 'order': []})

    def test_reset_clears_pins_and_custom_order_then_persists(self):
        seed = layout(pinned=['deepseek', 'gemini'], order=self.SEED)
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[{'control': 'reset'}, {'control': 'confirmReset'}],
        )
        before, prompt, after = out['snapshots']
        self.assertEqual(before['pinned'], ['deepseek', 'gemini'])
        self.assertEqual(prompt['pinned'], ['deepseek', 'gemini'])
        self.assertEqual(prompt['dialog']['title'], 'Clear all pins and reset the card order?')
        self.assertIn('custom card order', prompt['dialog']['description'])
        self.assertIs(prompt['dialog']['destructive'], True)
        self.assertEqual(after['pinned'], [])
        self.assertEqual(after['unpinned'], self.SEED, 'reset restores the payload order')
        self.assertIs(after['layoutTools']['reset']['disabled'], True)
        self.assertEqual(
            out['storage']['cardLayout'],
            {'pinned': [], 'order': []},
        )
        remount = run(data=data(), editing=True, storage=out['storage'])['snapshots'][0]
        self.assertEqual(remount['pinned'], [])
        self.assertEqual(remount['unpinned'], self.SEED)
        self.assertIs(remount['controls']['anthropic']['pin']['pressed'], False)

    def test_reset_cancel_changes_nothing(self):
        seed = layout(pinned=['deepseek'], order=['openai', 'anthropic', 'gemini'])
        out = run(
            data=data(), editing=True, storage=seed,
            actions=[{'control': 'reset'}, {'control': 'cancelReset'}],
        )
        after = out['snapshots'][2]
        self.assertIsNone(after['dialog'])
        self.assertEqual(after['pinned'], ['deepseek'])
        self.assertEqual(after['unpinned'], ['openai', 'anthropic', 'gemini'])
        self.assertEqual(out['storage'], seed)

    def test_reset_enabled_for_a_custom_order_without_pins(self):
        snap = run(
            data=data(), editing=True,
            storage=layout(order=['openai', 'anthropic']),
        )['snapshots'][0]
        self.assertIs(snap['layoutTools']['reset']['disabled'], False)


class PinRoundTripTests(unittest.TestCase):
    """A pin/unpin cycle must be invisible, whatever order it runs in.

    The band is the product of payload order, stored ranks, an optional sort
    and the pinned set. Pinning records a restore position; unpinning must put
    the card back without rewriting the ranks of cards that are still on the
    shelf, so any permutation of pins and unpins returns the same band.
    """

    PROVIDERS = ('anthropic', 'deepseek', 'openai', 'gemini')
    SEEDS = {
        'custom': ['openai', 'anthropic', 'deepseek', 'gemini'],
        'partial': ['anthropic', 'openai'],
        'empty': [],
        'reversed': ['gemini', 'openai', 'deepseek', 'anthropic'],
        'single': ['openai'],
    }

    @classmethod
    def _cases(cls, providers, unpin_orders, seeds=None):
        cases = []
        labels = []
        for name, seed in (seeds or cls.SEEDS).items():
            for size in range(1, len(providers) + 1):
                for subset in itertools.combinations(providers, size):
                    for pins in itertools.permutations(subset):
                        for unpins in unpin_orders(pins):
                            actions = [{'control': 'pin', 'id': pid} for pid in pins]
                            actions += [{'control': 'pin', 'id': pid} for pid in unpins]
                            cases.append({
                                'data': data(),
                                'editing': True,
                                'storage': layout(order=list(seed)),
                                'actions': actions,
                                'lean': True,
                            })
                            labels.append((name, pins, unpins))
        return cases, labels

    def _assert_round_trips(self, cases, labels):
        results = run_batch(cases)
        self.assertEqual(len(results), len(cases))
        failures = []
        for label, result in zip(labels, results):
            start = result['snapshots'][0]['unpinned']
            end = result['snapshots'][-1]['unpinned']
            if start != end:
                failures.append((label, start, end))
        self.assertEqual(
            failures[:8], [],
            f'{len(failures)} of {len(cases)} pin/unpin orders changed the band',
        )

    def test_every_pin_and_unpin_permutation_restores_the_band(self):
        # Three providers keep the exhaustive permutation matrix fast while
        # still covering both unpin orders of every pin order.
        cases, labels = self._cases(
            self.PROVIDERS[:3],
            lambda pins: tuple(itertools.permutations(pins)),
        )
        self.assertEqual(len(cases), len(self.SEEDS) * 51)
        self._assert_round_trips(cases, labels)

    def test_full_roster_pin_orders_restore_the_band(self):
        # Four providers and both a forward and a reversed unpin order is the
        # matrix the independent review used to find the partial-rank swap.
        # Three seeds cover the full, partial and single-rank layouts; the
        # exhaustive-permutation test above runs every seed on three providers.
        names = ('custom', 'partial', 'single')
        cases, labels = self._cases(
            self.PROVIDERS,
            lambda pins: (pins, tuple(reversed(pins))),
            seeds={name: self.SEEDS[name] for name in names},
        )
        self.assertEqual(len(cases), len(names) * 128)
        self._assert_round_trips(cases, labels)

    def test_partial_order_round_trip_after_the_roster_grows(self):
        # Review regression: two providers, OpenAI pulled to the front, then a
        # poll adds DeepSeek and Gemini. Pinning Anthropic and DeepSeek and
        # unpinning them in the reverse order moved Anthropic behind both new
        # cards because the hidden pinned rank was overwritten.
        small = data({'anthropic': provider(50), 'openai': provider(70)})
        grown = data({
            'anthropic': provider(50),
            'deepseek': provider(20),
            'openai': provider(70),
            'gemini': provider(30),
        })
        out = run(
            data=small, editing=True,
            actions=[
                {'control': 'up', 'id': 'openai'},
                {'control': 'data', 'data': grown},
                {'control': 'pin', 'id': 'anthropic'},
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'pin', 'id': 'deepseek'},
                {'control': 'pin', 'id': 'anthropic'},
            ],
        )
        self.assertEqual(out['snapshots'][1]['unpinned'], ['openai', 'anthropic'])
        self.assertEqual(
            out['snapshots'][2]['unpinned'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
        )
        self.assertEqual(
            out['snapshots'][-1]['unpinned'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
            'an unpin order must not rewrite a still-pinned card rank',
        )
        self.assertEqual(out['snapshots'][-1]['pinned'], [])


class PinReorderOrderTests(unittest.TestCase):
    """A manual move after pinning must not depend on the unpin order.

    Pinning hides cards from the drawn band but leaves their rank in `order`.
    A move of the visible band rewrites those ranks, and the pinned cards must
    return to the band the move produced no matter which one is unpinned first.
    The expected band is the start band with the mover swapped past its drawn
    neighbour: the same move applied to the full band, with the pins held in
    place.
    """

    PROVIDERS = ('anthropic', 'deepseek', 'openai', 'gemini', 'cursor')
    SEEDS = {
        'empty': {'cardLayout': {'pinned': [], 'order': []}},
        'natural': {'cardLayout': {'pinned': [], 'order': list(PROVIDERS)}},
        'reverse': {'cardLayout': {'pinned': [], 'order': list(reversed(PROVIDERS))}},
        'partial': {'cardLayout': {'pinned': [], 'order': ['openai', 'anthropic']}},
        'custom': {'cardLayout': {'pinned': [], 'order': [
            'gemini', 'anthropic', 'cursor', 'deepseek', 'openai',
        ]}},
        'asc': {'cardLayout': {'pinned': [], 'order': list(PROVIDERS), 'sort': 'asc'}},
        'desc': {'cardLayout': {'pinned': [], 'order': list(PROVIDERS), 'sort': 'desc'}},
    }

    @staticmethod
    def _moved(start, visible, mover, delta):
        # Swap the mover with its drawn neighbour in the full start band. This
        # is what the arrow does to the ranks; doing it on the visible ids too
        # would lose the hidden cards' slots.
        band = list(visible)
        at = band.index(mover)
        target = at + delta
        if target < 0 or target >= len(band):
            return list(start)
        other = band[target]
        out = list(start)
        a, b = out.index(mover), out.index(other)
        out[a], out[b] = out[b], out[a]
        return out

    def test_every_manual_move_and_unpin_order_restores_the_moved_band(self):
        cases, labels = [], []
        for name, storage in self.SEEDS.items():
            for first, second in itertools.permutations(self.PROVIDERS, 2):
                for mover in self.PROVIDERS:
                    if mover in (first, second):
                        continue
                    for delta, control in ((-1, 'up'), (1, 'down')):
                        for unpins in ((first, second), (second, first)):
                            actions = [
                                {'control': 'pin', 'id': first},
                                {'control': 'pin', 'id': second},
                                {'control': control, 'id': mover},
                            ] + [{'control': 'pin', 'id': pid} for pid in unpins]
                            cases.append({
                                'data': data({
                                    pid: provider(10 + index)
                                    for index, pid in enumerate(self.PROVIDERS)
                                }),
                                'editing': True,
                                'storage': storage,
                                'actions': actions,
                                'lean': True,
                            })
                            labels.append((name, (first, second), mover, delta, unpins))
        # Seven seeds × 20 ordered pairs × 3 movers × 2 arrows × 2 unpin orders.
        self.assertEqual(len(cases), 7 * 20 * 3 * 2 * 2)
        results = run_batch(cases)
        self.assertEqual(len(results), len(cases))
        failures = []
        for label, result in zip(labels, results):
            name, pins, mover, delta, unpins = label
            start = result['snapshots'][0]['unpinned']
            after_pins = result['snapshots'][2]['unpinned']
            after_move = result['snapshots'][3]['unpinned']
            final = result['snapshots'][-1]['unpinned']
            expected = self._moved(start, after_pins, mover, delta)
            visible = [pid for pid in expected if pid not in pins]
            if after_move != visible:
                failures.append((label, 'move changed the visible band', visible, after_move))
            elif final != expected:
                failures.append((label, 'unpin order changed the band', expected, final))
            elif result['snapshots'][-1]['pinned']:
                failures.append((label, 'pins left', [], result['snapshots'][-1]['pinned']))
            elif sorted(final) != sorted(start):
                failures.append((label, 'roster changed', start, final))
        self.assertEqual(
            failures[:8], [],
            f'{len(failures)} of {len(cases)} moves depended on the unpin order',
        )

    def test_two_pinned_cards_keep_their_order_after_a_manual_move(self):
        # Independent review regression: pin DeepSeek then Gemini, move
        # Anthropic down while both are on the shelf, then unpin. The two
        # returned in opposite orders depending on which was released first.
        for unpins in (('deepseek', 'gemini'), ('gemini', 'deepseek')):
            with self.subTest(unpins=unpins):
                out = run(
                    data=data({
                        pid: provider(10 + index)
                        for index, pid in enumerate(self.PROVIDERS)
                    }),
                    editing=True,
                    storage={'cardLayout': {'pinned': [], 'order': list(self.PROVIDERS)}},
                    actions=[
                        {'control': 'pin', 'id': 'deepseek'},
                        {'control': 'pin', 'id': 'gemini'},
                        {'control': 'down', 'id': 'anthropic'},
                    ] + [{'control': 'pin', 'id': pid} for pid in unpins],
                )
                self.assertEqual(
                    out['snapshots'][3]['unpinned'],
                    ['openai', 'anthropic', 'cursor'],
                )
                self.assertEqual(
                    out['snapshots'][-1]['unpinned'],
                    ['openai', 'deepseek', 'anthropic', 'gemini', 'cursor'],
                    'the moved band and the pin-time pair order must both survive',
                )
                self.assertEqual(out['snapshots'][-1]['pinned'], [])
                self.assertEqual(
                    out['storage']['cardLayout']['order'],
                    ['openai', 'deepseek', 'anthropic', 'gemini', 'cursor'],
                )


class SortStaleRankTests(unittest.TestCase):
    """Moves operate on the drawn band, even when `order` disagrees with it."""

    def _sorted_with_hidden_openai(self):
        # Custom: OpenAI first. While OpenAI is disabled the band sorts A-Z;
        # the stored order still keeps OpenAI's old rank at the front. A later
        # poll shows OpenAI again, so the band and `order` now disagree.
        sorted_run = run(
            data=data(), editing=True, storage=layout(order=['openai']),
            disabled=['openai'],
            actions=[
                {'control': 'sort', 'direction': 'asc'},
                {'control': 'confirmSort'},
            ],
        )
        self.assertEqual(
            sorted_run['storage']['cardLayout']['sort'], 'asc',
            'the setup needs a persisted sort direction',
        )
        return sorted_run['storage']

    def test_arrow_reorders_the_drawn_band_not_the_stale_ranks(self):
        storage = self._sorted_with_hidden_openai()
        out = run(
            data=data(), editing=True, storage=storage,
            actions=[{'control': 'down', 'id': 'anthropic'}],
        )
        self.assertEqual(
            out['snapshots'][0]['unpinned'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
            'the sorted band must be the one the arrow operates on',
        )
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['deepseek', 'anthropic', 'gemini', 'openai'],
            'moving Anthropic down must not also teleport OpenAI to the front',
        )
        self.assertEqual(
            out['storage']['cardLayout'],
            {'pinned': [], 'order': ['deepseek', 'anthropic', 'gemini', 'openai']},
            'the new custom order is exactly the band that was drawn',
        )

    def test_edge_arrow_under_a_sort_keeps_the_sort_direction(self):
        storage = self._sorted_with_hidden_openai()
        out = run(
            data=data(), editing=True, storage=storage,
            actions=[
                {'control': 'up', 'id': 'anthropic'},
                {'control': 'down', 'id': 'openai'},
            ],
        )
        self.assertEqual(
            out['storage'], storage,
            'an unavailable move is not a custom order and must not clear sort',
        )

    def test_moving_after_the_sort_clears_the_direction(self):
        storage = self._sorted_with_hidden_openai()
        out = run(
            data=data(), editing=True, storage=storage,
            actions=[{'control': 'down', 'id': 'deepseek'}],
        )
        self.assertNotIn(
            'sort', out['storage']['cardLayout'],
            'a hand-placed card owns the band from now on',
        )
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['anthropic', 'gemini', 'deepseek', 'openai'],
        )

    def test_every_sorted_arrow_moves_the_drawn_neighbour(self):
        # Property over every stored rank set, sort direction, card and arrow:
        # the new band is the drawn band with exactly that adjacent swap, and
        # an edge click is still a no-op that keeps the sort direction.
        ascending = ['anthropic', 'deepseek', 'gemini', 'openai']
        stored_sets = (
            [],
            ['openai'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
            ['gemini', 'openai', 'deepseek', 'anthropic'],
        )
        cases = []
        labels = []
        for direction, displayed in (
            ('asc', ascending),
            ('desc', list(reversed(ascending))),
        ):
            for stored in stored_sets:
                seed = {
                    'cardLayout': {
                        'pinned': [],
                        'order': list(stored),
                        'sort': direction,
                    },
                }
                for index, card in enumerate(displayed):
                    for delta, control in ((-1, 'up'), (1, 'down')):
                        target = index + delta
                        if 0 <= target < len(displayed):
                            expected = list(displayed)
                            expected[index], expected[target] = expected[target], expected[index]
                        else:
                            expected = list(displayed)
                        cases.append({
                            'data': data(),
                            'editing': True,
                            'storage': seed,
                            'actions': [{'control': control, 'id': card}],
                            'lean': True,
                        })
                        labels.append((direction, tuple(stored), card, control, seed, expected))
        results = run_batch(cases)
        failures = []
        for (direction, stored, card, control, seed, expected), result in zip(labels, results):
            final = result['snapshots'][-1]['unpinned']
            layout = result['storage'].get('cardLayout', {})
            edge = final == expected == result['snapshots'][0]['unpinned']
            if final != expected:
                failures.append((direction, stored, card, control, expected, final))
            elif edge and result['storage'] != seed:
                failures.append((direction, stored, card, control, 'no write', layout))
            elif not edge and 'sort' in layout:
                failures.append((direction, stored, card, control, 'sort cleared', layout))
        self.assertEqual(failures[:8], [], f'{len(failures)} sorted arrows moved the wrong cards')

    def test_disabled_ranks_survive_the_drawn_band_move(self):
        # The disabled provider has no visible slot while the move happens; its
        # rank is a preference over the whole provider set, not a hole to fill.
        storage = {
            'cardLayout': {
                'pinned': [],
                'order': ['openai', 'ghost', 'anthropic', 'deepseek', 'gemini'],
            },
        }
        out = run(
            data=data(), editing=True, storage=storage,
            actions=[{'control': 'down', 'id': 'anthropic'}],
        )
        self.assertEqual(
            out['storage']['cardLayout']['order'],
            ['openai', 'ghost', 'deepseek', 'anthropic', 'gemini'],
            'the hidden ghost keeps its rank between the visible slots',
        )
        self.assertEqual(
            out['snapshots'][1]['unpinned'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
        )


class GroupedSelectionFallbackTests(unittest.TestCase):
    def test_vanished_selection_falls_back_to_the_implicit_login(self):
        # Review regression: an explicit pick outlived the account. While it
        # was missing the card fell back to entries[0], so the next backend
        # reorder flipped it to whatever login arrived first.
        def payload(order):
            return data({
                f'commandcode:{name}': provider(40 + index * 10, account_label=f'{name}.gmail')
                for index, name in enumerate(order)
            })

        out = run(
            data=payload(['alpha', 'beta', 'gamma']), editing=True,
            actions=[
                {'control': 'account', 'text': 'beta.gmail'},
                {'control': 'data', 'data': payload(['gamma', 'alpha'])},
                {'control': 'reorderAccounts'},
                {'control': 'remount'},
            ],
        )
        self.assertEqual(
            [snap['providers'].get('commandcode') for snap in out['snapshots']],
            ['commandcode:alpha', 'commandcode:beta',
             'commandcode:alpha', 'commandcode:alpha', 'commandcode:alpha'],
            'the implicit default must win while the explicit pick is gone',
        )


class PinnedBadgeTests(unittest.TestCase):
    def test_pin_badge_sits_after_the_plan_pill_and_replaces_the_label(self):
        snap = run(data=data(), storage=layout(pinned=['deepseek']))['snapshots'][0]
        self.assertEqual(snap['pinned'], ['deepseek'])
        self.assertEqual(snap['pinnedBadges'], {'deepseek': 'Pinned card'})
        self.assertEqual(
            snap['headerRight']['deepseek'], ['plan', 'pin'],
            'the pin icon belongs to the right of the plan label',
        )
        self.assertEqual(snap['headerRight']['anthropic'], ['plan'])
        self.assertIn('Pinned card', snap['labels'])
        # The shelf heading is gone from the rendered text; the region keeps
        # an aria-label so the grouping is still announced.
        self.assertFalse(snap['shelf']['hasTextLabel'])
        self.assertNotIn('Pinned', snap['text'])
        self.assertEqual(snap['shelf']['label'], 'Pinned')

    def test_badge_does_not_need_a_plan_label(self):
        payload = data({
            'anthropic': provider(50),
            'deepseek': provider(20, plan=None),
        })
        snap = run(data=payload, storage=layout(pinned=['deepseek']))['snapshots'][0]
        self.assertEqual(snap['headerRight']['deepseek'], ['pin'])
        self.assertEqual(snap['pinnedBadges'], {'deepseek': 'Pinned card'})

    def test_edit_mode_replaces_the_badge_with_one_functional_pin_control(self):
        snap = run(
            data=data(), editing=True, page=True,
            storage=layout(pinned=['deepseek']),
        )['snapshots'][0]
        # The pressed pin toggle is the indicator *and* the control while
        # editing: a static badge beside it would be a second pin glyph.
        self.assertEqual(snap['headerRight']['deepseek'], ['controls'])
        self.assertEqual(snap['headerRight']['anthropic'], ['controls'])
        self.assertEqual(snap['pinnedBadges'], {})
        self.assertEqual(snap['pinControlCounts']['deepseek'], 1)
        self.assertIn('Unpin DeepSeek', snap['controls']['deepseek']['pin']['label'])
        self.assertIs(snap['controls']['deepseek']['pin']['pressed'], True)

        quiet = run(data=data(), storage=layout(pinned=['deepseek']))['snapshots'][0]
        self.assertEqual(quiet['pinnedBadges'], {'deepseek': 'Pinned card'})
        self.assertEqual(quiet['headerRight']['deepseek'], ['plan', 'pin'])

    def test_failed_and_no_data_pinned_cards_keep_the_badge(self):
        payload = data({
            'deepseek': {'unavailable_reason': 'fetch-error:OSError', 'details': ['read failed']},
            'cursor': {'windows': [], 'details': []},
        })
        snap = run(data=payload, storage=layout(pinned=['deepseek', 'cursor']))['snapshots'][0]
        self.assertEqual(snap['pinned'], ['deepseek', 'cursor'])
        self.assertEqual(
            snap['pinnedBadges'],
            {'deepseek': 'Pinned card', 'cursor': 'Pinned card'},
        )

    def test_every_card_shape_has_exactly_one_pin_affordance_per_mode(self):
        payload = data({
            'anthropic': provider(50),
            'deepseek': {'unavailable_reason': 'fetch-error:OSError', 'details': ['read failed']},
            'cursor': {'windows': [], 'details': []},
            'commandcode:alpha': provider(40, account_label='alpha.gmail'),
            'commandcode:beta': provider(60, account_label='beta.gmail'),
        })
        pinned = ['anthropic', 'deepseek', 'cursor', 'commandcode']
        editing = run(
            data=payload, editing=True, page=True,
            storage=layout(pinned=pinned),
        )['snapshots'][0]
        self.assertEqual(editing['pinned'], pinned)
        self.assertEqual(editing['pinnedBadges'], {})
        for card_id in pinned:
            self.assertEqual(editing['pinControlCounts'][card_id], 1, card_id)
            self.assertIs(editing['controls'][card_id]['pin']['pressed'], True, card_id)
            self.assertIn(card_id, editing['dragHandles'])

        quiet = run(data=payload, storage=layout(pinned=pinned))['snapshots'][0]
        self.assertEqual(set(quiet['pinnedBadges']), set(pinned))
        self.assertEqual(quiet['controls'], {})

    def test_badge_is_translated(self):
        snap = run(data=data(), storage=layout(pinned=['deepseek']), locale='de')['snapshots'][0]
        self.assertEqual(snap['pinnedBadges'], {'deepseek': 'Angeheftete Karte'})
        self.assertIn('Angeheftete Karte', snap['labels'])


class DropSlotTests(unittest.TestCase):
    """The insertion helper behind a pointer drop.

    The real pointer gesture, control exclusion and band scrolling live in
    tests/widget_card_browser.cjs; this class pins the slot math that both the
    arrows and a drop share.
    """

    def move(self, storage, visible, source, target, before):
        out = run(data=data(), actions=[{
            'control': 'moveSlot', 'storage': storage, 'visible': visible,
            'from': source, 'to': target, 'before': before,
        }])
        return out['helperMoves'][0]

    def test_drop_before_moves_the_card_into_the_slot(self):
        self.assertEqual(
            self.move([], ['anthropic', 'deepseek', 'openai', 'gemini'],
                      'gemini', 'anthropic', True),
            ['gemini', 'anthropic', 'deepseek', 'openai'],
        )

    def test_drop_after_moves_the_card_into_the_slot(self):
        self.assertEqual(
            self.move([], ['anthropic', 'deepseek', 'openai', 'gemini'],
                      'deepseek', 'openai', False),
            ['anthropic', 'openai', 'deepseek', 'gemini'],
        )

    def test_pinned_and_unpinned_bands_move_independently(self):
        out = run(data=data(), actions=[
            {'control': 'moveSlot', 'storage': ['anthropic', 'deepseek'],
             'visible': ['anthropic', 'deepseek'], 'from': 'deepseek',
             'to': 'anthropic', 'before': True},
            {'control': 'moveSlot', 'storage': ['openai', 'gemini'],
             'visible': ['openai', 'gemini'], 'from': 'openai',
             'to': 'gemini', 'before': False},
        ])
        self.assertEqual(out['helperMoves'], [
            ['deepseek', 'anthropic'],
            ['gemini', 'openai'],
        ])

    def test_a_card_cannot_move_into_the_other_band(self):
        # Neither side of a cross-band pair is in the visible list of the
        # band being moved, so the slot math returns the order untouched.
        self.assertEqual(
            self.move(['anthropic'], ['anthropic'], 'openai', 'anthropic', True),
            ['anthropic'],
        )
        self.assertEqual(
            self.move(['anthropic'], ['anthropic'], 'anthropic', 'openai', True),
            ['anthropic'],
        )

    def test_drop_onto_itself_or_its_own_slot_is_a_noop(self):
        visible = ['anthropic', 'deepseek', 'openai', 'gemini']
        self.assertEqual(
            self.move([], visible, 'deepseek', 'deepseek', True), visible,
        )
        self.assertEqual(
            self.move([], visible, 'anthropic', 'deepseek', True), visible,
        )

    def test_disabled_and_unknown_provider_slots_are_kept(self):
        self.assertEqual(
            self.move(['openai', 'anchor', 'gemini', 'deepseek'],
                      ['anthropic', 'deepseek', 'gemini'], 'deepseek', 'gemini', True),
            ['openai', 'anchor', 'deepseek', 'gemini', 'anthropic'],
        )

    def test_grouped_cards_drop_by_base_provider_identity(self):
        self.assertEqual(
            self.move([], ['anthropic', 'commandcode'], 'commandcode', 'anthropic', True),
            ['commandcode', 'anthropic'],
        )


class PersistenceSafetyTests(unittest.TestCase):
    def test_malformed_layout_is_sanitized(self):
        storage = {'cardLayout': {
            'pinned': ['anthropic', 'anthropic', 7, '', 'anthropic:work', ' deepseek '],
            'order': ['openai', None, {}, 'openai', 'gemini'],
        }}
        out = run(
            data=data(), editing=True, storage=storage,
            actions=[{'control': 'pin', 'id': 'openai'}],
        )
        first = out['snapshots'][0]
        self.assertEqual(first['pinned'], ['anthropic', 'deepseek'])
        self.assertEqual(first['unpinned'], ['openai', 'gemini'])
        self.assertEqual(
            out['storage']['cardLayout'],
            {
                'pinned': ['anthropic', 'deepseek', 'openai'],
                'order': ['openai', 'gemini'],
                # Pinning a ranked card now records its neighbours too; the
                # rank alone stops describing a slot once the band moves.
                'pinAnchors': {'openai': {'prev': None, 'next': 'gemini'}},
            },
            'the stored map must be rewritten sanitized and deduplicated',
        )

    def test_storage_failure_still_updates_the_in_memory_layout(self):
        out = run(
            data=data(), editing=True, storageThrows=True,
            actions=[{'control': 'pin', 'id': 'anthropic'}],
        )
        self.assertEqual(out['snapshots'][0]['pinned'], [])
        self.assertEqual(out['snapshots'][1]['pinned'], ['anthropic'])
        self.assertEqual(out['storage'], {}, 'no write can succeed when storage throws')

    def test_many_pins_use_a_bounded_scrolling_shelf(self):
        pinned = ['anthropic', 'deepseek', 'openai', 'gemini']
        snap = run(
            data=data({'anthropic': provider(10), 'deepseek': provider(20),
                       'openai': provider(30), 'gemini': provider(40)}),
            editing=True, storage=layout(pinned=pinned),
        )['snapshots'][0]
        self.assertEqual(snap['pinned'], pinned)
        self.assertEqual(snap['unpinned'], [])
        self.assertEqual(snap['shelf']['style']['position'], 'sticky')
        # The shelf is its own scrollport above the list now, inside the
        # stable `data-quota-body` region but outside the list's scroller.
        self.assertIs(snap['shelfInScroll'], False)
        self.assertIs(snap['shelfInBody'], True)
        self.assertIs(snap['scrollInBody'], True)
        self.assertEqual(snap['body']['style']['containerType'], 'size')
        # Only the card list inside the shelf scrolls; the sticky backdrop
        # itself must not grow its own scrollbar or it would detach.
        self.assertEqual(
            snap['shelf']['frameStyle']['maxHeight'], 'calc(50cqh - 12px)',
            'a static render uses the conservative half-viewport fallback; '
            'the browser measures the exact half-window maximum',
        )
        self.assertEqual(snap['shelf']['scrollStyle']['overflowY'], 'auto')
        self.assertEqual(
            snap['shelf']['scrollStyle']['overscrollBehaviorY'], 'contain',
            'the shelf must not chain touch/trackpad scrolling into the list',
        )
        self.assertNotIn('overflowY', snap['shelf']['style'])
        self.assertEqual(snap['shelf']['scrollStyle']['minHeight'], 0)
        self.assertEqual(snap['scroll']['style']['overflowY'], 'auto')
        self.assertEqual(snap['scroll']['style']['scrollbarGutter'], 'stable')
        # The old "Pinned (n)" heading is gone, but the region keeps a name
        # for assistive tech and each pinned card carries the pin badge.
        self.assertFalse(snap['shelf']['hasTextLabel'])
        self.assertNotIn('Pinned', snap['text'])
        self.assertEqual(snap['shelf']['label'], 'Pinned')
        self.assertEqual(snap['pinnedBadges'], {}, 'edit mode has no separate badge')
        normal = run(data=data(), storage=layout(pinned=pinned))['snapshots'][0]
        self.assertEqual(set(normal['pinnedBadges']), set(pinned))
        self.assertEqual(
            normal['shelf']['frameStyle']['maxHeight'], 'calc(50cqh - 12px)',
            'the quiet view gets the same pre-measurement cap',
        )

    def test_each_band_owns_a_real_scrollport_and_scrollbar(self):
        snap = run(
            data=data(),
            storage=layout(pinned=['anthropic', 'deepseek']),
        )['snapshots'][0]
        # The stable body region holds two scrollports; the shelf is not a
        # child of the list's scroller, so the list bar cannot run beside it.
        self.assertIs(snap['shelfInBody'], True)
        self.assertIs(snap['scrollInBody'], True)
        self.assertIs(snap['shelfInScroll'], False)
        self.assertNotIn('overflowY', snap['body']['style'])
        self.assertEqual(snap['body']['style']['containerType'], 'size')
        # Both bands get a real native scrollbar with the same reserved
        # gutter, which is what keeps their cards aligned while only one
        # band overflows.
        for band in ('shelf', 'scroll'):
            with self.subTest(band=band):
                style = snap['shelf']['scrollStyle'] if band == 'shelf' else snap['scroll']['style']
                self.assertEqual(style['overflowY'], 'auto')
                self.assertEqual(style['scrollbarGutter'], 'stable')
                self.assertNotIn('scrollbarWidth', style)
                self.assertNotIn('msOverflowStyle', style)
                self.assertEqual(style['overscrollBehaviorY'], 'contain')
        # The resize strip and cue stop at that gutter instead of covering
        # the pinned bar. Zero is the pre-measurement value: the browser
        # measures the host's 8px classic scrollbar and updates the var.
        self.assertEqual(snap['shelf']['style']['--quota-pinned-scrollbar'], '0px')
        resize = snap['shelf']['resize']['style']
        self.assertEqual(resize['insetInlineEnd'], 'var(--quota-pinned-scrollbar, 0px)')
        self.assertEqual(resize['insetInlineStart'], 0)
        self.assertEqual(resize['paddingInlineStart'], 'var(--quota-pinned-scrollbar, 0px)')
        self.assertNotIn('left', resize)
        self.assertNotIn('right', resize)
        # The route page constrains the whole two-scrollport region to the
        # same column as the header and cards, so both bars line up at the
        # card edge instead of one sitting at the window edge.
        page_snap = run(
            data=data(), page=True,
            storage=layout(pinned=['anthropic', 'deepseek']),
        )['snapshots'][0]
        self.assertEqual(page_snap['body']['style']['maxWidth'], 680)
        self.assertEqual(page_snap['shelf']['style']['width'], '100%')

    def test_shelf_exposes_a_keyboard_and_pointer_resizer(self):
        snap = run(data=data(), storage=layout(pinned=['anthropic', 'deepseek']))['snapshots'][0]
        resize = snap['shelf']['resize']
        self.assertIsNotNone(resize, 'a pinned shelf must be resizable')
        self.assertEqual(resize['role'], 'separator')
        self.assertEqual(resize['orientation'], 'horizontal')
        self.assertEqual(resize['label'], 'Resize pinned section')
        self.assertIn('arrow keys', resize['hint'])
        self.assertEqual(resize['title'], 'Drag to resize the pinned section')
        self.assertEqual(resize['tabIndex'], 0)
        self.assertFalse(resize['disabled'], 'no blocked state before measurement')
        self.assertEqual(resize['style']['position'], 'absolute')
        self.assertEqual(resize['style']['bottom'], 0)
        self.assertEqual(resize['style']['height'], 16)
        # The static render has no layout metrics yet; the inline height is
        # therefore absent and the CSS fallback caps the frame.
        self.assertNotIn('height', snap['shelf']['frameStyle'])
        withoutPins = run(data=data())['snapshots'][0]
        self.assertIsNone(withoutPins['shelf'], 'no pins means no shelf and no handle')

    def test_saved_shelf_height_is_loaded_and_sanitized(self):
        pinned = ['anthropic', 'deepseek']
        snap = run(
            data=data(),
            storage={**layout(pinned=pinned), 'pinnedShelfHeight': 233.6},
        )['snapshots'][0]
        self.assertEqual(snap['shelf']['frameStyle']['height'], 234)
        self.assertEqual(snap['shelf']['resize']['now'], 234)
        self.assertFalse(snap['shelf']['resize']['disabled'])
        for bad in ('nonsense', None, -12, 0):
            with self.subTest(value=bad):
                clean = run(
                    data=data(),
                    storage={**layout(pinned=pinned), 'pinnedShelfHeight': bad},
                )['snapshots'][0]
                self.assertNotIn('height', clean['shelf']['frameStyle'], bad)
                self.assertIsNone(clean['shelf']['resize'].get('now'), bad)
        capped = run(
            data=data(),
            storage={**layout(pinned=pinned), 'pinnedShelfHeight': 99999},
        )['snapshots'][0]
        self.assertEqual(capped['shelf']['frameStyle']['height'], 4000)

    def test_shelf_falls_back_to_a_half_window_cap_when_any_card_is_unpinned(self):
        three = ['anthropic', 'deepseek', 'openai']
        for editing in (True, False):
            with self.subTest(editing=editing):
                snap = run(
                    data=data(), editing=editing, storage=layout(pinned=three),
                )['snapshots'][0]
                self.assertEqual(
                    snap['shelf']['frameStyle']['maxHeight'],
                    'calc(50cqh - 12px)',
                    'the pre-measurement cap no longer depends on edit mode',
                )

    def test_shelf_paints_an_opaque_themed_backdrop_including_its_padding(self):
        snap = run(data=data(), storage=layout(pinned=['anthropic']))['snapshots'][0]
        style = snap['shelf']['style']
        background = style['background']
        # The pane shell paints --ui-bg-chrome; the shelf must match it or the
        # pinned band reads as a different surface. The opaque card tokens
        # remain as fallbacks for older hosts.
        self.assertIn('--ui-bg-chrome', background)
        self.assertIn('--ui-chat-bubble-opaque-background', background)
        self.assertIn('--ui-bg-editor', background)
        # No separator or frame: pinned cards are the list's first cards, held
        # in place, not a section of their own.
        self.assertNotIn('borderBottom', style)
        self.assertNotIn('border', style)
        self.assertNotIn('boxShadow', style)
        self.assertEqual(style['width'], '100%')
        # The gap under the last pinned card lives inside the painted box.
        self.assertEqual(style['paddingBottom'], 8)
        self.assertNotIn('marginBottom', style)
        # The scrollport has no top padding: a strip there would sit above the
        # sticky box and let scrolling cards bleed through the pane's top edge.
        self.assertNotIn('paddingTop', snap['scroll']['style'])
        self.assertEqual(snap['scroll']['contentStyle']['paddingTop'], 0)
        # The shelf's own 4px top padding paints the edge and keeps the first
        # card at the same visual offset (the old 2px scroll padding + 2px
        # shelf padding).
        self.assertEqual(style['paddingTop'], 4)
        # A native scrollbar would narrow and off-centre the pinned cards.
        # The fix is matching `scrollbar-gutter: stable` gutters on both
        # bands, not hiding the pinned bar: the band now has a real one.
        self.assertEqual(snap['shelf']['scrollStyle']['scrollbarGutter'], 'stable')
        self.assertNotIn('scrollbarWidth', snap['shelf']['scrollStyle'])
        self.assertEqual(snap['scroll']['style']['scrollbarGutter'], 'stable')
        self.assertNotIn('scrollbarWidth', snap['scroll']['style'])

    def test_unpinned_list_keeps_its_top_breathing_room(self):
        snap = run(data=data())['snapshots'][0]
        self.assertIsNone(snap['shelf'])
        # With no sticky band to paint the edge, the content column carries
        # the 2px gap under the pane header.
        self.assertEqual(snap['scroll']['contentStyle']['paddingTop'], 2)
        self.assertNotIn('paddingTop', snap['scroll']['style'])

    def test_layout_is_client_local_and_does_not_touch_other_settings(self):
        out = run(
            data=data(), editing=True,
            storage={'paneDetail': 'clean', 'disabledProviders': ['cursor']},
            actions=[{'control': 'pin', 'id': 'deepseek'}],
        )
        self.assertEqual(out['storage']['paneDetail'], 'clean')
        self.assertEqual(out['storage']['disabledProviders'], ['cursor'])


class CardLayoutTranslationTests(unittest.TestCase):
    def test_edit_controls_are_translated_in_every_bundle(self):
        for loc in LOCALES:
            with self.subTest(locale=loc):
                snap = run(
                    data=data(), editing=True, page=True, locale=loc,
                    storage=layout(pinned=['anthropic']),
                )['snapshots'][0]
                labels = ' | '.join(snap['labels'])
                self.assertNotIn('editCardsTip', labels)
                self.assertNotIn('pinCardTip', labels)
                self.assertNotIn('moveCardUpTip', labels)
                self.assertNotIn('editCardsHint', labels)
                self.assertNotIn('pinnedCardTip', labels)
                self.assertEqual(snap['pinnedBadges'], {})
                self.assertTrue(snap['hint'])
                self.assertNotIn('editCardsHint', snap['hint'])
                self.assertIn(snap['controls']['anthropic']['pin']['label'], labels)
                resize = snap['shelf']['resize']
                for field in ('label', 'hint', 'title'):
                    self.assertTrue(resize[field], f'{loc}: resize {field} is empty')
                    self.assertNotIn('pinnedShelfResize', resize[field])
                if loc != 'en':
                    self.assertNotEqual(
                        resize['label'], 'Resize pinned section',
                        f'{loc}: the resize label must be translated',
                    )
        self.assertIn('Anthropic anheften', run(data=data(), editing=True, locale='de')['snapshots'][0]['labels'])
        ja = run(data=data(), editing=True, locale='ja')['snapshots'][0]
        self.assertIn('Anthropic を固定', ja['controls']['anthropic']['pin']['label'])

    def test_sort_and_reset_tools_and_warning_are_translated_in_every_bundle(self):
        for loc in LOCALES:
            with self.subTest(locale=loc):
                out = run(
                    data=data(), editing=True, page=True, locale=loc,
                    storage=layout(pinned=['anthropic']),
                    actions=[{'control': 'sort', 'direction': 'asc'}],
                )
                snap = out['snapshots'][1]
                tools = snap['layoutTools']
                for key in ('sortAz', 'sortZa', 'reset'):
                    self.assertTrue(tools[key]['label'], f'{loc}:{key} needs a label')
                    self.assertNotIn(f'{key}', tools[key]['label'])
                    self.assertNotIn('Cards', tools[key]['label'])
                self.assertTrue(tools['reset']['text'], f'{loc}: the reset control needs visible text')
                dialog = snap['dialog']
                self.assertTrue(dialog and dialog['open'])
                for field in ('title', 'description', 'confirmLabel', 'cancelLabel'):
                    self.assertTrue(dialog[field], f'{loc}: dialog {field} is empty')
                    self.assertNotIn('sortCards', dialog[field])
                self.assertNotIn('accountsConfirm', dialog['cancelLabel'])
                reset = run(
                    data=data(), editing=True, locale=loc,
                    storage=layout(pinned=['anthropic']),
                    actions=[{'control': 'reset'}],
                )['snapshots'][1]['dialog']
                for field in ('title', 'description', 'confirmLabel', 'cancelLabel'):
                    self.assertTrue(reset[field], f'{loc}: reset dialog {field} is empty')
                    self.assertNotIn('resetCardLayout', reset[field])
                self.assertIs(reset['destructive'], True)
        self.assertEqual(
            run(data=data(), editing=True, locale='de',
                actions=[{'control': 'sort', 'direction': 'asc'}])['snapshots'][1]['dialog']['title'],
            'Eigene Reihenfolge der nicht angehefteten Karten ersetzen?',
        )
        ja = run(data=data(), editing=True, locale='ja',
                 actions=[{'control': 'sort', 'direction': 'desc'}])['snapshots'][1]['dialog']
        self.assertEqual(ja['confirmLabel'], '並べ替え')
        self.assertIn('固定したカードとその順序は変わりません', ja['description'])


class PureCardLayoutHelperTests(unittest.TestCase):
    def test_sanitize_and_default(self):
        out = run_js({'sanitizeCardLayout': [{
            'pinned': ['anthropic:work', 'anthropic', 7, '', None, 'deepseek'],
            'order': 'not-a-list',
        }]})
        self.assertEqual(out['sanitizeCardLayout'], {'pinned': ['anthropic', 'deepseek'], 'order': []})
        self.assertEqual(run_js({'sanitizeCardLayout': [None]})['sanitizeCardLayout'],
                         {'pinned': [], 'order': []})
        out = run_js({'sanitizeCardLayout': [{
            'pinned': ['anthropic'],
            'order': [],
            # Only a pinned card can own a slot; junk and stale entries drop.
            'pinSlots': {
                'anthropic': 2.7, 'deepseek': 1, 'ghost': -1, 'gemini': 'nope',
            },
        }]})
        self.assertEqual(
            out['sanitizeCardLayout'],
            {'pinned': ['anthropic'], 'order': [], 'pinSlots': {'anthropic': 2}},
        )

    def test_seed_unpinned_move_keeps_only_recorded_pins(self):
        full = ['anthropic', 'deepseek', 'openai', 'gemini']
        # A pin made by this build has an anchor/slot, so the move materialises
        # its rank and the later unpin has a stable place to return to.
        out = run_js({'seedUnpinnedMoveOrder': [{
            'pinned': ['deepseek'],
            'order': [],
            'pinAnchors': {'deepseek': {'prev': 'anthropic', 'next': 'openai'}},
            'pinSlots': {'deepseek': 1},
        }, [], full]})
        self.assertEqual(
            out['seedUnpinnedMoveOrder'], full,
            'a recorded pin joins the full band the move rewrites',
        )
        # A legacy pin with no rank, anchor or slot has nothing to restore:
        # moving the unpinned band must not invent a pinned rank for it.
        out = run_js({'seedUnpinnedMoveOrder': [
            {'pinned': ['deepseek'], 'order': []},
            [],
            full,
        ]})
        self.assertEqual(
            out['seedUnpinnedMoveOrder'],
            ['anthropic', 'openai', 'gemini'],
            'an unrecorded legacy pin stays out of the moved band',
        )
        # A ranked pin is part of the stored order already and must stay.
        out = run_js({'seedUnpinnedMoveOrder': [
            {'pinned': ['deepseek'], 'order': ['gemini', 'deepseek']},
            ['gemini', 'deepseek'],
            full,
        ]})
        self.assertEqual(
            out['seedUnpinnedMoveOrder'],
            ['gemini', 'anthropic', 'deepseek', 'openai'],
            'a ranked pin keeps its rank order while the visible ids merge in',
        )
        # Under an explicit sort the seed is the sorted band, not the pre-sort
        # rank array, so the hidden pins return in the sorted sequence.
        out = run_js({'seedUnpinnedMoveOrder': [
            {
                'pinned': ['deepseek'],
                'order': ['gemini', 'anthropic', 'openai', 'deepseek'],
                'sort': 'asc',
            },
            ['gemini', 'anthropic', 'openai', 'deepseek'],
            ['anthropic', 'cursor', 'deepseek', 'gemini', 'openai'],
        ]})
        self.assertEqual(
            out['seedUnpinnedMoveOrder'],
            ['anthropic', 'cursor', 'deepseek', 'gemini', 'openai'],
            'a sorted move is seeded from the band the user sees',
        )
        # Hidden/absent providers are not in the drawn band, but their stored
        # rank must survive the sorted move (the plain sort path keeps it too).
        out = run_js({'seedUnpinnedMoveOrder': [
            {
                'pinned': ['deepseek'],
                'order': ['ghost', 'gemini', 'anthropic', 'openai', 'deepseek'],
                'sort': 'asc',
            },
            ['ghost', 'gemini', 'anthropic', 'openai', 'deepseek'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
        ]})
        self.assertEqual(
            out['seedUnpinnedMoveOrder'],
            ['ghost', 'anthropic', 'deepseek', 'gemini', 'openai'],
            'a hidden rank keeps its array position through a sorted move',
        )

    def test_sanitize_rejects_coerced_pin_slots(self):
        # `null`, `false`, `""` and `[]` all coerce to 0 via Number(), which
        # used to place a corrupt entry at the top of the band. Only a real
        # finite number is a slot; fractions keep their old floor behaviour.
        pinned = [
            'anthropic', 'deepseek', 'openai', 'gemini',
            'cursor', 'openrouter', 'ollama', 'aider',
        ]
        out = run_js({'sanitizeCardLayout': [{
            'pinned': pinned,
            'pinSlots': {
                'anthropic': None,
                'deepseek': False,
                'openai': True,
                'gemini': '',
                'cursor': [],
                'openrouter': [2],
                'ollama': '2',
                'aider': 2.7,
            },
        }]})
        self.assertEqual(
            out['sanitizeCardLayout']['pinSlots'], {'aider': 2},
            'only numeric slot values may survive sanitization',
        )
        for bad in (None, False, '', [], {}, '2'):
            with self.subTest(value=bad):
                out = run_js({'sanitizeCardLayout': [{
                    'pinned': ['anthropic'],
                    'pinSlots': {'anthropic': bad},
                }]})
                self.assertNotIn('pinSlots', out['sanitizeCardLayout'])

    def test_sanitize_pin_anchors_drops_junk_and_stale_ids(self):
        out = run_js({'sanitizeCardLayout': [{
            'pinned': ['anthropic', 'deepseek', 'openai', 'gemini'],
            'pinAnchors': {
                'anthropic': {'prev': 'ghost', 'next': 'deepseek'},
                'deepseek': {'prev': None, 'next': None},
                'openai': 'not-an-object',
                'gemini': {'prev': 'openai', 'next': 'gemini'},
                'stale': {'prev': 'anthropic', 'next': 'openai'},
            },
        }]})
        self.assertEqual(
            out['sanitizeCardLayout']['pinAnchors'],
            {
                'anthropic': {'prev': 'ghost', 'next': 'deepseek'},
                'gemini': {'prev': 'openai', 'next': None},
            },
            'anchors must be objects for pinned cards only; self-anchors drop',
        )
        out = run_js({'sanitizeCardLayout': [{
            'pinned': ['anthropic'],
            'pinAnchors': [],
        }]})
        self.assertNotIn('pinAnchors', out['sanitizeCardLayout'])

    def test_sanitize_sort_direction(self):
        # A legacy layout has no direction at all; only the two values the
        # sort buttons can write are meaningful. Everything else must drop so
        # a corrupt value cannot be mistaken for an explicit sort.
        out = run_js({'sanitizeCardLayout': [{
            'pinned': [], 'order': ['b', 'a'], 'sort': 'desc',
        }]})
        self.assertEqual(
            out['sanitizeCardLayout'],
            {'pinned': [], 'order': ['b', 'a'], 'sort': 'desc'},
        )
        for bad in ('ascending', 'ASC', 1, True, None, {}, []):
            with self.subTest(sort=bad):
                out = run_js({'sanitizeCardLayout': [{
                    'pinned': [], 'order': ['b', 'a'], 'sort': bad,
                }]})
                self.assertNotIn('sort', out['sanitizeCardLayout'])

    def test_order_groups_splits_and_sorts(self):
        groups = [{'base': 'a'}, {'base': 'b'}, {'base': 'c'}, {'base': 'd'}]
        out = run_js({'orderCardGroups': [groups, {'pinned': ['d', 'missing'], 'order': ['c', 'a']}]})
        self.assertEqual([g['base'] for g in out['orderCardGroups']['pinned']], ['d'])
        self.assertEqual([g['base'] for g in out['orderCardGroups']['unpinned']], ['c', 'a', 'b'])

    def test_move_keeps_holes_and_stops_at_edges(self):
        out = run_js({
            'moveCardInBand': [
                ['x', 'a', 'b'], ['a', 'b'], 'b', -1,
            ],
        })
        self.assertEqual(out['moveCardInBand'], ['x', 'b', 'a'])
        out = run_js({'moveCardInBand': [[], ['a', 'b', 'c'], 'c', -1]})
        self.assertEqual(out['moveCardInBand'], ['a', 'c', 'b'])
        # An unavailable move is not a request to materialise the merged
        # layout: it returns the stored order untouched so the caller can
        # compare it with what it passed and skip the storage write.
        out = run_js({'moveCardInBand': [[], ['a', 'b'], 'a', -1]})
        self.assertEqual(out['moveCardInBand'], [], 'an edge move must not materialise an order')
        out = run_js({'moveCardInBand': [['b'], ['a', 'b'], 'a', -1]})
        self.assertEqual(out['moveCardInBand'], ['b'], 'an edge move returns the caller order as-is')

    def test_move_to_slot_inserts_instead_of_swapping(self):
        out = run_js({'moveCardToSlot': [['x', 'a', 'b', 'c'], ['a', 'b', 'c'], 'c', 'a', True]})
        self.assertEqual(out['moveCardToSlot'], ['x', 'c', 'a', 'b'])
        out = run_js({'moveCardToSlot': [['x', 'a', 'b', 'c'], ['a', 'b', 'c'], 'a', 'c', False]})
        self.assertEqual(out['moveCardToSlot'], ['x', 'b', 'c', 'a'])
        out = run_js({'moveCardToSlot': [[], ['a', 'b'], 'a', 'b', True]})
        self.assertEqual(out['moveCardToSlot'], ['a', 'b'], 'dropping in place changes nothing')
        out = run_js({'moveCardToSlot': [[], ['a', 'b'], 'a', 'b', False]})
        self.assertEqual(out['moveCardToSlot'], ['b', 'a'])
        out = run_js({'moveCardToSlot': [['a', 'b'], ['a', 'b'], 'zz', 'b', True]})
        self.assertEqual(out['moveCardToSlot'], ['a', 'b'], 'unknown ids are refused')

    def test_insert_pinned_slot_restores_only_the_visible_slots(self):
        out = run_js({'insertPinnedCardSlot': [
            ['openai', 'anthropic', 'gemini'],
            ['openai', 'anthropic', 'gemini'],
            'deepseek', 1,
        ]})
        self.assertEqual(
            out['insertPinnedCardSlot'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
        )
        out = run_js({'insertPinnedCardSlot': [
            [], ['anthropic', 'openai'], 'deepseek', 1,
        ]})
        self.assertEqual(
            out['insertPinnedCardSlot'], [],
            'an empty order is the payload order: no frozen slot to restore',
        )
        out = run_js({'insertPinnedCardSlot': [
            ['openai', 'ghost', 'gemini'],
            ['openai', 'gemini'],
            'deepseek', 1,
        ]})
        self.assertEqual(
            out['insertPinnedCardSlot'],
            ['openai', 'deepseek', 'ghost', 'gemini'],
            'a missing provider keeps its stored rank while the slot is filled',
        )
        out = run_js({'insertPinnedCardSlot': [
            ['openai', 'gemini'], ['openai', 'gemini'], 'deepseek', 99,
        ]})
        self.assertEqual(out['insertPinnedCardSlot'], ['openai', 'gemini', 'deepseek'])
        out = run_js({'insertPinnedCardSlot': [
            ['openai', 'deepseek'], ['openai'], 'deepseek', 0,
        ]})
        self.assertEqual(out['insertPinnedCardSlot'], ['openai', 'deepseek'])

    def test_insert_pinned_slot_prefers_neighbour_anchors(self):
        out = run_js({'insertPinnedCardSlot': [
            ['openai', 'gemini', 'anthropic'],
            ['openai', 'gemini', 'anthropic'],
            'deepseek', 0,
            {'prev': 'gemini', 'next': 'anthropic'},
        ]})
        self.assertEqual(
            out['insertPinnedCardSlot'],
            ['openai', 'gemini', 'deepseek', 'anthropic'],
            'the anchors must win over a stale numeric index',
        )
        out = run_js({'insertPinnedCardSlot': [
            ['openai', 'gemini', 'anthropic'],
            ['openai', 'gemini', 'anthropic'],
            'deepseek', 2,
            {'prev': 'ghost', 'next': 'openai'},
        ]})
        self.assertEqual(
            out['insertPinnedCardSlot'],
            ['deepseek', 'openai', 'gemini', 'anthropic'],
            'an absent previous anchor defers to the visible next one',
        )
        out = run_js({'insertPinnedCardSlot': [
            ['openai', 'gemini', 'anthropic'],
            ['openai', 'gemini', 'anthropic'],
            'deepseek', 1,
            {'prev': 'anthropic', 'next': 'openai'},
        ]})
        self.assertEqual(
            out['insertPinnedCardSlot'],
            ['openai', 'deepseek', 'gemini', 'anthropic'],
            'a reversed anchor pair falls back to the numeric slot',
        )
        for bad in (None, False, '', '2', []):
            with self.subTest(slot=bad):
                out = run_js({'insertPinnedCardSlot': [
                    ['openai', 'gemini', 'anthropic'],
                    ['openai', 'gemini', 'anthropic'],
                    'deepseek', bad, None,
                ]})
                self.assertEqual(
                    out['insertPinnedCardSlot'],
                    ['deepseek', 'openai', 'gemini', 'anthropic'],
                    'a malformed legacy slot must not coerce into a position',
                )

    def test_restore_pinned_slot_holds_still_pinned_ranks(self):
        # Anthropic is still pinned, so its slot in `order` must not be pushed
        # to the other side of a returning sibling. The band already draws the
        # restored position, so the right answer is to keep the stored array.
        out = run_js({'restorePinnedCardSlot': [
            ['openai', 'anthropic'],
            ['openai', 'gemini'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
            ['anthropic', 'deepseek'],
            'deepseek', 1,
            {'prev': 'anthropic', 'next': 'gemini'},
        ]})
        self.assertEqual(
            out['restorePinnedCardSlot'], ['openai', 'anthropic'],
            'a hidden pinned rank must survive an unpin that already draws correctly',
        )
        # When the anchors do force a different drawn slot, the still-pinned
        # rank keeps its position between the visible cards.
        out = run_js({'restorePinnedCardSlot': [
            ['openai', 'anthropic', 'gemini'],
            ['openai', 'gemini'],
            ['openai', 'anthropic', 'gemini', 'deepseek'],
            ['anthropic'],
            'deepseek', 2,
            {'prev': 'openai', 'next': None},
        ]})
        self.assertEqual(
            out['restorePinnedCardSlot'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
        )
        # Legacy layouts have no anchors: the numeric slot is the whole record.
        out = run_js({'restorePinnedCardSlot': [
            ['openai', 'anthropic', 'gemini'],
            ['openai', 'anthropic', 'gemini'],
            ['openai', 'anthropic', 'gemini', 'deepseek'],
            [],
            'deepseek', 1, None,
        ]})
        self.assertEqual(
            out['restorePinnedCardSlot'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
        )

    def test_restore_pinned_slot_uses_anchors_over_an_existing_rank(self):
        # The rank alone is not the restore record any more: a later hand
        # reorder rewrites that slot, so a ranked pinned card must resolve
        # from its neighbours first. Here its stale rank says right after
        # OpenAI, while its pin-time neighbours place it after Anthropic.
        out = run_js({'restorePinnedCardSlot': [
            ['openai', 'deepseek', 'anthropic', 'gemini'],
            ['openai', 'anthropic', 'gemini'],
            ['openai', 'deepseek', 'anthropic', 'gemini'],
            ['deepseek'],
            'deepseek', None,
            {'prev': 'anthropic', 'next': 'gemini'},
        ]})
        self.assertEqual(
            out['restorePinnedCardSlot'],
            ['openai', 'anthropic', 'deepseek', 'gemini'],
            'anchors must beat a rank that a reorder has made stale',
        )
        # A pair that is still visible but now points backwards cannot name a
        # between-slot. Keep the existing rank instead of falling to slot 0.
        out = run_js({'restorePinnedCardSlot': [
            ['gemini', 'deepseek', 'anthropic'],
            ['gemini', 'anthropic'],
            ['gemini', 'deepseek', 'anthropic'],
            ['deepseek'],
            'deepseek', None,
            {'prev': 'anthropic', 'next': 'gemini'},
        ]})
        self.assertEqual(
            out['restorePinnedCardSlot'],
            ['gemini', 'deepseek', 'anthropic'],
            'a reversed pair must keep a ranked card at its existing rank',
        )

    def test_reorder_visible_order_maps_the_drawn_band_into_stored_slots(self):
        out = run_js({'reorderVisibleCardOrder': [
            ['openai', 'ghost', 'anthropic', 'deepseek', 'gemini'],
            ['anthropic', 'deepseek', 'gemini', 'openai'],
            ['deepseek', 'anthropic', 'gemini', 'openai'],
        ]})
        self.assertEqual(
            out['reorderVisibleCardOrder'],
            ['deepseek', 'ghost', 'anthropic', 'gemini', 'openai'],
            'the drawn band becomes the visible slots; the ghost rank stays put',
        )
        out = run_js({'reorderVisibleCardOrder': [
            ['b', 'a', 'c'], ['a', 'b'], ['a', 'b'],
        ]})
        self.assertEqual(
            out['reorderVisibleCardOrder'], ['b', 'a', 'c'],
            'a move that changes nothing must not rewrite the stored order',
        )
        out = run_js({'reorderVisibleCardOrder': [
            ['a', 'b'], ['a', 'b'], ['a'],
        ]})
        self.assertEqual(
            out['reorderVisibleCardOrder'], ['a', 'b'],
            'a malformed moved sequence is refused',
        )

    def test_dragged_card_band_is_computed_from_the_drawn_ids(self):
        # A drag after a persisted sort must not derive its destination from
        # stored indices: these ids are the sorted band, not the stored order.
        out = run_js({'draggedCardBand': [
            ['anthropic', 'deepseek', 'gemini', 'openai'],
            'anthropic', 'deepseek', False,
        ]})
        self.assertEqual(
            out['draggedCardBand'],
            ['deepseek', 'anthropic', 'gemini', 'openai'],
        )
        # Dropping a card into the slot it already owns is a no-op (null), so
        # the caller keeps the stored layout and an active sort.
        out = run_js({'draggedCardBand': [
            ['anthropic', 'deepseek', 'gemini'], 'anthropic', 'deepseek', True,
        ]})
        self.assertIsNone(out['draggedCardBand'])
        out = run_js({'draggedCardBand': [
            ['anthropic', 'deepseek', 'gemini'], 'anthropic', 'anthropic', True,
        ]})
        self.assertIsNone(out['draggedCardBand'], 'a drop onto itself is refused')

    def test_drop_line_marks_the_insertion_edge(self):
        self.assertEqual(
            run_js({'cardDropLine': [True]})['cardDropLine'],
            DROP_BEFORE,
        )
        self.assertEqual(
            run_js({'cardDropLine': [False]})['cardDropLine'],
            DROP_AFTER,
        )

    def test_toggle_pin_uses_the_base_id(self):
        out = run_js({'toggleCardPin': [{'pinned': [], 'order': []}, 'anthropic:work']})
        self.assertEqual(out['toggleCardPin']['pinned'], ['anthropic'])
        out = run_js({'toggleCardPin': [{'pinned': ['anthropic'], 'order': ['anthropic']}, 'anthropic']})
        self.assertEqual(out['toggleCardPin'], {'pinned': [], 'order': ['anthropic']})

    def test_normalize_collapses_only_a_complete_default_order(self):
        visible = ['a', 'b', 'c']
        out = run_js({'normalizeCardLayout': [
            {'pinned': [], 'order': ['a', 'b', 'c']}, visible, visible,
        ]})
        self.assertEqual(
            out['normalizeCardLayout'], {'pinned': [], 'order': []},
            'a materialised copy of the payload order is not a preference',
        )
        out = run_js({'normalizeCardLayout': [
            {'pinned': [], 'order': ['b', 'a', 'c']}, visible, visible,
        ]})
        self.assertEqual(
            out['normalizeCardLayout']['order'], ['b', 'a', 'c'],
            'a real custom order is kept',
        )
        out = run_js({'normalizeCardLayout': [
            {'pinned': [], 'order': ['ghost', 'a', 'b', 'c']}, visible, visible,
        ]})
        self.assertEqual(
            out['normalizeCardLayout']['order'], ['ghost', 'a', 'b', 'c'],
            'an absent provider slot is a preference and is kept',
        )
        out = run_js({'normalizeCardLayout': [
            {'pinned': [], 'order': ['a', 'b']}, visible, visible,
        ]})
        self.assertEqual(
            out['normalizeCardLayout']['order'], ['a', 'b'],
            'a partial order still carries rank information',
        )
        out = run_js({'normalizeCardLayout': [
            {'pinned': ['d'], 'order': ['a', 'b', 'c']}, visible, visible,
        ]})
        self.assertEqual(
            out['normalizeCardLayout']['pinned'], ['d'],
            'pins are never normalized away',
        )

    def test_sort_visible_order_keeps_absent_provider_slots(self):
        out = run_js({'sortVisibleCardOrder': [
            [], ['b', 'a'], ['a', 'b'],
        ]})
        self.assertEqual(out['sortVisibleCardOrder'], ['a', 'b'])
        out = run_js({'sortVisibleCardOrder': [
            ['x', 'b', 'a', 'y'], ['a', 'b'], ['a', 'b'],
        ]})
        self.assertEqual(out['sortVisibleCardOrder'], ['x', 'a', 'b', 'y'])
        out = run_js({'sortVisibleCardOrder': [
            ['gemini', 'openai', 'deepseek'], ['gemini', 'deepseek'], ['deepseek', 'gemini'],
        ]})
        self.assertEqual(
            out['sortVisibleCardOrder'],
            ['deepseek', 'openai', 'gemini'],
            'the absent id keeps its slot between the sorted visible ids',
        )
        out = run_js({'sortVisibleCardOrder': [
            ['b', 'a'], ['a', 'b'], ['a'],
        ]})
        self.assertEqual(out['sortVisibleCardOrder'], ['b', 'a'], 'a partial sort is refused')


if __name__ == '__main__':
    unittest.main()
