"""Agent-monitor views of the desktop widget, rendered offline.

Runs the real `desktop/plugin.js` through `tests/widget_monitor_renderer.cjs`
against SYNTHETIC `hermes quota monitor ...` payloads (names like work/home,
anthropic#0a1b2c3d — never real data) and asserts what each view shows, the
notification rules and their dedupe, the open/copy actions, the old-backend
notice, and the en/tr bundles.
"""
import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / 'widget_monitor_renderer.cjs'

from widget_harness import nodes, text  # noqa: E402


NOTIFY_STORAGE_DEFAULTS = {
    'notifyQuota': True, 'notifyBudget': True, 'notifyKanban': True, 'notifyHealth': True,
    'notifyAgents': True, 'notifyTurns': True, 'notifyContext': True,
}


def run(**options):
    # The read-only trial ships the monitor and every notification kind OFF;
    # widget tests exercise the explicitly enrolled behaviour, so they opt in
    # here. A test can pass enroll=False to observe the shipped defaults.
    if options.pop('enroll', True):
        storage = dict(options.get('storage') or {})
        storage.setdefault('monitorEnabled', True)
        for key, value in NOTIFY_STORAGE_DEFAULTS.items():
            storage.setdefault(key, value)
        options['storage'] = storage
    # Freeze the renderer's clock at NOW: every payload is relative to it.
    options.setdefault('now', NOW * 1000)
    p = subprocess.run(
        ['node', str(HARNESS)], input=json.dumps(options), text=True,
        capture_output=True, timeout=30,
        env={**os.environ, 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TZ': 'UTC'},
    )
    if p.returncode:
        raise AssertionError(p.stderr)
    return json.loads(p.stdout)


NOW = time.time()


def day(offset=0):
    return time.strftime('%Y-%m-%d', time.gmtime(NOW + offset * 86400))


def iso(seconds_from_now):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW + seconds_from_now))


def quota_payload():
    return {'fetched_at': iso(-30), 'age_s': 30, 'providers': {
        'anthropic': {'account_label': 'work', 'windows': [
            {'label': 'Current session', 'used_percent': 96, 'reset_at': iso(7200)},
            {'label': 'Weekly', 'used_percent': 40, 'reset_at': iso(5 * 86400)}]},
        'anthropic:home': {'account_label': 'home', 'windows': [
            {'label': 'Current session', 'used_percent': 10, 'reset_at': iso(9000)}]},
    }}


def usage(cost_today=12.5, month_days=None):
    month_days = month_days if month_days is not None else [(0, cost_today), (-1, 30.0), (-2, 7.5)]
    daily = [{'day': day(o), 'by': {'anthropic': {'tokens': 1000, 'cost': c}}, 'sessions': 3} for o, c in month_days]
    return {
        'today': {'totals': {'cost': cost_today, 'tokens': 420000}, 'daily': daily[:1]},
        'month': {'days': [d['day'] for d in daily], 'totals': {'cost': sum(c for _, c in month_days)},
                  'daily': daily,
                  'by_model': [{'model': 'claude-test-large', 'tokens': 900000, 'cost': 40.0, 'provider': 'anthropic'},
                               {'model': 'claude-test-small', 'tokens': 90000, 'cost': 2.5, 'provider': 'anthropic'}],
                  'by_profile': [{'profile': 'work', 'tokens': 800000, 'cost': 35.0, 'sessions': 9},
                                 {'profile': 'home', 'tokens': 10000, 'cost': 1.0, 'sessions': 2}]},
    }


def snapshot(**over):
    snap = {
        'monitor_version': 1,
        'generated_at': NOW,
        'live': {
            'now': NOW, 'last_1m': 1500, 'last_5m': 6000, 'last_60m': 120000, 'cost_60m': 6.0,
            'tokens': [{'t': NOW - 120, 'by': {'hermes': {'tokens': 100, 'cost': 0.1}}},
                       {'t': NOW - 60, 'by': {'hermes': {'tokens': 900, 'cost': 0.4}}}],
            'counts': {'open': 3, 'working': 1, 'needs_approval': 1, 'your_turn': 1, 'stalled': 0, 'subagents': 1},
            'running': [
                {'id': 'sess_work_1', 'kind': 'hermes', 'profile': 'work', 'source_name': 'Hermes', 'status': 'running',
                 'state': 'working', 'title': 'Refactor the parser', 'model': 'claude-test-large', 'tokens': 52000,
                 'cost': 1.25, 'context_tokens': 81000, 'last': NOW - 20, 'activity': 'terminal: make test',
                 'long_tool_call': None, 'subagent': False,
                 'open': {'kind': 'hermes-session', 'command': 'hermes --resume sess_work_1'}},
                {'id': 'sess_work_2', 'kind': 'hermes', 'profile': 'work', 'source_name': 'Hermes', 'status': 'running',
                 'state': 'needs_approval', 'title': 'Deploy preview', 'model': 'claude-test-small', 'tokens': 800,
                 'cost': 0.02, 'last': NOW - 90, 'subagent': False,
                 'open': {'kind': 'hermes-session', 'command': 'hermes --resume sess_work_2'}},
                {'id': 'sub_1', 'kind': 'hermes', 'profile': 'work', 'status': 'running', 'state': 'working',
                 'title': 'Child task', 'subagent': True, 'parent': 'sess_work_1', 'tokens': 10, 'last': NOW - 5},
                {'id': 'cc-uuid-1', 'kind': 'claude-code', 'profile': None, 'source_name': 'Claude Code',
                 'status': 'running', 'state': 'waiting', 'title': 'Fix flaky test', 'model': 'claude-test-large',
                 'tokens': 3000, 'cost': 0.3, 'last': NOW - 400, 'subagent': False,
                 'long_tool_call': None,
                 'open': {'kind': 'claude-code', 'command': 'claude --resume cc-uuid-1'}},
            ],
            'burn': [{'provider': 'anthropic', 'window': 'Current session', 'used': 96, 'per_hour': 6.0,
                      'eta_full': NOW + 2400, 'fills_before_reset': True, 'reset': NOW + 7200},
                     {'provider': 'anthropic', 'window': 'Weekly', 'used': 40, 'per_hour': 0.5,
                      'eta_full': NOW + 120 * 3600, 'fills_before_reset': False}],
            'attention': [
                {'id': 'sess_work_2|approval', 'kind': 'approval', 'session_id': 'sess_work_2', 'profile': 'work',
                 'title': 'Deploy preview', 'since': NOW - 90, 'detail': 'terminal command'},
            ],
        },
        'health': {
            'now': NOW,
            'accounts': [{'provider': 'anthropic', 'credentials': [
                {'label': 'work', 'account_key': '0a1b2c3d', 'status': 'ok', 'locked': False},
                {'label': 'home', 'account_key': '4e5f6a7b', 'status': 'exhausted', 'locked': True,
                 'exhausted_until': NOW + 3600}]}],
            'models': [
                {'provider': 'anthropic', 'model': 'claude-test-large', 'status': 'ok', 'calls_24h': 400,
                 'error_rate': 0.0, 'errors_24h': 0},
                {'provider': 'openrouter', 'model': 'test/failing-model', 'status': 'failing', 'calls_24h': 20,
                 'error_rate': 0.45, 'errors_24h': 9, 'last_error': {'summary': 'HTTP 503: upstream unavailable'}},
            ],
            'chains': [{'profile': 'work', 'provider': 'anthropic', 'model': 'claude-test-large', 'status': 'ok',
                        'fallbacks': [{'provider': 'anthropic', 'model': 'claude-test-small', 'state': 'skip',
                                       'reason': 'same_backend'},
                                      {'provider': 'openrouter', 'model': 'test/failing-model', 'state': 'risky',
                                       'reason': 'error_rate', 'rate': 0.45}],
                        'usable': 1}],
            'speed': [{'source': 'hermes', 'model': 'claude-test-large', 'turns': 40, 'p50_s': 12.0, 'p90_s': 95.0}],
        },
        'kanban': {'boards': [{'board': 'default', 'counts': {'todo': 4, 'blocked': 1, 'running': 1, 'done': 9},
                               'cards': [
            {'id': 't_run1', 'title': 'Write release notes', 'assignee': 'writer', 'status': 'running',
             'open': {'kind': 'kanban-card', 'command': 'hermes kanban show t_run1'}},
            {'id': 't_blk1', 'title': 'Pick a logo', 'assignee': 'designer', 'status': 'blocked',
             'block_kind': 'needs_input', 'open': {'kind': 'kanban-card', 'command': 'hermes kanban show t_blk1'}},
            {'id': 't_rev1', 'title': 'Review API docs', 'assignee': 'reviewer', 'status': 'review'},
            {'id': 't_fail1', 'title': 'Nightly build', 'assignee': 'builder', 'status': 'todo',
             'consecutive_failures': 2, 'last_failure_error': 'exit 1'},
            {'id': 't_done1', 'title': 'Old finished card', 'assignee': 'writer', 'status': 'done'},
        ]}]},
        'usage': usage(),
        'top_sessions': [{'source': 'hermes', 'profile': 'work', 'id': 'sess_top_1', 'title': 'Big migration',
                          'tokens': 700000, 'cost': 25.0},
                         {'source': 'claude-code', 'profile': None, 'id': 'cc-top', 'title': 'CLI session',
                          'tokens': 5000, 'cost': 0.5}],
        'clis': {},
        'errors': {},
    }
    snap.update(over)
    return snap


def find(tree, pred):
    return [n for n in nodes(tree) if pred(n)]


class MonitorViewsTest(unittest.TestCase):
    def pane(self, view, storage=None, **extra):
        return run(component='pane', data=quota_payload(), monitor=snapshot(),
                   storage={'paneView': view, **(storage or {})}, **extra)

    def test_monitor_and_notifications_ship_off_until_enrolled(self):
        # Read-only trial: no monitor spawn and no OS notification before the
        # user explicitly enrolls. applyStoredMonitor() is the boot path.
        out = run(expr='(applyStoredMonitor(), {monitor: monitorEnabledAtom.get(), '
                       'notify: NOTIFY_KINDS.map((k) => notifyAtoms[k].get())})',
                  enroll=False, storage={})
        self.assertEqual(out['value']['monitor'], False)
        self.assertEqual(out['value']['notify'], [False] * 7)
        # And an unenrolled pane never asks for a monitor snapshot.
        off = run(component='pane', data=quota_payload(), storage={'paneView': 'live'}, enroll=False)
        self.assertEqual([q for q in off['queries'] if q['key'][1] == 'monitor' and q.get('enabled')], [])

    def test_live_view_groups_states_and_metrics(self):
        out = self.pane('live')
        s = text(out['tree'])
        for expected in ('Refactor the parser', 'Deploy preview', 'Fix flaky test', 'Child task',
                         'working', 'needs approval', 'your turn', '52k tokens', '$1.25', 'ctx 81k',
                         'Claude Code', 'Needs attention (1)', 'work needs approval', '1.5k', '≈ $0.10 / min'):
            self.assertIn(expected, s)
        states = [n['props'].get('data-state') for n in find(out['tree'], lambda n: 'data-state' in n.get('props', {}))]
        self.assertIn('needs_approval', states)
        self.assertIn('waiting', states)
        # Approval sorts before working inside the profile group.
        self.assertLess(s.index('Deploy preview'), s.index('Refactor the parser'))
        # Token sparkline is an inline SVG with theme colours only.
        sparks = find(out['tree'], lambda n: n.get('props', {}).get('data-sparkline'))
        self.assertTrue(sparks)
        self.assertIn('var(--ui-', json.dumps(sparks[0]))
        self.assertEqual(sparks[0]['props']['preserveAspectRatio'], 'none')

    def test_health_view(self):
        snap = snapshot()
        snap['health']['accounts'].append({'provider': 'cursor', 'credentials': [], 'unavailable': 'no-credentials'})
        snap['health']['accounts'].append({'provider': 'copilot', 'credentials': [{'label': 'gh auth token', 'status': None}],
                                           'unavailable_code': 'no-data', 'unavailable': 'opt-in-disabled'})
        s = text(run(component='pane', data=quota_payload(), monitor=snap, storage={'paneView': 'health'})['tree'])
        self.assertIn('Not set up here (2): Cursor, Copilot', s)
        for expected in ('Accounts', 'limit reached', 'unlocks in', 'test/failing-model', 'failing', '45% errors',
                         'HTTP 503: upstream unavailable', 'Fallback chains', 'skipped: same backend as primary',
                         'risky (45% errors)', 'median 12s · p90 1m · 40 turns'):
            self.assertIn(expected, s)
        # Failing models list first.
        self.assertLess(s.index('test/failing-model'), s.index('claude-test-large'))

    def test_usage_view_budget_and_estimate_without_projection(self):
        out = self.pane('usage', storage={'paneView': 'usage', 'budgetDaily': 10, 'budgetMonthly': 40})
        s = text(out['tree'])
        for expected in ('Today', '$12.50', 'This month', 'estimated API price', 'Budget', '$12.50 / $10.00 · 125%',
                         'Last 7 days', 'Top models · month', 'claude-test-large', 'Top profiles · month',
                         'Top sessions · 7 days', 'Big migration', '$25.00'):
            self.assertIn(expected, s)
        self.assertNotIn('Month-end', s)
        budgets = [n['props']['data-budget'] for n in find(out['tree'], lambda n: 'data-budget' in n.get('props', {}))]
        self.assertEqual(budgets, ['Daily', 'Monthly'])

    def test_no_projection_render_when_capability_unset_or_false(self):
        for marker in ({}, {'projection_available': False}):
            with self.subTest(capability=marker):
                snap = snapshot()
                snap['usage'].update(marker)
                out = run(component='pane', data=quota_payload(), monitor=snap,
                          storage={'paneView': 'usage', 'budgetDaily': 10, 'budgetMonthly': 40})
                s = text(out['tree'])
                self.assertIn('estimated API price', s)
                self.assertNotIn('Month-end', s)
                self.assertNotIn('month-end', s)
                card = text(run(expr='CostCard({snapshot: useMonitor().snapshot, close(){}})', data=quota_payload(), monitor=snap)['value'])
                self.assertNotIn('Month-end', card)
                self.assertNotIn('≈ $', card)
                self.assertIn('estimated API cost', card)

    def test_usage_view_without_budget_hints_settings(self):
        s = text(self.pane('usage')['tree'])
        self.assertIn('Set a daily or monthly budget in Settings', s)

    def test_kanban_view_groups(self):
        out = self.pane('kanban')
        s = text(out['tree'])
        groups = [n['props']['data-kanban-group'] for n in find(out['tree'], lambda n: 'data-kanban-group' in n.get('props', {}))]
        self.assertEqual(groups, ['running', 'blocked', 'review', 'failing'])
        for expected in ('Write release notes', 'Pick a logo', 'needs your input', 'Review API docs',
                         'Nightly build', '2× failed', 'exit 1'):
            self.assertIn(expected, s)
        self.assertNotIn('Old finished card', s)

    def test_quota_view_forecast_history_and_account_hint(self):
        history = {'monitor_version': 1, 'generated_at': NOW,
                   'history': [{'provider': 'anthropic', 'window': 'Current session',
                                'points': [[NOW - 3600, 80], [NOW - 1800, 88], [NOW, 96]]}],
                   'burn': []}
        out = run(component='pane', data=quota_payload(), monitor=snapshot(), history=history,
                  storage={'paneView': 'quota'})
        s = text(out['tree'])
        self.assertIn('+6.0%/h', s)
        self.assertRegex(s, r'full in ~(39|40)m')
        self.assertIn('before reset', s)
        self.assertIn("won't fill before reset", s)
        forecasts = [n['props']['data-forecast'] for n in find(out['tree'], lambda n: 'data-forecast' in n.get('props', {}))]
        self.assertIn('fills', forecasts)
        self.assertIn('ok', forecasts)
        self.assertTrue(find(out['tree'], lambda n: n.get('props', {}).get('data-sparkline')))
        self.assertIn('Anthropic · work is at 96% · Anthropic · home has room', s)
        # History is only queried while the Quota view shows it.
        hist = [q for q in out['queries'] if str(q['key'][2]).startswith('history')]
        self.assertTrue(hist and hist[-1]['enabled'])
        live = run(component='pane', data=quota_payload(), monitor=snapshot(), storage={'paneView': 'live'})
        hist = [q for q in live['queries'] if str(q['key'][2]).startswith('history')]
        self.assertFalse(any(q['enabled'] for q in hist))

    def test_tabs_and_settings(self):
        out = self.pane('quota')
        tabs = find(out['tree'], lambda n: 'data-tab' in n.get('props', {}))
        self.assertEqual([n['props']['data-tab'] for n in tabs], ['quota', 'live', 'health', 'usage', 'kanban', 'accounts'])
        self.assertEqual([n['props']['aria-selected'] for n in tabs], [True, False, False, False, False, False])
        settings = text(run(component='settings', data=quota_payload())['tree'])
        for expected in ('Agent monitor', 'Show working / your-turn chip', 'Burn rate, forecast and history',
                         'History range', 'Budget', 'Daily', 'Monthly', 'Notifications', 'Budget 80% / 100%',
                         'Agent finished — your turn', 'Failing models and locked accounts'):
            self.assertIn(expected, settings)

    def test_monitor_off_hides_tabs(self):
        out = run(component='pane', data=quota_payload(), monitor=snapshot(),
                  storage={'paneView': 'live', 'monitorEnabled': False})
        self.assertFalse(find(out['tree'], lambda n: n.get('props', {}).get('data-monitor-tabs')))
        self.assertIn('Anthropic · work', text(out['tree']))  # falls back to the quota list

    def test_old_backend_shows_quiet_notice(self):
        out = run(component='pane', data=quota_payload(), monitor={'unsupported': True}, storage={'paneView': 'health'})
        notice = find(out['tree'], lambda n: n.get('props', {}).get('data-monitor-notice'))
        self.assertEqual(notice[0]['props']['data-monitor-notice'], 'unsupported')
        self.assertIn('update it (./install.sh)', text(out['tree']))
        # The quota view is untouched by an old backend: no tabs-induced errors, no extras.
        q = run(component='pane', data=quota_payload(), monitor={'unsupported': True}, storage={'paneView': 'quota'})
        s = text(q['tree'])
        self.assertIn('Anthropic · work', s)
        self.assertNotIn('%/h', s)
        self.assertNotIn('has room', s)
        # No agents chip either.
        self.assertIsNone(run(component='agents', data=quota_payload(), monitor={'unsupported': True})['tree'])

    def test_fetch_monitor_detects_old_backend(self):
        old = run(expr='fetchMonitor(host.state.focusedSessionOwner.get(), ["quota","monitor","snapshot","--json"])',
                  cliResult={'code': 2, 'output': "hermes quota: error: argument quota_command: invalid choice: 'monitor'"})
        self.assertEqual(old['value'], {'unsupported': True})
        self.assertEqual(old['cli'][0]['argv'], ['--profile', 'work', 'quota', 'monitor', 'snapshot', '--json'])
        ok = run(expr='fetchMonitor(host.state.focusedSessionOwner.get(), ["quota","monitor","snapshot","--json"])',
                 cliResult={'code': 0, 'output': 'warning: noise\n' + json.dumps({'monitor_version': 1, 'live': {}})})
        self.assertEqual(ok['value']['monitor_version'], 1)
        with self.assertRaises(AssertionError):
            run(expr='fetchMonitor(host.state.focusedSessionOwner.get(), ["quota","monitor","snapshot","--json"])',
                cliResult={'code': 1, 'output': 'Traceback: something else broke'})

    def test_agents_chip(self):
        out = run(component='agents', data=quota_payload(), monitor=snapshot())
        tree = out['tree']
        # Compact form: coloured dot + number per non-zero state; the words
        # live in the aria-label and the tip.
        chip = find(tree, lambda n: n.get('props', {}).get('data-agents-chip'))[0]
        self.assertIn('1 working · 1 your turn · 1 approval', chip['props']['aria-label'])
        segs = [n['props']['data-seg'] for n in find(tree, lambda n: 'data-seg' in n.get('props', {}))]
        self.assertEqual(segs, ['needs_approval', 'working', 'waiting'])
        self.assertEqual(text(chip).split(), ['1', '1', '1'])
        off = run(component='agents', data=quota_payload(), monitor=snapshot(), storage={'statusbarAgents': False})
        self.assertIsNone(off['tree'])

    def test_turkish_bundle(self):
        out = run(component='pane', data=quota_payload(), monitor=snapshot(), storage={'paneView': 'live'}, locale='tr')
        s = text(out['tree'])
        self.assertIn('onay bekliyor', s)
        self.assertIn('sıra sende', s)
        self.assertIn('İlgi bekliyor (1)', s)

    def test_every_english_key_has_turkish(self):
        out = run(expr='[Object.keys(I18N_EN).filter((k) => !(k in I18N_TR)), Object.keys(I18N_TR).filter((k) => !(k in I18N_EN))]')
        self.assertEqual(out['value'], [[], []])
        # Every literal t("key")/tr("key") used by the widget exists in the en bundle.
        source = (ROOT / 'desktop' / 'plugin.js').read_text(encoding='utf-8')
        used = set(re.findall(r'\bt\(\s*"([A-Za-z0-9_]+)"', source)) | set(re.findall(r'\btr\(\s*"([A-Za-z0-9_]+)"', source))
        keys = set(run(expr='Object.keys(I18N_EN)')['value'])
        self.assertEqual(sorted(used - keys), [])


class MonitorActionsTest(unittest.TestCase):
    def test_hermes_row_opens_session_cli_row_copies(self):
        expr = '''(async () => {
          const scope = host.state.focusedSessionOwner.get();
          const snap = TEST_SNAPSHOT;
          const a = await openMonitorTarget(scope, {open: snap.live.running[0].open, row: snap.live.running[0], sessionId: 'sess_work_1', profile: 'work'}, snap);
          const b = await openMonitorTarget(scope, {open: snap.live.running[3].open, row: snap.live.running[3]}, snap);
          const c = await openMonitorTarget(scope, {sessionId: 'sess_work_2', profile: 'work'}, snap);
          const d = await openMonitorTarget(scope, {sessionId: 'cc-uuid-1', profile: 'Claude Code', source: 'claude-code'}, snap);
          return [a, b, c, d];
        })()'''
        out = run(expr=expr.replace('TEST_SNAPSHOT', json.dumps(snapshot())))
        self.assertEqual(out['value'], ['opened', 'copied', 'opened', 'copied'])
        self.assertEqual([o['id'] for o in out['opened']], ['sess_work_1', 'sess_work_2'])
        self.assertEqual(out['opened'][0]['opts'], {'profile': 'work'})
        self.assertEqual(out['clipboard'], ['claude --resume cc-uuid-1'] * 2)
        self.assertIn('Copied: claude --resume cc-uuid-1', out['toasts'][0]['message'])

    def test_budget_stays_local_without_backend_writes(self):
        out = run(expr='(setBudget("daily", "12.345"), setBudget("monthly", -3), syncBudgetToBackend())')
        self.assertFalse(out['value'])
        self.assertEqual(out['storage']['budgetDaily'], 12.35)
        self.assertEqual(out['storage']['budgetMonthly'], 0)
        self.assertEqual(out['cli'], [])

    def test_backend_error_payload_is_a_quiet_error(self):
        out = run(component='pane', data=quota_payload(), monitor={'monitor_version': 1, 'error': 'RuntimeError: x'},
                  storage={'paneView': 'live'})
        self.assertIn('Agent monitor unavailable right now.', text(out['tree']))


class MonitorNotificationTest(unittest.TestCase):
    """Rules + dedupe, through the real runner (storage + ctx.os.notify)."""

    def notify_twice(self, first, second, storage=None, providers=None):
        expr = '''(() => {
          const scope = host.state.focusedSessionOwner.get();
          const providers = PROVIDERS;
          const a = runMonitorNotifier(scope, providers, FIRST).map((n) => n.kind + ':' + n.title);
          const b = runMonitorNotifier(scope, providers, SECOND).map((n) => n.kind + ':' + n.title);
          return [a, b];
        })()'''
        expr = (expr.replace('PROVIDERS', json.dumps(providers if providers is not None else quota_payload()['providers']))
                .replace('FIRST', json.dumps(first)).replace('SECOND', json.dumps(second)))
        return run(expr=expr, storage=storage or {})

    def test_threshold_rules_fire_once_per_period(self):
        out = self.notify_twice(snapshot(), snapshot(generated_at=NOW + 60),
                                storage={'budgetDaily': 10, 'budgetMonthly': 1000})
        first, second = out['value']
        self.assertIn('quota:Quota at 96%', first)
        self.assertIn('soon:Quota filling up soon', first)
        self.assertTrue(any(k.startswith('hint:Anthropic · work is at 96%') for k in first))
        self.assertIn('budget:Daily budget at 125%', first)
        self.assertFalse(any(k.startswith('budget:Monthly') for k in first))
        # Same data again (a later poll): nothing new — dedupe holds.
        self.assertEqual(second, [])
        self.assertEqual(len(out['notified']), len(first))
        # Persisted per focused profile, so a reload does not repeat them.
        self.assertIn('monitorNotified:local:work', out['storage'])

    def test_set_rules_seed_then_fire_on_new_items(self):
        base = snapshot()
        later = json.loads(json.dumps(base))
        later['generated_at'] = NOW + 60
        later['live']['attention'].append({'id': 'cc-uuid-1|your_turn', 'kind': 'your_turn', 'session_id': 'cc-uuid-1',
                                           'profile': 'home', 'title': 'Fix flaky test', 'since': NOW})
        later['kanban']['boards'][0]['cards'].append({'id': 't_blk2', 'title': 'Sign contract', 'assignee': 'lead',
                                                      'status': 'blocked'})
        later['health']['models'].append({'provider': 'anthropic', 'model': 'claude-test-small',
                                          'status': 'account_exhausted'})
        out = self.notify_twice(base, later, providers={})
        first, second = out['value']
        # First poll only seeds the kanban/health/attention sets.
        self.assertEqual(first, [])
        self.assertIn('attention:home finished — your turn', second)
        self.assertIn('kanban:Kanban: card blocked', second)
        self.assertIn('health:Model account limit: claude-test-small', second)
        self.assertEqual(len(second), 3)

    def test_toggles_silence_kinds_without_backlog(self):
        later = snapshot(generated_at=NOW + 60)
        later['live']['attention'] = [{'id': 'x|your_turn', 'kind': 'your_turn', 'session_id': 'x', 'profile': 'work'}]
        out = self.notify_twice(snapshot(), later, storage={'notifyQuota': False, 'notifyTurns': False})
        first, second = out['value']
        self.assertFalse([k for k in first if k.split(':')[0] in ('quota', 'soon', 'hint')])
        self.assertEqual(second, [])
        state = json.loads(out['storage']['monitorNotified:local:work'])
        # Crossed thresholds are still recorded, so re-enabling does not flood.
        self.assertTrue(state['quota'])

    def test_nav_badge_label(self):
        expr = '''(() => {
          const t = (k, ...a) => (typeof I18N_EN[k] === 'function' ? I18N_EN[k](...a) : I18N_EN[k]);
          const tr = (k, ...a) => (typeof I18N_TR[k] === 'function' ? I18N_TR[k](...a) : I18N_TR[k]);
          return [
            navBadge({working: 0, approval: 0, yourTurn: 0, stalled: 0}, 60, t),
            navBadge({working: 2, approval: 0, yourTurn: 3, stalled: 0}, 40, t),
            navBadge({working: 1, approval: 1, yourTurn: 0, stalled: 1}, 8, t),
            navBadge({working: 0, approval: 0, yourTurn: 3, stalled: 0}, null, tr),
          ];
        })()'''
        out = run(expr=expr)['value']
        self.assertEqual(out[0], {'label': 'Quota', 'codicon': 'pulse'})
        self.assertEqual(out[1]['label'], 'Quota · 2 active')
        self.assertIn('spin', out[1]['codicon'])
        self.assertEqual(out[2]['label'], 'Quota · 1 approval · 1 active · ⚠ 8%')
        self.assertEqual(out[2]['codicon'], 'bell-dot')
        self.assertEqual(out[3]['label'], 'Kota · 3 sıra sende')

    def test_pure_rules_function(self):
        expr = '''(() => {
          const r1 = monitorNotifications({snapshot: SNAP, providers: {}, settings: {}}, {}, NOW);
          const r2 = monitorNotifications({snapshot: SNAP, providers: {}, settings: {}}, r1.state, NOW);
          return [r1.notes.length, r2.notes.length, r1.state.attention, r1.state.cards.length];
        })()'''.replace('SNAP', json.dumps(snapshot())).replace('NOW', repr(NOW))
        out = run(expr=expr)
        self.assertEqual(out['value'][0], 0)
        self.assertEqual(out['value'][1], 0)
        self.assertEqual(out['value'][2], ['sess_work_2|approval'])
        self.assertEqual(out['value'][3], 3)  # needs-input + review + failing; running/done are not alerts


    def test_unusable_provider_windows_do_not_raise_quota_notifications(self):
        provider = {
            'windows': [{'label': 'Session', 'used_percent': 99, 'reset_at': iso(600)}],
            'access_state': 'unavailable',
            'api_calls_available': None,
        }
        expr = '''(() => {
          const p = PROVIDER;
          const r = monitorNotifications({snapshot: SNAP, providers: {'anthropic:work': p}, settings: {}}, {}, NOW);
          return r.notes.filter((n) => n.kind === 'quota' || n.kind === 'soon').length;
        })()'''.replace('SNAP', json.dumps(snapshot())).replace(
            'PROVIDER', json.dumps(provider)).replace('NOW', repr(NOW))
        self.assertEqual(0, run(expr=expr)['value'])

def _react_available():
    return all((ROOT / 'node_modules' / m).is_dir() for m in ('react', 'react-dom', '@babel/core', '@babel/preset-env'))


@unittest.skipUnless(_react_available(), 'npm i --no-save react react-dom @babel/core @babel/preset-react @babel/preset-env')
class MonitorRealReactTest(unittest.TestCase):
    """Real hooks + jsx runtime: every view renders without throwing or React errors."""

    def render(self, **options):
        # See run(): the trial ships the monitor OFF; enroll explicitly here so
        # the real-hook views actually mount with the snapshot under test.
        if options.pop('enroll', True):
            storage = dict(options.get('storage') or {})
            storage.setdefault('monitorEnabled', True)
            for key, value in NOTIFY_STORAGE_DEFAULTS.items():
                storage.setdefault(key, value)
            options['storage'] = storage
        p = subprocess.run(['node', str(Path(__file__).resolve().parent / 'widget_monitor_ssr.cjs')],
                           input=json.dumps(options), text=True, capture_output=True, timeout=60,
                           env={**os.environ, 'TZ': 'UTC', 'LANG': 'en_US.UTF-8'})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_all_views_render(self):
        history = {'monitor_version': 1, 'history': [{'provider': 'anthropic', 'window': 'Current session',
                                                      'points': [[NOW - 600, 90], [NOW, 96]]}], 'burn': []}
        out = self.render(data=quota_payload(), monitor=snapshot(), history=history,
                          storage={'budgetDaily': 10, 'budgetMonthly': 40})
        # Only the pre-existing "key spread into JSX" warning class is tolerated.
        bad = [e for e in out['errors'] if 'is being spread into JSX' not in e]
        self.assertEqual(bad, [])
        html = out['html']
        self.assertIn('data-sparkline', html['quota'])
        self.assertIn('Deploy preview', html['live'])
        self.assertIn('Fallback chains', html['health'])
        self.assertIn('$12.50 / $10.00', html['usage'])
        self.assertIn('Pick a logo', html['kanban'])
        self.assertIn('1 working', html['agents'])
        self.assertIn('data-cost-chip="bad"', html['cost'])
        self.assertIn('$12.50', html['cost'])
        self.assertIn('Notifications', html['settings'])
        for markup in html.values():
            self.assertNotRegex(markup, r'#[0-9a-fA-F]{3,6}\b|rgb\(')

    def test_old_backend_renders_notice_everywhere(self):
        out = self.render(data=quota_payload(), monitor={'unsupported': True}, locale='tr')
        bad = [e for e in out['errors'] if 'is being spread into JSX' not in e]
        self.assertEqual(bad, [])
        self.assertIn('./install.sh', out['html']['live'])
        self.assertEqual(out['html']['agents'], '')
        self.assertEqual(out['html']['cost'], '')


if __name__ == '__main__':
    unittest.main()
