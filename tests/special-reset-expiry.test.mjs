import test from 'node:test';
import assert from 'node:assert/strict';
import { specialResetModel, lineModel, specialNoticeText, pickerOptions, rememberedIndex, panelModel } from '../tui/quota-panel.mjs';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const now = Date.parse('2026-11-03T10:00:00Z');
const cache = (status, scheduled) => ({
  providers: { 'openai-codex': { windows: [{ label: 'Weekly', used_percent: 20 }] } },
  codex_special_reset: {
    announcement: { id: 'expiry', status, reset_type: 'regular',
      announced_at: '2026-11-01T00:00:00Z', scheduled_for: scheduled,
      source_url: 'https://x.com/thsottiaux/status/123' },
    fetched_at: '2026-11-03T09:00:00Z', stale: false,
  },
});
test('special notice expires at the scheduled instant even without a new fetch', () => {
  for (const scheduled of ['2026-11-03T09:59:59Z', '2026-11-03T10:00:00Z', '2026-11-03T11:00:00+01:00', '2026-11-03T10:00:00']) {
    const data = cache('scheduled', scheduled);
    assert.equal(specialResetModel(data, now), null);
    assert.equal(specialNoticeText(data, now), '');
    assert.doesNotMatch(lineModel(data, 'openai-codex', 120, now).text, /✨|codex-resets/);
  }
  const future = cache('scheduled', '2026-11-03T10:00:01Z');
  assert.ok(specialResetModel(future, now));
  assert.equal(specialResetModel(future, now + 1000), null);
  assert.equal(specialResetModel(cache('recorded', null), now), null);
  assert.ok(specialResetModel(cache('scheduled', null), now));
  assert.match(specialNoticeText(cache('scheduled', null), now), /scheduled/i);
});

test('an open picker expires cached notices and clears its timer on unmount', async () => {
  // Exercise the real component body with a minimal hook host, no user files.
  const source = await readFile(new URL('../tui/quota-panel.mjs', import.meta.url), 'utf8');
  const component = source.slice(source.indexOf('  function Picker('), source.indexOf('\n  panel = defineWidgetApp'));
  const slots = [];
  let cursor = 0, mounted = false, effect, cleanup, tick, cleared = false, clock = now;
  let state = { phase: 'loading', options: [], index: 0 };
  const React = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = initial;
      return [slots[i], value => { slots[i] = value; }];
    },
    useEffect(fn) { if (!mounted) effect = fn; },
  };
  const Picker = vm.runInNewContext(`(${component})`, {
    React, Date: { now: () => clock },
    setInterval: fn => { tick = fn; return 42; },
    clearInterval: id => { assert.equal(id, 42); cleared = true; },
    readCache: async () => cache('scheduled', '2026-11-03T10:00:01Z'),
    prefs: { load: async () => ({}) }, chosen: null, picker: {},
    sdk: { updateWidget: (_app, update) => { state = update(state); } },
    pickerOptions, rememberedIndex, panelModel, specialNoticeText,
    h: (type, props, ...children) => ({ type, props, children }),
    Overlay: 'Overlay', Dialog: 'Dialog', Box: 'Box', Text: 'Text',
  });
  const render = () => {
    cursor = 0;
    return JSON.stringify(Picker({ state, t: { color: {} }, cols: 120, rows: 40 }));
  };
  render(); cleanup = effect(); mounted = true;
  await new Promise(resolve => setImmediate(resolve));
  assert.match(render(), /✨/);
  clock += 15000; tick();
  assert.doesNotMatch(render(), /✨|codex-resets/);
  assert.equal(state.phase, 'ready');
  cleanup();
  assert.equal(cleared, true);
});
