import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';

const source = new URL('../tui/quota-panel.mjs', import.meta.url);
test('the TUI panel is shipped as a standalone widget', () => {
  assert.ok(existsSync(source), 'missing standalone quota widget');
});

test('renders remaining quota, preserves nested accounts and filters by provider', async () => {
  const { panelModel } = await import(source);
  const now = Date.parse('2026-10-22T12:00:00Z');
  const cache = { fetched_at: '2026-10-22T11:59:00Z', providers: {
    'openai-codex': { accounts: [{ label: 'Personal', windows: [
      { label: 'Session', used_percent: 25, reset_at: '2026-10-22T13:00:00Z' }
    ] }] },
    'anthropic:work': { label: 'Work', windows: [{ label: 'Weekly', used_percent: 90 }] }
  } };
  const model = panelModel(cache, ['openai-codex'], now);
  assert.equal(model.rows.length, 1);
  assert.match(model.rows[0].text, /Personal/);
  assert.match(model.rows[0].text, /75% left/);
  assert.match(model.rows[0].text, /1h/);
  assert.equal(model.stale, false);
});

test('unknown values, errors, stale data and balances are never fabricated percentages', async () => {
  const { panelModel } = await import(source);
  const model = panelModel({ providers: {
    broken: { unavailable_reason: 'expired token', windows: [{ used_percent: 0 }] },
    unknown: { windows: [{ label: 'Monthly', used_percent: null }] },
    balance: { account_balances: [{ currency: 'USD', total_balance: '3.50' }] },
    bad: null
  } });
  assert.equal(model.stale, true);
  assert.equal(model.rows.length, 3);
  assert.match(model.rows[0].text, /unavailable/);
  assert.doesNotMatch(model.rows[0].text, /100%/);
  assert.match(model.rows[1].text, /\? left/);
  assert.match(model.rows[2].text, /USD 3.50/);
  const missing = panelModel({}, ['absent']);
  assert.match(missing.rows[0].text, /absent.*no data/);
});

test('registers an opt-in ambient dock with a distinct command and validated selectors', async () => {
  const { default: register } = await import(source);
  const apps = [];
  register({ defineWidgetApp: value => { apps.push(value); return value; } });
  const app = apps.find(value => value.id === 'quota-panel');
  assert.equal(app.id, 'quota-panel');
  assert.equal(app.mode, 'ambient');
  assert.equal(app.zone, 'dock-top');
  assert.deepEqual(app.init('anthropic').selected, ['anthropic']);
  assert.equal(app.init('anthropic,openai-codex'), null);
  assert.equal(app.init('$(touch hacked)'), null);
});

test('picker excludes missing credentials and disabled rows without reading keys', async () => {
  const { pickerOptions } = await import(source);
  const rows = pickerOptions({ providers: {
    absent: { unavailable_reason: 'no-credentials' },
    off: { unavailable_reason: 'disabled' },
    optedOut: { unavailable_reason: 'opt-in-disabled' },
    expired: { label: 'Reconnect me', unavailable_reason: 'auth-failed' },
    ready: { label: 'Ready', windows: [{ used_percent: 20 }] },
    'openai-codex': { accounts: [{ id: 'work', label: 'Work', windows: [{ used_percent: 10 }] }] },
  } });
  assert.deepEqual(rows.map(row => row.id), ['expired', 'ready', 'openai-codex:work']);
  assert.equal(rows[0].unavailable, true);
});

test('picker navigates, selects one account, and cancel never opens a panel', async () => {
  const { default: register } = await import(source);
  const { mkdtemp, rm } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  // register() persists the pick, so the test must never write the real pref.
  const dir = await mkdtemp(join(tmpdir(), 'quota-widget-'));
  const previous = process.env.HERMES_HOME;
  process.env.HERMES_HOME = dir;
  try {
    const apps = [], opened = [];
    const sdk = { defineWidgetApp: app => { apps.push(app); return app; },
      openWidget: (app, state) => opened.push([app.id, state]) };
    register(sdk);
    const picker = apps.find(app => app.id === 'quota-select');
    assert.equal(picker.mode, 'modal');
    let state = { phase: 'ready', options: [{ id: 'first' }, { id: 'second' }], index: 0 };
    state = picker.reduce(state, { ch: '', key: { downArrow: true } });
    assert.equal(state.index, 1);
    assert.equal(picker.reduce(state, { ch: '', key: { return: true } }), null);
    assert.deepEqual(opened, [['quota-panel', { selected: ['second'] }]]);
    assert.deepEqual(apps.find(app => app.id === 'quota-panel').init('').selected, ['second']);
    assert.equal(picker.reduce(state, { ch: '', key: { escape: true } }), null);
    assert.equal(opened.length, 1);
    await new Promise(resolve => setTimeout(resolve, 50));
  } finally {
    if (previous === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = previous;
    await rm(dir, { recursive: true, force: true });
  }
});

test('the picker starts on the remembered provider instead of the first row', async () => {
  const { rememberedIndex, pickerOptions } = await import(source);
  const options = pickerOptions({ providers: {
    'openai-codex': { windows: [{ used_percent: 96 }] },
    grok: { windows: [{ used_percent: 84 }] },
    'anthropic:work': { windows: [{ used_percent: 50 }] },
  } });
  assert.equal(rememberedIndex(options, 'grok'), options.findIndex(row => row.id === 'grok'));
  assert.equal(rememberedIndex(options, 'anthropic'), options.findIndex(row => row.id === 'anthropic:work'),
    'a saved provider still matches its account rows');
  assert.equal(rememberedIndex(options, 'not-cached'), 0, 'an unavailable saved provider falls back to the first row');
  assert.equal(rememberedIndex(options, null), 0);
  assert.equal(rememberedIndex([], 'grok'), 0);
});

test('only a selection the cache still resolves is persisted', async () => {
  const { rememberable, pickerOptions } = await import(source);
  const options = pickerOptions({ providers: {
    'openai-codex': { windows: [{ used_percent: 1 }] },
    'anthropic:work': { windows: [{ used_percent: 50 }] },
  } });
  assert.equal(rememberable(options, ['openai-codex']), true);
  assert.equal(rememberable(options, ['anthropic']), true, 'provider-level selection matches its account row');
  assert.equal(rememberable(options, ['second']), false, 'a stale test artifact must never overwrite the preference');
  assert.equal(rememberable(options, []), false);
  assert.equal(rememberable(options, ['openai-codex', 'grok']), false);
});

test('the persisted preference is the string the picker resolves', async () => {
  const { mkdtemp, rm } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { createPrefsStore, markDockVisible, readPrefs, rememberedIndex, pickerOptions } = await import(source);
  const dir = await mkdtemp(join(tmpdir(), 'quota-widget-'));
  const previous = process.env.HERMES_HOME;
  process.env.HERMES_HOME = dir;
  try {
    const store = createPrefsStore(dir);
    await store.load();
    await markDockVisible(store, ['grok']);
    const saved = await readPrefs(dir);
    assert.equal(typeof saved.selected, 'string', 'the dock stores one selector, not an array');
    const options = pickerOptions({ providers: {
      'openai-codex': { windows: [{ used_percent: 96 }] },
      grok: { windows: [{ used_percent: 84 }] },
    } });
    assert.equal(rememberedIndex(options, saved.selected), options.findIndex(row => row.id === 'grok'));
  } finally {
    if (previous === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = previous;
    await rm(dir, { recursive: true, force: true });
  }
});

test('bar meters use remaining capacity and never fill unknown or unavailable windows', async () => {
  const { meterModel } = await import(source);
  const cache = { providers: {
    demo: { label: 'Demo', windows: [{ label: 'Weekly', used_percent: 75 }] },
    missing: { windows: [{ used_percent: null }] },
    expired: { unavailable_reason: 'auth-failed', windows: [{ used_percent: 0 }] },
  } };
  assert.equal(meterModel(cache, 'demo').meters[0].remaining, 25);
  assert.equal(meterModel(cache, 'missing').meters[0].remaining, null);
  assert.equal(meterModel(cache, 'expired').meters.length, 0);
  assert.equal(meterModel(cache, 'demo').label, 'Demo');
});

test('friendly provider names match Desktop and preserve account identity', async () => {
  const { panelModel, pickerOptions } = await import(source);
  const cache = { providers: {
    'openai-codex': { accounts: [{ id: 'personal', label: 'Personal', windows: [] }] },
    'anthropic:work': { account_label: 'Work', windows: [] },
    'opencode-go': { windows: [] },
  } };
  assert.deepEqual(panelModel(cache).rows.map(row => row.label), [
    'OpenAI Codex · Personal', 'Anthropic · Work', 'OpenCode Go',
  ]);
  assert.deepEqual(pickerOptions(cache).map(row => row.label), panelModel(cache).rows.map(row => row.label));
});

test('all public provider names stay aligned with the Desktop registry', async () => {
  const { readFile } = await import('node:fs/promises');
  const { PROVIDER_NAMES } = await import(source);
  const desktop = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
  const block = desktop.split('const PROVIDER_META = {')[1].split('\n};')[0];
  const names = Object.fromEntries([...block.matchAll(/\s*(?:"([^"]+)"|([\w]+)):\s*\{ name: "([^"]+)"/g)].map(match => [match[1] || match[2], match[3]]));
  assert.deepEqual(PROVIDER_NAMES, names);
});

test('single-line layout keeps the tightest window, reset date and overflow count', async () => {
  const { lineModel } = await import(source);
  const now = Date.parse('2026-10-22T12:00:00Z');
  const cache = { fetched_at: new Date(now).toISOString(), providers: {
    'openai-codex': { windows: [
      { label: 'Session', used_percent: 10, reset_at: '2026-10-22T13:00:00Z' },
      { label: 'Weekly', used_percent: 80, reset_at: '2026-10-27T14:30:00Z' },
    ] },
  } };
  const line = lineModel(cache, 'openai-codex', 100, now);
  assert.match(line.text, /OpenAI Codex/);
  assert.match(line.text, /Weekly/);
  assert.match(line.text, /20% left/);
  assert.match(line.text, /27\/10/);
  assert.doesNotMatch(line.text, /5d/);
  assert.doesNotMatch(line.text, /\n/);
  const small = lineModel(cache, 'openai-codex', 60, now);
  assert.ok([...small.text].length <= 60);
  assert.match(small.text, /\+1/);
  assert.match(lineModel(cache, 'openai-codex', 40, now).text, /\+1$/);
});

test('minimal quota line is explicit and omits routine cache noise', async () => {
  const { lineModel } = await import(source);
  const now = Date.parse('2026-10-22T12:00:00Z');
  const model = lineModel({ fetched_at: new Date(now).toISOString(), providers: {
    demo: { label: 'Demo', windows: [{ label: 'Weekly', used_percent: 60, reset_at: '2026-10-27T14:30:00Z' }] },
  } }, 'demo', 100, now);
  assert.match(model.text, /^Quota · Demo/);
  assert.doesNotMatch(model.text, /cache|5d/);
  assert.match(model.text, /27\/10/);
});

test('minimal quota renderer right-aligns and uses theme tokens by role', async () => {
  const { renderLine } = await import(source);
  const sdk = { Box: 'Box', Text: 'Text', h: (type, props, ...children) => ({ type, props, children }) };
  const t = { color: { primary: 'THEME_PRIMARY', muted: 'THEME_MUTED', ok: 'THEME_OK', warn: 'THEME_WARN', error: 'THEME_ERROR' } };
  const tree = renderLine(sdk, { text: 'Quota · Demo · Weekly ██░░ 40% left · reset 27/10 14:30', remaining: 40 }, t, 100);
  assert.equal(tree.props.justifyContent, 'flex-end');
  assert.equal(tree.props.height, 1);
  const nodes = tree.children[0].children;
  assert.ok(nodes.some(node => node.props?.color === 'THEME_PRIMARY' && node.children[0] === 'Quota'));
  assert.ok(nodes.some(node => node.props?.color === 'THEME_OK' && node.children[0].includes('██')));
  assert.ok(nodes.some(node => node.props?.color === 'THEME_MUTED'));
  const stale = renderLine(sdk, { text: 'Quota · STALE · Demo ██░░ 40% left', remaining: 40, stale: true }, t, 100);
  assert.ok(!stale.children[0].children.some(node => node.props?.color === 'THEME_OK'));
});

test('poller reads cache before refresh, is single-flight and stops on close', async () => {
  const { createPoller } = await import(source);
  let reads = 0, refreshes = 0, updates = 0, finish;
  const poller = createPoller({
    read: async () => { reads++; return {}; },
    refresh: async () => { refreshes++; await new Promise(resolve => { finish = resolve; }); },
    onUpdate: () => { updates++; },
    now: () => 100000,
  });
  await poller.tick();
  assert.equal(reads, 1);
  assert.equal(refreshes, 1);
  assert.equal(updates, 2);
  await poller.tick();
  assert.equal(refreshes, 1);
  poller.stop();
  const before = updates;
  finish();
  await new Promise(resolve => setImmediate(resolve));
  await poller.tick();
  assert.equal(updates, before);
  assert.equal(reads, 2);
});

test('refresh failure is visible and retries are throttled; fresh caches never refresh', async () => {
  const { createPoller } = await import(source);
  let now = Date.parse('2026-10-22T12:00:00Z'), attempts = 0, latest;
  const poller = createPoller({ read: async () => ({}),
    refresh: async () => { attempts++; throw Error('network down'); },
    now: () => now, onUpdate: state => { latest = state; } });
  await poller.tick();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(latest.error, /Refresh failed/);
  await poller.tick();
  assert.equal(attempts, 1);
  now += 60000;
  await poller.tick();
  assert.equal(attempts, 2);
  poller.stop();
  const fresh = createPoller({ read: async () => ({ fetched_at: new Date(now).toISOString(), providers: {} }),
    refresh: async () => { throw Error('must not refresh'); }, now: () => now, onUpdate: () => {} });
  await fresh.tick();
  fresh.stop();
});

const SPECIAL_SOURCE = 'https://x.com/thsottiaux/status/2113162413008884099';
const specialCache = (announcement, extra = {}) => ({
  fetched_at: '2026-10-22T12:00:00Z',
  providers: { 'openai-codex': { accounts: [{ id: 'work', label: 'Personal', windows: [
    { label: 'Session', used_percent: 20, reset_at: '2026-10-22T13:00:00Z' },
    { label: 'Weekly', used_percent: 80, reset_at: '2026-10-27T14:30:00Z' },
  ] }] } },
  codex_special_reset: {
    announcement, fetched_at: '2026-10-22T12:00:00Z', stale: false, unavailable_reason: null, ...extra,
  },
});

test('special reset announcement is a validated provider-level model, not a forecast', async () => {
  const { specialResetModel } = await import(source);
  const base = { id: 'reset-1', status: 'scheduled', reset_type: 'banked',
    announced_at: '2026-10-22T06:55:54Z', source_url: SPECIAL_SOURCE };
  const model = specialResetModel(specialCache(base));
  assert.equal(model.status, 'scheduled');
  assert.equal(model.resetType, 'banked');
  assert.match(model.date, /^22\/10/);
  assert.equal(model.sourceUrl, SPECIAL_SOURCE);
  assert.equal(model.stale, false);

  const recorded = specialResetModel(specialCache({ ...base, status: 'recorded' }));
  assert.equal(recorded, null);

  for (const bad of [
    { ...base, status: 'executing' },
    { ...base, reset_type: 'unknown' },
    { ...base, announced_at: '2026-10-22T06:55:54' },
    { ...base, announced_at: 'not-a-dateZ' },
    { ...base, id: '' },
    { ...base, source_url: 'https://x.com/attacker/status/1' },
    { ...base, source_url: 'http://x.com/thsottiaux/status/2113162413008884099' },
  ]) {
    assert.equal(specialResetModel(specialCache(bad)), null, JSON.stringify(bad));
  }

  assert.equal(specialResetModel({ providers: {} }), null);
  assert.equal(specialResetModel({ codex_special_reset: { announcement: null } }), null);
  const stale = specialResetModel(specialCache(base, { stale: true }));
  assert.equal(stale.stale, true);
  // Missing/malformed freshness metadata is qualified as stale, not fresh.
  const unknown = specialCache(base);
  delete unknown.codex_special_reset.stale;
  assert.equal(specialResetModel(unknown).stale, true);
  unknown.codex_special_reset.stale = false;
  unknown.codex_special_reset.fetched_at = 'not-a-date';
  assert.equal(specialResetModel(unknown).stale, true);
  // Old announcements keep their year so they cannot look recent.
  const old = specialResetModel(specialCache({ ...base, announced_at: '2024-03-02T06:55:54Z' }),
    Date.parse('2026-10-22T12:00:00Z'));
  assert.match(old.date, /^02\/03\/2024 \d{2}:\d{2}$/);
});

test('special reset formats in the cache timezone, not the renderer zone', async () => {
  const { specialResetModel } = await import(source);
  const cache = specialCache({ id: 'r1', status: 'scheduled', reset_type: 'regular',
    announced_at: '2026-10-22T06:55:54Z', source_url: SPECIAL_SOURCE });
  const now = Date.parse('2026-10-22T12:00:00Z');
  cache.timezone = 'Asia/Tokyo';
  assert.equal(specialResetModel(cache, now).date, '22/10 15:55');
  cache.timezone = 'America/Los_Angeles';
  assert.equal(specialResetModel(cache, now).date, '21/10 23:55');
  cache.timezone = 'Not/AZone';
  assert.ok(specialResetModel(cache, now)); // unknown zone falls back, never hides
});

test('special reset shows the scheduled estimate instead of the post time', async () => {
  const { specialResetModel, specialNoticeText } = await import(source);
  const cache = specialCache({ id: 'r1', status: 'scheduled', reset_type: 'regular',
    announced_at: '2026-10-22T06:55:54Z', scheduled_for: '2026-10-22T17:00:00Z',
    source_url: SPECIAL_SOURCE });
  cache.timezone = 'Europe/Lisbon';
  const now = Date.parse('2026-10-22T12:00:00Z');
  assert.equal(specialResetModel(cache, now).date, '22/10 18:00');
  assert.match(specialNoticeText(cache, now), /22\/10 18:00/);
  // A malformed estimate falls back to the publication time, never hides.
  cache.codex_special_reset.announcement.scheduled_for = 'not-a-time';
  assert.equal(specialResetModel(cache, now).date, '22/10 07:55');
});

test('special reset line keeps quota at 80 cols and shows announced date, state and credit', async () => {
  const { lineModel } = await import(source);
  const now = Date.parse('2026-10-22T12:00:00Z');
  for (const cols of [80, 100, 120]) {
    const line = lineModel(specialCache({ id: 'r1', status: 'scheduled', reset_type: 'regular',
      announced_at: '2026-10-22T06:55:54Z', source_url: SPECIAL_SOURCE }), 'openai-codex', cols, now);
    assert.ok([...line.text].length <= cols, `${cols}: ${line.text}`);
    assert.match(line.text, /\d{2}\/10 \d{2}:\d{2}/, `${cols}: ${line.text}`);
    assert.match(line.text, /scheduled/, `${cols}: ${line.text}`);
    assert.doesNotMatch(line.text, /scheduled_for|17:00|in \d|countdown/i);
    assert.match(line.text, /codex-resets\.com/, `${cols}: ${line.text}`);
    assert.match(line.text, /20%|80%/, `${cols}: ${line.text}`);
    assert.match(line.text, /Codex/, `${cols}: ${line.text}`);
    assert.doesNotMatch(line.text, /[\x00-\x1f\x7f]/);
    assert.doesNotMatch(line.text, /\n/);
  }
  const wide = lineModel(specialCache({ id: 'r1', status: 'recorded', reset_type: 'banked',
    announced_at: '2026-10-20T06:33:48Z', source_url: SPECIAL_SOURCE }), 'openai-codex', 120, now).text;
  assert.doesNotMatch(wide, /Banked|recorded|✨/);
  // Account renewal stays visible when the historical notice is hidden.
  assert.match(wide, /reset 27\/10/);
});

test('special reset line degrades safely at 40 cols and is absent outside Codex', async () => {
  const { lineModel, specialNoticeText } = await import(source);
  const now = Date.parse('2026-10-22T12:00:00Z');
  const cache = specialCache({ id: 'r1', status: 'scheduled', reset_type: 'regular',
    announced_at: '2026-10-22T06:55:54Z', source_url: SPECIAL_SOURCE });
  const line = lineModel(cache, 'openai-codex', 40, now);
  assert.ok([...line.text].length <= 40, line.text);
  // At 40 columns mandatory quota identity/state wins; attribution lives in
  // the picker instead of being truncated into a misleading fragment.
  assert.match(line.text, /Quota/);
  assert.match(line.text, /20%|80%/);
  assert.doesNotMatch(line.text, /codex-resets\.com/);
  assert.doesNotMatch(line.text, /\n/);
  assert.match(specialNoticeText(cache, now), /✨ Special reset 22\/10 \d{2}:\d{2} · scheduled · codex-resets\.com/);
  const narrow = specialNoticeText(cache, now, 40);
  assert.match(narrow, /✨ 22\/10 \d{2}:\d{2} · codex-resets\.com/);
  assert.ok([...narrow].length <= 40);

  const other = lineModel(cache, 'openai-codex:work', 120, now);
  assert.match(other.text, /\d{2}\/10 \d{2}:\d{2}/); // selected Codex account keeps the provider notice
  assert.match(other.text, /Personal/);
  const anthropic = lineModel({ ...cache,
    providers: { anthropic: { windows: [{ label: 'Weekly', used_percent: 10 }] } } }, 'anthropic', 120, now);
  assert.doesNotMatch(anthropic.text, /announced|codex-resets|Special/i);
});

test('special reset never hides unavailable, stale or refresh-error quota state', async () => {
  const { lineModel } = await import(source);
  const now = Date.parse('2026-10-22T12:00:00Z');
  const base = specialCache({ id: 'r1', status: 'scheduled', reset_type: 'regular',
    announced_at: '2026-10-22T06:55:54Z', source_url: SPECIAL_SOURCE });
  const unavailable = JSON.parse(JSON.stringify(base));
  unavailable.providers['openai-codex'].accounts[0].unavailable_reason = 'auth-failed';
  unavailable.codex_special_reset.stale = true;
  const row = lineModel(unavailable, 'openai-codex', 120, now, { error: true });
  assert.match(row.text, /unavailable/);
  assert.doesNotMatch(row.text, /\?%/);
  assert.match(row.text, /STALE/);
  assert.match(row.text, /refresh error/);
  assert.match(row.text, /\d{2}\/10 \d{2}:\d{2}/);
  const stale = lineModel(base, 'openai-codex', 80, now);
  assert.doesNotMatch(stale.text, /\?%/);
});

test('special reset hides malformed root data, marks stale, and barely styles as a warning', async () => {
  const { lineModel, renderLine } = await import(source);
  const now = Date.parse('2026-10-22T12:00:00Z');
  const good = specialCache({ id: 'r1', status: 'scheduled', reset_type: 'regular',
    announced_at: '2026-10-22T06:55:54Z', source_url: SPECIAL_SOURCE }, { stale: true });
  const stale = lineModel(good, 'openai-codex', 120, now);
  assert.match(stale.text, /STALE/);
  assert.match(stale.text, /\d{2}\/10 \d{2}:\d{2}/);
  for (const bad of [
    { announcement: { id: 'x', status: 'nope', reset_type: 'regular', announced_at: '2026-10-22T06:55:54Z' } },
    { announcement: 'text' },
    null,
    { announcement: { id: 'x', status: 'scheduled', reset_type: 'regular', announced_at: '22/10/2026' } },
  ]) {
    const cache = specialCache(null);
    cache.codex_special_reset = bad;
    assert.doesNotMatch(lineModel(cache, 'openai-codex', 120, now).text, /codex-resets|announced/);
  }

  const sdk = { Box: 'Box', Text: 'Text', h: (type, props, ...children) => ({ type, props, children }) };
  const t = { color: { primary: 'P', muted: 'M', ok: 'OK', warn: 'W', error: 'E' } };
  const tree = renderLine(sdk, stale, t, 100);
  assert.equal(tree.props.height, 1);
  assert.equal(tree.props.justifyContent, 'flex-end');
  const colors = tree.children[0].children.map(node => node.props?.color);
  assert.ok(!colors.includes('E'), 'announcement must not be alarming red');
  assert.ok(colors.includes('M'));
});

test('real cache reader respects HERMES_HOME and rejects malformed cache', async () => {
  const { mkdtemp, writeFile, rm } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { readCache } = await import(source);
  const dir = await mkdtemp(join(tmpdir(), 'quota-widget-'));
  const previous = process.env.HERMES_HOME;
  process.env.HERMES_HOME = dir;
  try {
    await assert.rejects(readCache());
    await writeFile(join(dir, 'quota_cache.json'), '{broken');
    await assert.rejects(readCache());
    await writeFile(join(dir, 'quota_cache.json'), JSON.stringify({ providers: {} }));
    assert.deepEqual(await readCache(), { providers: {} });
  } finally {
    if (previous === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = previous;
    await rm(dir, { recursive: true, force: true });
  }
});

test('the display preference is owner-only, atomic and never trusts junk', async () => {
  const { mkdtemp, readFile, stat, writeFile, rm } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { normalizePrefs, prefsPath, readPrefs, writePrefs } = await import(source);
  const dir = await mkdtemp(join(tmpdir(), 'quota-prefs-'));
  try {
    assert.deepEqual(await readPrefs(dir), { selected: null, autoOpen: true });
    assert.deepEqual(await writePrefs({ selected: 'anthropic:work', autoOpen: true }, dir),
      { selected: 'anthropic:work', autoOpen: true });
    assert.equal((await stat(prefsPath(dir))).mode & 0o777, 0o600);
    assert.deepEqual(JSON.parse(await readFile(prefsPath(dir), 'utf8')),
      { version: 1, selected: 'anthropic:work', autoOpen: true });
    assert.deepEqual(await readPrefs(dir), { selected: 'anthropic:work', autoOpen: true });
    await writeFile(prefsPath(dir), '{"selected":"anthropic:work","autoOpen":false}');
    assert.deepEqual(await readPrefs(dir), { selected: 'anthropic:work', autoOpen: false });
    await writeFile(prefsPath(dir), 'not json at all');
    assert.deepEqual(await readPrefs(dir), { selected: null, autoOpen: true });
    assert.deepEqual(normalizePrefs({ selected: '$(touch hacked)', autoOpen: 'true' }), { selected: null, autoOpen: true });
    assert.deepEqual(normalizePrefs({ selected: ' grok ', autoOpen: false }), { selected: 'grok', autoOpen: false });
    assert.deepEqual(normalizePrefs(['grok']), { selected: null, autoOpen: true });
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('the preference never lands in the watched widget directory and rewrites nothing', async () => {
  const { mkdtemp, rm, stat } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { prefsPath, writePrefs } = await import(source);
  const dir = await mkdtemp(join(tmpdir(), 'quota-watch-'));
  try {
    // Writing inside tui-widgets/ would hot-reload the widget, remount the dock
    // and write again — the reload/write loop seen in a live TUI.
    assert.ok(!prefsPath(dir).startsWith(join(dir, 'tui-widgets')), 'preference must stay outside the watched directory');
    await writePrefs({ selected: 'grok' }, dir);
    const first = (await stat(prefsPath(dir))).mtimeMs;
    await new Promise(resolve => setTimeout(resolve, 20));
    await writePrefs({ selected: 'grok' }, dir);
    assert.equal((await stat(prefsPath(dir))).mtimeMs, first, 'an unchanged preference must not be rewritten');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('the saved dock auto-opens once per TUI process and honours auto-open', async () => {
  const { mkdtemp, rm } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { restoreSavedPanel, writePrefs } = await import(source);
  const dir = await mkdtemp(join(tmpdir(), 'quota-restore-'));
  const opened = [];
  const sdk = { openWidget: (app, state) => opened.push({ app, state }) };
  const panel = { id: 'quota-panel' };
  delete globalThis.__hermesQuotaPanel;
  try {
    assert.equal(await restoreSavedPanel(sdk, panel, { home: dir }), null);
    await writePrefs({ selected: 'anthropic:account2', autoOpen: true }, dir);
    assert.equal(await restoreSavedPanel(sdk, panel, { home: dir }), 'anthropic:account2');
    assert.deepEqual(opened, [{ app: panel, state: { selected: ['anthropic:account2'] } }]);
    assert.equal(await restoreSavedPanel(sdk, panel, { home: dir }), null, 'a /widgets-reload must not reopen it');
    await writePrefs({ selected: 'grok', autoOpen: false }, dir);
    assert.equal(await restoreSavedPanel(sdk, panel, { home: dir, force: true }), null, 'off stays off');
    await writePrefs({ selected: 'grok', autoOpen: true }, dir);
    assert.equal(await restoreSavedPanel(sdk, panel, { home: dir, force: true }), 'grok');
    assert.equal(opened.length, 2);
    assert.equal(await restoreSavedPanel({}, panel, { home: dir, force: true }), null, 'no host hook, no crash');
  } finally {
    delete globalThis.__hermesQuotaPanel;
    await rm(dir, { recursive: true, force: true });
  }
});

test('showing the dock persists it; off and on drive only the startup preference', async () => {
  const { mkdtemp, rm } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { createPrefsStore, markDockVisible, readPrefs, writePrefs, default: register } = await import(source);
  const dir = await mkdtemp(join(tmpdir(), 'quota-toggle-'));
  const previous = process.env.HERMES_HOME;
  delete globalThis.__hermesQuotaPanel;
  try {
    const store = createPrefsStore(dir);
    await markDockVisible(store, ['anthropic:work']);
    await store.save({ autoOpen: false });
    // Serialized writes: the last intent wins, never an older one landing last.
    void store.save({ autoOpen: true });
    await store.idle();
    assert.deepEqual(await readPrefs(dir), { selected: 'anthropic:work', autoOpen: true });

    process.env.HERMES_HOME = dir;
    await writePrefs({ selected: 'anthropic:work', autoOpen: true }, dir);
    const apps = [], opened = [];
    register({ defineWidgetApp: value => { apps.push(value); return value; },
      openWidget: (app, state) => opened.push(state) });
    await new Promise(resolve => setTimeout(resolve, 25));
    assert.deepEqual(opened, [{ selected: ['anthropic:work'] }], 'a new TUI session restores the dock');
    const app = apps.find(value => value.id === 'quota-panel');
    assert.equal(app.init('off'), null, 'off closes without a dock state');
    await new Promise(resolve => setTimeout(resolve, 25));
    assert.deepEqual(await readPrefs(dir), { selected: 'anthropic:work', autoOpen: false });
    assert.deepEqual(app.init('on'), { selected: ['anthropic:work'] }, 'on reopens the remembered account');
    assert.deepEqual(app.init('anthropic:work').selected, ['anthropic:work']);
    assert.equal(app.init('anthropic grok'), null, 'still one selector at a time');
  } finally {
    if (previous === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = previous;
    delete globalThis.__hermesQuotaPanel;
    await rm(dir, { recursive: true, force: true });
  }
});
