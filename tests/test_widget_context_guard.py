"""Context guard: status-bar chip + card, /compress action, notifications, Live rings, nav hint.

Real `desktop/plugin.js` through `tests/widget_monitor_renderer.cjs` with
SYNTHETIC payloads only (work/home profiles, claude-test-* models); the focused
chat comes from the renderer's `focus` option (host.state atoms).
"""
import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import Optional

from test_widget_monitor import NOW, run, snapshot
from widget_harness import nodes, text

CLICK_JS = '''
const __walk = (n, out) => {
  if (Array.isArray(n)) { n.forEach((c) => __walk(c, out)); return out; }
  if (!n || typeof n !== 'object') return out;
  out.push(n);
  __walk(n.props && n.props.children, out);
  return out;
};
const __find = (tree, name) => __walk(tree, []).filter((n) => n.props && n.props[name] != null);
'''


def find(tree, pred):
    return [n for n in nodes(tree) if pred(n)]


def attr(tree, name):
    return [n['props'][name] for n in nodes(tree) if name in n.get('props', {})]


def usage(pct, used=None, cap=1_000_000, compressions=0, estimated=False):
    used = int(cap * pct / 100) if used is None else used
    return {'context_used': used, 'context_max': cap, 'context_percent': pct, 'compressions': compressions,
            'context_estimated': estimated, 'input': 0, 'output': 0, 'total': 0, 'calls': 1}


def ctx_snapshot(work1: Optional[float] = 84.0, work2: Optional[float] = 40.0, home1: Optional[float] = 93.0, **over):
    snap = snapshot(**over)
    rows = snap['live']['running']
    pcts = {'sess_work_1': work1, 'sess_work_2': work2}
    for r in rows:
        if r['id'] in pcts and pcts[r['id']] is not None:
            r.update(context_max=1_000_000, context_tokens=int(pcts[r['id']] * 10_000),
                     context_percent=pcts[r['id']], context_estimated=False)
    if home1 is not None:
        rows.append({'id': 'sess_home_1', 'kind': 'hermes', 'profile': 'home', 'source_name': 'Hermes',
                     'status': 'running', 'state': 'waiting', 'title': 'Plan the trip', 'model': 'claude-test-small',
                     'tokens': 9000, 'cost': 0.4, 'last': NOW - 300, 'subagent': False,
                     'context_tokens': int(home1 * 2000), 'context_max': 200_000, 'context_percent': home1,
                     'context_estimated': False,
                     'open': {'kind': 'hermes-session', 'command': 'hermes -p home --resume sess_home_1'}})
    # The CLI row has a token count but no window: never part of the guard.
    for r in rows:
        if r['id'] == 'cc-uuid-1':
            r['context_tokens'] = 190_000
    return snap


def chip(**options):
    return run(component='context', **options)['tree']


def button(tree):
    return find(tree, lambda n: n.get('type') == 'button' and 'data-context-chip' in n.get('props', {}))[0]


class LevelsTest(unittest.TestCase):
    def test_levels_follow_threshold_and_danger(self):
        expr = '''[
          contextLevel(79.4, 80), contextLevel(79.5, 80), contextLevel(85, 80), contextLevel(89.4, 80),
          contextLevel(89.6, 80), contextLevel(100, 80), contextLevel(75, 70), contextLevel(75, 80),
          contextLevel(85, 90), contextLevel(null, 80), contextLevel("x", 80),
          contextColor("warn"), contextColor("danger"), contextColor(null),
        ]'''
        self.assertEqual(run(expr=expr)['value'], [
            None, 'warn', 'warn', 'warn', 'danger', 'danger', 'warn', None, None, None, None,
            'var(--ui-yellow)', 'var(--ui-red)', 'var(--ui-accent)'])

    def test_focused_usage_shapes(self):
        out = run(expr='[focusedContext(null), focusedContext({}), focusedContext({context_used: 50, context_max: 200}),'
                       ' focusedContext({context_percent: 120, compressions: 2, context_estimated: true})]')['value']
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertEqual(out[2]['pct'], 25)
        self.assertEqual(out[3]['pct'], 100)
        self.assertEqual(out[3]['compressions'], 2)
        self.assertTrue(out[3]['estimated'])


class ChipVisibilityTest(unittest.TestCase):
    def test_hidden_while_everything_is_below_threshold(self):
        calm = ctx_snapshot(work1=30, home1=12)
        self.assertIsNone(chip(monitor=calm, focus={'usage': usage(27), 'storedId': 'sess_work_2'}))
        # No focused chat at all and nothing over: still hidden.
        self.assertIsNone(chip(monitor=calm))

    def test_focused_chat_over_threshold(self):
        tree = chip(monitor=ctx_snapshot(work1=30, home1=12), focus={'usage': usage(84), 'storedId': 'sess_work_2'})
        b = button(tree)
        self.assertEqual(b['props']['data-context-chip'], 'warn')
        self.assertEqual(text(find(tree, lambda n: 'data-ctx-text' in n.get('props', {}))[0]), '84%')
        self.assertIn('Context 84% full', b['props']['aria-label'])
        ring = find(b, lambda n: n.get('type') == 'circle' and n.get('key') == 'arc')[0]
        self.assertEqual(ring['props']['stroke'], 'var(--ui-yellow)')

    def test_other_sessions_over_threshold_only(self):
        tree = chip(monitor=ctx_snapshot(), focus={'usage': usage(27), 'storedId': 'sess_work_2'})
        b = button(tree)
        self.assertEqual(text(find(tree, lambda n: 'data-ctx-text' in n.get('props', {}))[0]), '2 sessions ≥ 80%')
        self.assertEqual(b['props']['data-context-chip'], 'danger', 'home at 93% makes it red')
        tip = text(find(tree, lambda n: n.get('type') == 'Tip')[0]['props']['label'])
        self.assertIn('This chat: context 27% full (270k / 1.0M)', tip)
        self.assertIn('2 other open sessions ≥ 80%', tip)

    def test_live_reading_wins_for_the_focused_row(self):
        # The snapshot says the focused chat is at 84 %, its live usage says 27 % (just compressed).
        snap = ctx_snapshot(home1=None)
        self.assertIsNone(chip(monitor=snap, focus={'usage': usage(27), 'storedId': 'sess_work_1'}))
        # No live reading yet: the snapshot row stands in for the focused chat.
        tree = chip(monitor=snap, focus={'storedId': 'sess_work_1'})
        self.assertEqual(text(find(tree, lambda n: 'data-ctx-text' in n.get('props', {}))[0]), '84%')

    def test_focused_plus_others_and_danger(self):
        tree = chip(monitor=ctx_snapshot(work1=50), focus={'usage': usage(95), 'storedId': 'sess_work_2'})
        self.assertEqual(text(find(tree, lambda n: 'data-ctx-text' in n.get('props', {}))[0]), '95% +1')
        self.assertEqual(button(tree)['props']['data-context-chip'], 'danger')

    def test_threshold_setting_and_toggles(self):
        focus = {'usage': usage(84), 'storedId': 'sess_work_2'}
        calm = ctx_snapshot(work1=30, home1=12)
        self.assertIsNone(chip(monitor=calm, focus=focus, storage={'contextWarn': 90}))
        self.assertIsNotNone(chip(monitor=calm, focus={'usage': usage(72), 'storedId': 'x'}, storage={'contextWarn': 70}))
        self.assertIsNone(chip(monitor=calm, focus=focus, storage={'statusbarContext': False}))
        self.assertIsNone(chip(monitor=calm, focus=focus, storage={'showStatusBar': False}))
        # An unknown stored threshold falls back to 80.
        self.assertIsNotNone(chip(monitor=calm, focus=focus, storage={'contextWarn': 55}))
        # Works without a monitor backend (focused chat only).
        self.assertIsNotNone(chip(monitor={'unsupported': True}, focus=focus))

    def test_cli_rows_without_window_never_count(self):
        snap = ctx_snapshot(work1=10, work2=10, home1=None)
        self.assertIsNone(chip(monitor=snap, focus={'usage': usage(10), 'storedId': 'sess_work_1'}))

    def test_closed_chip_builds_no_card(self):
        tree = chip(monitor=ctx_snapshot(), focus={'usage': usage(84)})
        self.assertEqual(find(tree, lambda n: n.get('type') == 'PopoverContent'), [])


class CompressTest(unittest.TestCase):
    def compress(self, topic='', **options):
        out = run(expr=f'compressFocused({json.dumps(topic)})', **options)
        return out['value'], out['submitted'], out['toasts']

    def test_submits_compress_to_the_focused_chat(self):
        value, sent, toasts = self.compress(focus={'sessionId': 'rt-1', 'storedId': 'sess_work_1', 'usage': usage(84)})
        self.assertTrue(value['ok'])
        self.assertEqual(sent, [{'sessionId': 'rt-1', 'text': '/compress'}])
        self.assertEqual(toasts[0]['kind'], 'success')

    def test_topic_is_appended_on_one_line(self):
        _, sent, _ = self.compress('  auth   flow\nrefactor ', focus={'sessionId': 'rt-1'})
        self.assertEqual(sent, [{'sessionId': 'rt-1', 'text': '/compress auth flow refactor'}])
        _, sent, _ = self.compress('x' * 500, focus={'sessionId': 'rt-1'})
        self.assertEqual(len(sent[0]['text']), len('/compress ') + 200)
        # Runtime id missing: the stored id still addresses the composer.
        _, sent, _ = self.compress('', focus={'storedId': 'sess_work_1'})
        self.assertEqual(sent, [{'sessionId': 'sess_work_1', 'text': '/compress'}])

    def test_refuses_while_busy_or_without_a_chat(self):
        value, sent, toasts = self.compress(focus={'sessionId': 'rt-1', 'busy': True})
        self.assertEqual((value['ok'], value['reason'], sent, toasts), (False, 'busy', [], []))
        value, sent, _ = self.compress()
        self.assertEqual((value['ok'], value['reason'], sent), (False, 'no_session', []))

    def test_unclaimed_submit_warns(self):
        value, sent, toasts = self.compress(focus={'sessionId': 'rt-1'}, submitResult=False)
        self.assertFalse(value['ok'])
        self.assertEqual(len(sent), 1)
        self.assertEqual(toasts[0]['kind'], 'warning')

    def card(self, **options):
        return run(component='contextCard', monitor=ctx_snapshot(), **options)

    def test_card_disables_compress_when_busy_or_no_chat(self):
        busy = self.card(focus={'sessionId': 'rt-1', 'storedId': 'sess_work_2', 'usage': usage(84), 'busy': True})
        btn = find(busy['tree'], lambda n: n.get('props', {}).get('data-ctx-action') == 'compress')[0]
        self.assertTrue(btn['props']['disabled'])
        self.assertIn('Wait until the current turn finishes.', text(busy['tree']))
        none = self.card()
        btn = find(none['tree'], lambda n: n.get('props', {}).get('data-ctx-action') == 'compress')[0]
        self.assertTrue(btn['props']['disabled'])
        ready = self.card(focus={'sessionId': 'rt-1', 'storedId': 'sess_work_2', 'usage': usage(84)})
        btn = find(ready['tree'], lambda n: n.get('props', {}).get('data-ctx-action') == 'compress')[0]
        self.assertFalse(btn['props']['disabled'])

    def test_card_shows_focused_ring_and_other_sessions(self):
        out = self.card(focus={'sessionId': 'rt-1', 'storedId': 'sess_work_2',
                               'usage': usage(84, used=840_000, compressions=2, estimated=True)})
        tree = out['tree']
        self.assertEqual(attr(tree, 'data-ctx-focused'), ['warn'])
        self.assertIn('≈ 840k / 1.0M tokens', text(tree))
        self.assertIn('compressed 2× so far', text(tree))
        self.assertEqual(attr(tree, 'data-ctx-row'), ['sess_home_1', 'sess_work_1'], 'fullest first, focused excluded')
        self.assertEqual(attr(tree, 'data-card-link'), ['context'])
        self.assertIn('Other sessions ≥ 80%', text(tree))

    def test_confirm_stage_submits_with_topic_and_rows_open_sessions(self):
        expr = '(async () => {' + CLICK_JS + f'''
          const snap = {json.dumps(ctx_snapshot())};
          let closed = 0;
          const tree = ContextCard({{snapshot: snap, scope: host.state.focusedSessionOwner.get(), close: () => closed++, stage: 'confirm', topic: 'db schema'}});
          const inputs = __find(tree, 'data-ctx-topic').length;
          __find(tree, 'data-ctx-action').find((n) => n.props['data-ctx-action'] === 'confirm').props.onClick();
          __find(tree, 'data-ctx-row').find((n) => n.props['data-ctx-row'] === 'sess_home_1').props.onClick();
          await new Promise((r) => setTimeout(r, 5));
          return {{inputs, closed}};
        }})()'''
        out = run(expr=expr, focus={'sessionId': 'rt-1', 'storedId': 'sess_work_2', 'usage': usage(84)})
        self.assertEqual(out['value'], {'inputs': 1, 'closed': 2})
        self.assertEqual(out['submitted'], [{'sessionId': 'rt-1', 'text': '/compress db schema'}])
        self.assertEqual(out['opened'][0]['id'], 'sess_home_1')
        self.assertEqual(out['opened'][0]['opts'], {'profile': 'home'})


class ContextNotificationTest(unittest.TestCase):
    def rules(self, snaps, focus=None, settings=None, warn=80):
        expr = '''(() => {
          let state = {};
          return SNAPS.map((snap) => {
            const r = monitorNotifications({snapshot: snap, providers: {}, settings: SETTINGS,
              context: {warn: WARN, usage: FOCUS && FOCUS.usage, focusedStoredId: FOCUS && FOCUS.storedId}}, state, NOW);
            state = r.state;
            return {notes: r.notes.filter((n) => n.kind === 'context' || n.kind === 'attention')
                                  .map((n) => ({kind: n.kind, key: n.key, title: n.title, target: n.target})),
                    context: state.context};
          });
        })()'''
        expr = (expr.replace('SNAPS', json.dumps(snaps)).replace('SETTINGS', json.dumps(settings or {}))
                .replace('FOCUS', json.dumps(focus)).replace('WARN', str(warn)).replace('NOW', repr(NOW)))
        return run(expr=expr)['value']

    def test_once_per_session_per_level(self):
        out = self.rules([ctx_snapshot(), ctx_snapshot(), ctx_snapshot(work1=91), ctx_snapshot(work1=95)])
        first = [(n['key'], n['title']) for n in out[0]['notes']]
        self.assertEqual(first, [('home|sess_home_1', 'Context 93% full'), ('work|sess_work_1', 'Context 84% full')])
        self.assertEqual(out[0]['context'], {'work|sess_work_1': 80, 'home|sess_home_1': 90})
        self.assertEqual(out[1]['notes'], [], 'same levels again: nothing')
        self.assertEqual([n['title'] for n in out[2]['notes']], ['Context 91% full'], 'crossing 90 fires once more')
        self.assertEqual(out[3]['notes'], [])
        target = out[0]['notes'][0]['target']
        self.assertEqual(target, {'sessionId': 'sess_home_1', 'profile': 'home', 'source': 'hermes'})

    def test_rearms_after_dropping_well_below(self):
        out = self.rules([ctx_snapshot(home1=None), ctx_snapshot(work1=77, home1=None), ctx_snapshot(work1=30, home1=None),
                          ctx_snapshot(work1=82, home1=None)])
        self.assertEqual(len(out[0]['notes']), 1)
        self.assertEqual(out[1]['notes'], [], '77 % is inside the hysteresis band: stays armed-off')
        self.assertEqual(out[2]['context'], {})
        self.assertEqual([n['title'] for n in out[3]['notes']], ['Context 82% full'])

    def test_focused_live_reading_and_threshold(self):
        focus = {'storedId': 'sess_work_2', 'usage': usage(88)}
        out = self.rules([ctx_snapshot(work1=10, home1=None)], focus=focus)
        self.assertEqual([n['key'] for n in out[0]['notes']], ['work|sess_work_2'])
        out = self.rules([ctx_snapshot(work1=10, home1=None)], focus=focus, warn=90)
        self.assertEqual(out[0]['notes'], [])

    def test_toggle_silences_but_records(self):
        out = self.rules([ctx_snapshot(), ctx_snapshot()], settings={'context': False})
        self.assertEqual(out[0]['notes'], [])
        self.assertEqual(out[0]['context'], {'work|sess_work_1': 80, 'home|sess_home_1': 90})

    def test_big_context_attention_defers_to_the_guard(self):
        later = ctx_snapshot()
        later['live']['attention'].append({'id': 'big_context:hermes:work:sess_work_1', 'kind': 'big_context',
                                           'session_id': 'sess_work_1', 'profile': 'work', 'title': 'Refactor the parser',
                                           'detail': '840000 tokens', 'value': 840000})
        on = self.rules([ctx_snapshot(), later])
        self.assertEqual([n for n in on[1]['notes'] if n['kind'] == 'attention'], [])
        off = self.rules([ctx_snapshot(), later], settings={'context': False})
        self.assertEqual([n['title'] for n in off[1]['notes'] if n['kind'] == 'attention'], ['work: large context'])

    def test_os_notification_opens_the_session(self):
        expr = '''(() => {
          const scope = host.state.focusedSessionOwner.get();
          return runMonitorNotifier(scope, {}, SNAP).filter((n) => n.kind === 'context').length;
        })()'''.replace('SNAP', json.dumps(ctx_snapshot()))
        out = run(expr=expr)
        self.assertEqual(out['value'], 2)
        ctx_notes = [n for n in out['notified'] if n['title'].startswith('Context')]
        self.assertTrue(all(n['activatable'] for n in ctx_notes))
        self.assertIn('Plan the trip · home — compress it (/compress) or start a new chat', [n['body'] for n in ctx_notes])
        tr = run(expr=expr, locale='tr')
        self.assertIn('Bağlam %93 dolu', [n['title'] for n in tr['notified']])


class LiveAndNavTest(unittest.TestCase):
    def test_live_rows_show_ring_and_sort_over_threshold_first(self):
        snap = ctx_snapshot(home1=None)
        snap['live']['running'].append({
            'id': 'sess_work_3', 'kind': 'hermes', 'profile': 'work', 'source_name': 'Hermes', 'status': 'running',
            'state': 'working', 'title': 'Small chore', 'model': 'claude-test-small', 'tokens': 100, 'last': NOW - 2,
            'subagent': False, 'context_tokens': 20_000, 'context_max': 1_000_000, 'context_percent': 2.0})
        out = run(component='live', monitor=snap, focus={'storedId': 'sess_work_2', 'usage': usage(91)})
        tree = out['tree']
        self.assertEqual(sorted(attr(tree, 'data-ctx-pct')), [2, 84, 91])
        levels = dict(zip(attr(tree, 'data-ctx-pct'), attr(tree, 'data-ctx-level')))
        self.assertEqual(levels, {91: 'danger', 84: 'warn', 2: 'ok'})
        # CHANGED (layout A, 2026-11-03): the attention row now leads with the
        # session title too (approved design), so the ordering contract is
        # asserted on the session rows only (data-session-head), not on every span.
        titles = [text(n) for h in nodes(tree) if (h.get('props') or {}).get('data-session-head')
                  for n in nodes(h) if n.get('type') == 'span'
                  and text(n) in ('Refactor the parser', 'Deploy preview', 'Small chore')]
        self.assertEqual(titles, ['Deploy preview', 'Refactor the parser', 'Small chore'],
                         'approval first, then the over-threshold working row before a newer calm one')
        self.assertIn('ctx 840k/1.0M', text(tree))

    def test_nav_badge_context_hint(self):
        expr = '''(() => {
          const t = (k, ...a) => (typeof I18N_EN[k] === 'function' ? I18N_EN[k](...a) : I18N_EN[k]);
          const tr = (k, ...a) => (typeof I18N_TR[k] === 'function' ? I18N_TR[k](...a) : I18N_TR[k]);
          const z = {working: 0, approval: 0, yourTurn: 0, stalled: 0};
          return [
            navBadge(z, 60, t, 92).label,
            navBadge(z, 60, t, 89.4).label,
            navBadge({...z, working: 2}, 8, t, 95).label,
            navBadge({...z, approval: 1, working: 1}, 8, t, 91).label,
            navBadge({...z, working: 1, approval: 1, stalled: 1}, 8, t, null).label,
            navBadge(z, null, tr, 92).label,
          ];
        })()'''
        self.assertEqual(run(expr=expr)['value'], [
            'Quota · ctx 92%',
            'Quota',
            'Quota · ctx 95% · 2 active',
            'Quota · 1 approval · ctx 91%',
            'Quota · 1 approval · 1 active · ⚠ 8%',
            'Kota · bağlam %92',
        ])

    def test_settings_controls_and_defaults(self):
        out = run(component='settings')
        seg = [n for n in nodes(out['tree']) if n.get('type') == 'segmented' and 'data-context-warn' in n['props']]
        self.assertEqual(len(seg), 1)
        self.assertEqual(seg[0]['props']['value'], '80')
        self.assertEqual([o['id'] for o in seg[0]['props']['options']], ['70', '80', '90'])
        body = text(out['tree'])
        self.assertIn('Show context warning chip in the status bar', body)
        self.assertIn('Context window filling up (threshold / 90%)', body)
        tr = text(run(component='settings', locale='tr')['tree'])
        self.assertIn('Bağlam uyarı eşiği', tr)
        # Real shipped defaults: chip visible-on-availability, threshold 80,
        # and the context notification kind OFF (explicit opt-in in the trial).
        defaults = run(expr='(applyStoredMonitor(), [contextChipAtom.get(), contextWarnAtom.get(), notifyAtoms.context.get()])',
                       enroll=False)
        self.assertEqual(defaults['value'], [True, 80, False])
        stored = run(expr='(applyStoredMonitor(), [contextChipAtom.get(), contextWarnAtom.get(), notifyAtoms.context.get()])',
                     storage={'statusbarContext': False, 'contextWarn': 70, 'notifyContext': True},
                     enroll=False)
        self.assertEqual(stored['value'], [False, 70, True])
        self.assertIn('context:statusbar.right', out['registered'])


def _react_available():
    root = Path(__file__).resolve().parent.parent
    return all((root / 'node_modules' / m).is_dir() for m in ('react', 'react-dom', '@babel/core', '@babel/preset-env'))


@unittest.skipUnless(_react_available(), 'npm i --no-save react react-dom @babel/core @babel/preset-react @babel/preset-env')
class ContextRealReactTest(unittest.TestCase):
    def render(self, **options):
        # Trial default: no monitor until enrolled. This test needs the other
        # sessions from the snapshot (home at 93% drives the danger tone).
        storage = dict(options.get('storage') or {})
        storage.setdefault('monitorEnabled', True)
        options['storage'] = storage
        p = subprocess.run(['node', str(Path(__file__).resolve().parent / 'widget_monitor_ssr.cjs')],
                           input=json.dumps({'views': ['live'], **options}), text=True, capture_output=True,
                           timeout=60, env={**os.environ, 'TZ': 'UTC', 'LANG': 'en_US.UTF-8'})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_chip_card_and_live_render_with_real_hooks(self):
        out = self.render(monitor=ctx_snapshot(), storage={'paneView': 'live'},
                          focus={'sessionId': 'rt-1', 'storedId': 'sess_work_2', 'usage': usage(84)})
        self.assertEqual([e for e in out['errors'] if 'is being spread into JSX' not in e], [])
        html = out['html']
        self.assertIn('data-context-chip="danger"', html['context'])
        self.assertIn('84% +2', html['context'])
        self.assertIn('data-ctx-row="sess_home_1"', html['contextCard_idle'])
        self.assertIn('data-ctx-compress="confirm"', html['contextCard_confirm'])
        self.assertIn('<input', html['contextCard_confirm'])
        self.assertIn('data-ctx-pct="84"', html['live'])
        for markup in html.values():
            self.assertNotRegex(markup, r'#[0-9a-fA-F]{3,6}\b|rgb\(')
        calm = self.render(monitor=ctx_snapshot(work1=10, home1=None), focus={'usage': usage(20)})
        self.assertEqual(calm['html']['context'], '')


if __name__ == '__main__':
    unittest.main()
