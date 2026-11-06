"""Status-bar chips: agents (compact + flash + card), quota alerts, cost chip.

Real `desktop/plugin.js` through `tests/widget_monitor_renderer.cjs`, fed
SYNTHETIC payloads only (work/home profiles, claude-test-* models).
"""
import json
import re
import unittest

from test_widget_monitor import NOW, iso, quota_payload, run, snapshot, usage
from widget_harness import nodes, text


def find(tree, pred):
    return [n for n in nodes(tree) if pred(n)]


def attr(tree, name):
    return [n['props'][name] for n in nodes(tree) if name in n.get('props', {})]


# Walks a jsx() tree inside the vm and fires the onClick of matching nodes.
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


class CountdownAndForecastTest(unittest.TestCase):
    def alert(self, provider, burn=None, pid='anthropic'):
        snap = {'monitor_version': 1, 'live': {'burn': burn or []}}
        expr = (f'quotaChipAlert({json.dumps(pid)}, {json.dumps(provider)}, '
                f'monitorBurnMap({json.dumps(snap)}, null), {int(NOW)})')
        return run(expr=expr)['value']

    def test_countdown_formatting(self):
        out = run(expr='[59, 60, 3599, 3600, 7020, 86399, 90000, 6 * 86400 + 3 * 3600].map(fmtSpan)')['value']
        self.assertEqual(out, ['<1m', '1m', '59m', '1h 0m', '1h 57m', '23h 59m', '1d 1h', '6d 3h'])

    def test_reset_countdown_only_at_85_percent_used(self):
        provider = {'windows': [
            {'label': 'Session', 'used_percent': 84.9, 'reset_at': iso(600)},
            {'label': 'Weekly', 'used_percent': 91, 'reset_at': iso(7020)},
        ]}
        a = self.alert(provider)
        self.assertEqual(a['reset']['text'], '1h 57m')
        self.assertEqual(a['reset']['window'], 'Weekly')
        self.assertIsNone(a['full'])
        self.assertIsNone(self.alert({'windows': [{'label': 'Session', 'used_percent': 60, 'reset_at': iso(600)}]}))
        # A reset already in the past, stale or unavailable data: no countdown.
        self.assertIsNone(self.alert({'windows': [{'label': 'Session', 'used_percent': 99, 'reset_at': iso(-60)}]}))
        self.assertIsNone(self.alert({**provider, 'display_stale': True}))
        self.assertIsNone(self.alert({**provider, 'unavailable_reason': 'no-data'}))
        # A confirmed unusable entitlement outranks its stale windows: no
        # countdown or fill alert may fire for access that is not usable.
        self.assertIsNone(self.alert({**provider, 'access_state': 'unavailable'}))
        self.assertIsNone(self.alert({**provider, 'access_state': 'paused'}))

    def test_fullest_window_wins_then_soonest_reset(self):
        a = self.alert({'windows': [
            {'label': 'A', 'used_percent': 90, 'reset_at': iso(600)},
            {'label': 'B', 'used_percent': 97, 'reset_at': iso(5 * 86400)},
        ]})
        self.assertEqual(a['reset']['window'], 'B')
        self.assertEqual(a['reset']['text'], '5d 0h')

    def test_time_to_full_when_forecast_fills_before_reset(self):
        provider = {'windows': [{'label': 'Session', 'used_percent': 70, 'reset_at': iso(4 * 3600)}]}
        fills = [{'provider': 'anthropic', 'window': 'Session', 'per_hour': 15.0, 'eta_full': NOW + 2400,
                  'fills_before_reset': True}]
        a = self.alert(provider, [{**fills[0], 'eta_full': int(NOW) + 2400}])
        self.assertIsNone(a['reset'])  # 70 % used: no countdown yet
        self.assertEqual(a['full']['text'], '40m')
        self.assertTrue(a['full']['soon'])
        safe = [{'provider': 'anthropic', 'window': 'Session', 'per_hour': 1.0, 'eta_full': NOW + 30 * 3600,
                 'fills_before_reset': False}]
        self.assertIsNone(self.alert(provider, safe))

    def test_worst_mode_merges_fullest_reset_and_earliest_fill(self):
        out = run(expr='mergeChipAlerts([null, {reset: {used: 90, seconds: 60, text: "1m"}, full: null},'
                       ' {reset: {used: 99, seconds: 900, text: "15m"}, full: {seconds: 5000, text: "1h 23m"}},'
                       ' {reset: null, full: {seconds: 300, text: "5m", soon: true}}])')['value']
        self.assertEqual(out['reset']['text'], '15m')
        self.assertEqual(out['full']['text'], '5m')
        self.assertIsNone(run(expr='mergeChipAlerts([null, null])')['value'])

    def test_status_chip_shows_countdown_and_fill_and_toggle_hides_them(self):
        # quota_payload(): Current session 96 % used, resets in ~2 h; the
        # snapshot's live burn says it fills in 40 min, before the reset.
        out = run(component='status', data=quota_payload(), monitor=snapshot())
        resets = attr(out['tree'], 'data-chip-reset')
        self.assertEqual(len(resets), 1)
        self.assertRegex(resets[0], r'^(1h 59m|2h 0m)$')
        self.assertRegex(attr(out['tree'], 'data-chip-full')[0], r'^(39m|40m)$')
        chip = find(out['tree'], lambda n: n.get('type') == 'button')[0]
        self.assertRegex(chip['props']['aria-label'], r'resets in (1h 59m|2h 0m) · full in ~(39m|40m)')
        self.assertRegex(text(chip), r'full ~(39m|40m)')
        # The fill time is red when it lands within the hour.
        full = find(out['tree'], lambda n: 'data-chip-full' in n.get('props', {}))[0]
        self.assertEqual(full['props']['style']['color'], 'var(--ui-red)')
        worst = run(component='status', data=quota_payload(), monitor=snapshot(), storage={'statusbarMode': 'worst'})
        self.assertEqual(len(attr(worst['tree'], 'data-chip-reset')), 1)
        off = run(component='status', data=quota_payload(), monitor=snapshot(), storage={'statusbarQuotaAlerts': False})
        self.assertEqual(attr(off['tree'], 'data-chip-alert'), [])
        # Old backend (no monitor): countdown still works, no forecast.
        old = run(component='status', data=quota_payload(), monitor={'unsupported': True})
        self.assertEqual(len(attr(old['tree'], 'data-chip-reset')), 1)
        self.assertEqual(attr(old['tree'], 'data-chip-full'), [])

    def test_closed_chip_builds_no_card(self):
        out = run(component='status', data=quota_payload(), monitor=snapshot())
        self.assertEqual(find(out['tree'], lambda n: n.get('type') == 'PopoverContent'), [])
        # The saved-pool siblings merge into ONE status cell (one Popover root);
        # the second root in the full bar is the agents chip, which this
        # StatusBar-only expression does not render.
        self.assertEqual(len(find(out['tree'], lambda n: n.get('type') == 'Popover')), 1)


class CostChipTest(unittest.TestCase):
    def test_tinting_thresholds(self):
        expr = '''[
          costChipState({today: 50, mtd: 50}, 100, 0).tone,
          costChipState({today: 80, mtd: 80}, 100, 0).tone,
          costChipState({today: 99.9, mtd: 99.9}, 100, 0).tone,
          costChipState({today: 100, mtd: 100}, 100, 0).tone,
          costChipState({today: 10, mtd: 10}, 0, 0).tone,
          costChipState({today: 10, mtd: 950}, 100, 1000).worst.kind,
          costChipState({today: 10, mtd: 950}, 100, 1000).tone,
          budgetTone(NaN),
        ]'''
        self.assertEqual(run(expr=expr)['value'], ['good', 'warn', 'warn', 'bad', None, 'monthly', 'warn', None])

    def test_short_money(self):
        out = run(expr='[0, 4.2, 46.5, 99.99, 460.67, 999.4, 1234, 9999, 12500, -3].map(fmtUsdShort)')['value']
        self.assertEqual(out, ['$0.00', '$4.20', '$46.50', '$99.99', '$461', '$999', '$1.2k', '$10k', '$13k', '-$3.00'])

    def test_default_on_only_with_a_budget(self):
        out = run(expr='[costChipEnabled(null, 0, 0), costChipEnabled(null, 5, 0), costChipEnabled(null, 0, 50),'
                       ' costChipEnabled(false, 5, 50), costChipEnabled(true, 0, 0)]')['value']
        self.assertEqual(out, [False, True, True, False, True])
        self.assertIsNone(run(component='cost', data=quota_payload(), monitor=snapshot())['tree'])

    def test_chip_tint_text_and_underline(self):
        # usage(): $12.50 today against a $10 daily budget -> over (red).
        out = run(component='cost', data=quota_payload(), monitor=snapshot(), storage={'budgetDaily': 10})
        chip = find(out['tree'], lambda n: 'data-cost-chip' in n.get('props', {}))[0]
        self.assertEqual(chip['props']['data-cost-chip'], 'bad')
        self.assertEqual(text(chip).strip(), '$12.50')
        self.assertEqual(attr(out['tree'], 'data-cost-bar'), [125])
        self.assertIn('125% of the daily budget', chip['props']['aria-label'])
        warn = run(component='cost', data=quota_payload(), monitor=snapshot(), storage={'budgetDaily': 15})
        self.assertEqual(attr(warn['tree'], 'data-cost-chip'), ['warn'])
        # Forced on without a budget: plain value, no tint, no underline.
        plain = run(component='cost', data=quota_payload(), monitor=snapshot(), storage={'statusbarCost': True})
        self.assertEqual(attr(plain['tree'], 'data-cost-chip'), ['none'])
        self.assertEqual(attr(plain['tree'], 'data-cost-bar'), [])
        # Switched off despite a budget; old backend renders nothing.
        self.assertIsNone(run(component='cost', data=quota_payload(), monitor=snapshot(),
                              storage={'budgetDaily': 10, 'statusbarCost': False})['tree'])
        self.assertIsNone(run(component='cost', data=quota_payload(), monitor={'unsupported': True},
                              storage={'budgetDaily': 10})['tree'])

    def test_card_top_models_today_and_link(self):
        u = usage()
        u['today']['by_model'] = [
            {'model': 'claude-test-small', 'tokens': 10, 'cost': 1.0},
            {'model': 'claude-test-large', 'tokens': 99, 'cost': 9.0},
            {'model': 'claude-test-mid', 'tokens': 50, 'cost': 4.0},
            {'model': 'claude-test-tiny', 'tokens': 1, 'cost': 0.1},
        ]
        snap = snapshot(usage=u)
        expr = '(() => {' + CLICK_JS + f'''
          let closed = 0;
          const tree = CostCard({{snapshot: {json.dumps(snap)}, close: () => closed++}});
          __find(tree, 'data-card-link')[0].props.onClick();
          return {{closed, tree}};
        }})()'''
        out = run(expr=expr, storage={'budgetDaily': 10, 'budgetMonthly': 40})
        s = text(out['value']['tree'])
        self.assertLess(s.index('claude-test-large'), s.index('claude-test-mid'))
        self.assertLess(s.index('claude-test-mid'), s.index('claude-test-small'))
        self.assertNotIn('claude-test-tiny', s)
        for part in ('$12.50 / $10.00', 'estimated API cost', 'Top models · today'):
            self.assertIn(part, s)
        self.assertNotIn('Month-end', s)
        self.assertEqual(out['value']['closed'], 1)
        self.assertEqual(out['storage']['paneView'], 'usage')


class AgentsFlashTest(unittest.TestCase):
    def flashes(self, steps):
        """Feed successive `live.running` lists; return the flash kind per step."""
        expr = f'''(() => {{
          let prev = null;
          return {json.dumps(steps)}.map((running) => {{
            const r = attentionFlash(prev, {{running}});
            prev = r.next;
            return r.kind;
          }});
        }})()'''
        return run(expr=expr)['value']

    @staticmethod
    def row(i, state, **extra):
        return {'id': f's{i}', 'status': 'running', 'state': state, **extra}

    def test_only_newly_entered_states_flash(self):
        r = self.row
        kinds = self.flashes([
            [r(1, 'waiting'), r(2, 'working')],             # first look seeds: no flash
            [r(1, 'waiting'), r(2, 'working')],             # unchanged
            [r(1, 'waiting'), r(2, 'waiting')],             # s2 finished -> your turn
            [r(1, 'waiting'), r(2, 'waiting')],             # still waiting: no re-flash
            [r(1, 'needs_approval'), r(2, 'waiting')],      # s1 asks approval
            [r(1, 'working'), r(2, 'waiting'), r(3, 'waiting', subagent=True)],  # subagents never flash
            [r(1, 'waiting'), r(2, 'working'), r(4, 'needs_approval')],  # approval outranks your turn
            [r(1, 'waiting', status='ended'), r(2, 'working')],  # ended: nothing
        ])
        self.assertEqual(kinds, [None, None, 'waiting', None, 'needs_approval', None, 'needs_approval', None])

    def test_segments_and_series(self):
        out = run(expr='[agentChipSegments({open: 7, working: 1, your_turn: 5, stalled: 1, needs_approval: 0}),'
                       ' agentChipSegments({open: 2}),'
                       ' tokenSeries({tokens: [{by: {a: {tokens: 5}}}, {by: {a: 3, b: {tokens: 4}}}, {by: {}}]}, 2)]')['value']
        self.assertEqual(out[0], [{'state': 'working', 'n': 1}, {'state': 'waiting', 'n': 5}, {'state': 'stalled', 'n': 1}])
        self.assertEqual(out[1], [{'state': 'working', 'n': 0}])
        self.assertEqual(out[2], [7, 0])

    def test_chip_sparkline_toggle(self):
        on = run(component='agents', data=quota_payload(), monitor=snapshot())
        self.assertTrue(find(on['tree'], lambda n: n.get('props', {}).get('data-chip-spark')))
        off = run(component='agents', data=quota_payload(), monitor=snapshot(), storage={'statusbarSparkline': False})
        self.assertFalse(find(off['tree'], lambda n: n.get('props', {}).get('data-chip-spark')))

    def test_flash_class_needs_switch(self):
        # useState/useEffect are inert in this harness, so drive the pure part
        # and check the switch the effect consults.
        out = run(expr='(applyStoredMonitor(), chipFlashAtom.get())', storage={'statusbarFlash': False})
        self.assertFalse(out['value'])
        css = run(expr='STATUSBAR_CSS')['value']
        self.assertIn('prefers-reduced-motion:reduce', css)
        # Global sheet: every class and keyframe name carries the plugin prefix.
        self.assertTrue(all(c.startswith('qm-sb-') for c in re.findall(r'\.([a-z][\w-]*)', css)))
        self.assertTrue(all(k.startswith('qm-sb-') for k in re.findall(r'@keyframes ([\w-]+)', css)))


class AgentsCardTest(unittest.TestCase):
    def card(self, snap):
        expr = '(async () => {' + CLICK_JS + f'''
          let closed = 0;
          const snap = {json.dumps(snap)};
          const tree = AgentsCard({{snapshot: snap, scope: host.state.focusedSessionOwner.get(), close: () => closed++}});
          const rows = __find(tree, 'data-card-row');
          const groups = __find(tree, 'data-card-group').map((n) => n.props['data-card-group']);
          for (const id of ['sess_work_2', 'cc-uuid-1']) rows.find((r) => r.props['data-card-row'] === id).props.onClick();
          await new Promise((r) => setTimeout(r, 5));
          __find(tree, 'data-card-link')[0].props.onClick();
          return {{closed, groups, rows: rows.map((r) => [r.props['data-card-row'], r.props['data-row-action']])}};
        }})()'''
        return run(expr=expr)

    def test_rows_grouped_by_state_with_actions(self):
        out = self.card(snapshot())
        v = out['value']
        self.assertEqual(v['groups'], ['needs_approval', 'waiting', 'working'])
        self.assertEqual(v['rows'], [['sess_work_2', 'open'], ['cc-uuid-1', 'copy'], ['sess_work_1', 'open']])
        # Hermes row -> openSession; CLI row -> clipboard; each pick closes, so
        # does the footer link, which lands on the Live tab.
        self.assertEqual([o['id'] for o in out['opened']], ['sess_work_2'])
        self.assertEqual(out['clipboard'], ['claude --resume cc-uuid-1'])
        self.assertEqual(v['closed'], 3)
        self.assertEqual(out['storage']['paneView'], 'live')

    def test_long_groups_are_capped(self):
        snap = snapshot()
        snap['live']['running'] = [
            {'id': f'w{i}', 'kind': 'hermes', 'profile': 'work', 'status': 'running', 'state': 'waiting',
             'title': f'Task {i}', 'last': NOW - i} for i in range(8)]
        tree = run(expr=f'AgentsCard({{snapshot: {json.dumps(snap)}, scope: null, close: () => {{}}}})')['value']
        s = text(tree)
        self.assertIn('Task 0', s)
        self.assertNotIn('Task 6', s)
        self.assertIn('+3 more', s)


class QuotaCardTest(unittest.TestCase):
    def test_rings_resets_forecast_and_link(self):
        expr = '(() => {' + CLICK_JS + '''
          let closed = 0;
          const tree = QuotaCard({close: () => closed++});
          const windows = __find(tree, 'data-card-window').map((n) => n.props['data-card-window']);
          const forecasts = __find(tree, 'data-card-forecast').map((n) => n.props['data-card-forecast']);
          const providers = __find(tree, 'data-card-provider').map((n) => n.props['data-card-provider']);
          __find(tree, 'data-card-link')[0].props.onClick();
          return {closed, windows, forecasts, providers, tree};
        })()'''
        out = run(expr=expr, data=quota_payload(), monitor=snapshot())
        v = out['value']
        self.assertEqual(v['providers'], ['anthropic', 'anthropic:home'])
        self.assertEqual(v['windows'], ['Current session', 'Weekly', 'Current session'])
        self.assertIn('Current session', v['forecasts'])
        s = text(v['tree'])
        self.assertRegex(s, r'full in ~(39m|40m)')
        self.assertIn('↻', s)
        self.assertEqual(v['closed'], 1)
        self.assertEqual(out['storage']['paneView'], 'quota')
        # Disabled providers drop out of the card too.
        out = run(expr=expr, data=quota_payload(), monitor=snapshot(), storage={'disabledProviders': ['anthropic:home']})
        self.assertEqual(out['value']['providers'], ['anthropic'])


class SettingsTogglesTest(unittest.TestCase):
    def test_status_bar_card_lists_every_chip_switch(self):
        s = text(run(component='settings', data=quota_payload(), monitor=snapshot())['tree'])
        for label in ('Reset countdown and time-to-full on the quota chip',
                      'Tokens / minute sparkline in the agents chip',
                      'Flash the agents chip when a session needs you',
                      'Spend-today chip'):
            self.assertIn(label, s)
        tr = text(run(component='settings', data=quota_payload(), monitor=snapshot(), locale='tr')['tree'])
        self.assertIn('Bugünkü harcama çipi', tr)


if __name__ == '__main__':
    unittest.main()
