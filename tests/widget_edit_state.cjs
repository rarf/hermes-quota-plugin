// Real-React regression for pane-local edit mode.
//
// The stateful layout harness renders QuotaPane directly and simulates one
// mount, so it cannot model unmount/remount or two panes mounted at once. This
// harness bundles the real desktop/plugin.js against the host's React and
// jsdom, then drives real toggles through the DOM:
//
//   - a freshly mounted pane starts in the quiet card view, even when another
//     pane is editing (edit mode must not leak across surfaces);
//   - entering/leaving edit mode in one pane leaves the other untouched;
//   - remounting a pane clears any previous edit session;
//   - emptying the payload clears edit mode, and a later payload must not
//     resume it while the toggle was hidden.
//
//   QUOTA_DESKTOP_DIR=/path/to/apps/desktop node tests/widget_edit_state.cjs
//
// Synthetic data only; no network, credentials, or host build CSS.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');

const ROOT = path.resolve(__dirname, '..');
const HOST = process.env.QUOTA_DESKTOP_DIR
  || path.join(os.homedir(), '.hermes', 'hermes-agent', 'apps', 'desktop');
const OUT = fs.mkdtempSync(path.join(os.tmpdir(), 'quota-edit-state-'));
const req = createRequire(path.join(HOST, 'package.json'));
const { build } = req('esbuild');
const { JSDOM } = req('jsdom');

const provider = (used) => ({
  plan: 'Pro',
  windows: [
    { label: 'Session', used_percent: used, reset_at: '2099-01-01T00:00:00Z' },
    { label: 'Weekly', used_percent: Math.min(99, used + 12), reset_at: '2099-01-02T00:00:00Z' },
  ],
  details: ['Synthetic detail line for edit-state only'],
});
const fixture = {
  fetched_at: '2026-01-01T00:00:00Z',
  age_s: 12,
  providers: {
    anthropic: provider(24),
    deepseek: provider(38),
    openai: provider(52),
    gemini: provider(61),
  },
};
const emptyFixture = {
  fetched_at: '2026-01-01T00:00:00Z',
  age_s: 0,
  providers: {},
};

// Store-backed atom, matching the host SDK: QuotaPane subscribes through
// useValue, and layout writes must land in the same storage map the pane reads.
const sdk = `
import React from 'react';
export const atom = (value) => { const subs = new Set(); return { get: () => value, set: (next) => { if (Object.is(next, value)) return; value = next; for (const fn of [...subs]) fn(); }, subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); } }; };
export const useValue = (a) => React.useSyncExternalStore(a.subscribe, a.get, a.get);
export const useQuery = () => ({ data: window.__quotaFixture, isError: false, isPlaceholderData: false, isLoading: false });
export const useQueryClient = () => ({ invalidateQueries: () => {}, setQueryData: () => {} });
export const queryClient = { getQueriesData: () => [], getQueryCache: () => ({ subscribe: () => () => {} }) };
export const useMutation = () => ({});
export const usePluginI18n = () => window.__t;
export const useI18n = () => ({ locale: 'en' });
export const cn = (...args) => args.filter(Boolean).join(' ');
export const fmtDayTime = new Intl.DateTimeFormat('en-US');
export const host = { state: { focusedSessionOwner: atom(null) }, profileRoutes: async () => [], requestProfile: async () => ({ code: 0, output: '{}' }), notify: () => {} };
export const PANES_AREA = '', ROUTES_AREA = '', SIDEBAR_NAV_AREA = '', STATUSBAR_AREAS = { right: '' };
export const StatusDot = (props) => React.createElement('span', { 'data-tone': props.tone });
export const Input = 'input'; export const Switch = 'input'; export const SegmentedControl = 'span';
export const Tip = (props) => props.children;
export const Popover = (props) => { const kids = [].concat(props.children).filter(Boolean); return kids.length === 1 ? kids[0] : React.createElement('span', null, kids); };
export const PopoverTrigger = (props) => props.children; export const PopoverContent = (props) => React.createElement('div', props);
export const ConfirmDialog = () => null; export const icons = { RefreshCw: (props) => React.createElement('span', props, '↻') };
`;

const entry = `
import React from 'react';
import { createRoot } from 'react-dom/client';
import plugin, { QuotaPane } from 'quota-under-test';
const storage = new Map();
window.__store = storage;
window.__t = (key) => key;
const ctx = {
  storage: {
    get: (key) => storage.get(key),
    set: (key, value) => storage.set(key, value),
    remove: (key) => storage.delete(key),
  },
  i18n: { register: () => {} },
  os: { notify: () => {}, writeClipboard: async () => true },
  register: () => () => {},
};
plugin.register(ctx);
window.__roots = new Map();
window.__mountPane = (id) => {
  const host = document.createElement('div');
  host.id = id;
  document.body.appendChild(host);
  const root = createRoot(host);
  window.__roots.set(id, root);
  root.render(React.createElement(QuotaPane, {}));
  return true;
};
window.__unmountPane = (id) => {
  const root = window.__roots.get(id);
  if (!root) return false;
  window.__roots.delete(id);
  root.unmount();
  const host = document.getElementById(id);
  if (host) host.remove();
  return true;
};
window.__rerenderPane = (id) => {
  const root = window.__roots.get(id);
  if (!root) return false;
  root.render(React.createElement(QuotaPane, {}));
  return true;
};
window.__setFixture = (value) => { window.__quotaFixture = value; };
`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const checks = [];
const check = (name, ok, detail) => {
  checks.push({ name, ok: Boolean(ok), detail: detail === undefined ? null : detail });
};

async function waitFor(fn, label, timeout = 2000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      if (fn()) return true;
    } catch {
      /* retry while React paints */
    }
    await sleep(25);
  }
  throw new Error(`timed out waiting for ${label}`);
}

(async () => {
  await build({
    stdin: { contents: entry, resolveDir: HOST, loader: 'jsx' },
    bundle: true,
    format: 'iife',
    outfile: path.join(OUT, 'bundle.js'),
    jsx: 'automatic',
    tsconfig: path.join(HOST, 'tsconfig.json'),
    nodePaths: [path.join(HOST, 'node_modules'), path.join(HOST, '..', '..', 'node_modules')],
    plugins: [{
      name: 'edit-state-boundaries',
      setup(b) {
        b.onResolve({ filter: /^quota-under-test$/ }, () => ({ path: 'quota', namespace: 'quota' }));
        b.onLoad({ filter: /.*/, namespace: 'quota' }, () => ({
          contents: fs.readFileSync(path.join(ROOT, 'desktop/plugin.js'), 'utf8')
            + '\nexport { QuotaPane };',
          resolveDir: path.join(ROOT, 'desktop'),
          loader: 'js',
        }));
        b.onResolve({ filter: /^@hermes\/plugin-sdk$/ }, () => ({ path: 'sdk', namespace: 'fixture' }));
        b.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({ contents: sdk, resolveDir: HOST, loader: 'js' }));
      },
    }],
  });

  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://quota.test/',
  });
  const { window } = dom;
  const doc = window.document;
  window.__quotaFixture = fixture;
  window.eval(fs.readFileSync(path.join(OUT, 'bundle.js'), 'utf8'));

  const pane = (id) => doc.getElementById(id);
  const shells = (id) => pane(id).querySelectorAll('[data-card-shell]').length;
  const toggle = (id) => pane(id).querySelector('[data-card-edit]');
  const pressed = (id) => {
    const node = toggle(id);
    return node ? node.getAttribute('aria-pressed') : null;
  };
  const clickEdit = async (id) => {
    const node = toggle(id);
    if (!node) throw new Error(`no edit toggle in ${id}`);
    node.click();
    await sleep(60);
  };

  // A fresh pane is quiet.
  window.__mountPane('paneA');
  await waitFor(() => toggle('paneA'), 'paneA edit toggle');
  check('fresh-pane-quiet', shells('paneA') === 0 && pressed('paneA') === 'false', {
    shells: shells('paneA'), pressed: pressed('paneA'),
  });

  await clickEdit('paneA');
  await waitFor(() => shells('paneA') > 0, 'paneA edit shells');
  check('paneA-enters-edit', shells('paneA') > 0 && pressed('paneA') === 'true', {
    shells: shells('paneA'), pressed: pressed('paneA'),
  });

  // A second mounted surface must not inherit the first one's edit session.
  window.__mountPane('paneB');
  await waitFor(() => toggle('paneB'), 'paneB edit toggle');
  await sleep(80);
  check('second-pane-starts-quiet',
    shells('paneA') > 0 && pressed('paneA') === 'true'
      && shells('paneB') === 0 && pressed('paneB') === 'false', {
    paneA: { shells: shells('paneA'), pressed: pressed('paneA') },
    paneB: { shells: shells('paneB'), pressed: pressed('paneB') },
  });

  // Entering edit in B leaves A's session alone. (A leaked shared atom would
  // already have B editing here; skip the click so the failed check above
  // describes the bug instead of a timeout.)
  if (pressed('paneB') === 'false') {
    await clickEdit('paneB');
    await waitFor(() => shells('paneB') > 0, 'paneB edit shells');
  }
  check('panes-edit-independently',
    shells('paneA') > 0 && pressed('paneA') === 'true'
      && shells('paneB') > 0 && pressed('paneB') === 'true',
    { paneA: { shells: shells('paneA'), pressed: pressed('paneA') },
      paneB: { shells: shells('paneB'), pressed: pressed('paneB') } });

  // Leaving edit in A must not close B's controls.
  await clickEdit('paneA');
  await waitFor(() => shells('paneA') === 0, 'paneA quiet');
  check('pane-exit-is-local',
    pressed('paneA') === 'false' && shells('paneA') === 0
      && shells('paneB') > 0 && pressed('paneB') === 'true',
    { paneA: { shells: shells('paneA'), pressed: pressed('paneA') },
      paneB: { shells: shells('paneB'), pressed: pressed('paneB') } });

  // Remounting while B is editing must start clean, not resume the old pane's
  // session (the old module atom made every future mount edit-mode). Make sure
  // B is actually editing first, even if an earlier check failed.
  if (pressed('paneB') !== 'true') {
    await clickEdit('paneB');
    await waitFor(() => shells('paneB') > 0, 'paneB edit shells before remount');
  }
  window.__unmountPane('paneB');
  window.__mountPane('paneC');
  await waitFor(() => toggle('paneC'), 'paneC edit toggle');
  await sleep(80);
  check('remount-starts-quiet', shells('paneC') === 0 && pressed('paneC') === 'false', {
    shells: shells('paneC'), pressed: pressed('paneC'),
  });

  // An empty payload hides the toggle; the hidden session must not survive it.
  if (pressed('paneC') === 'false') {
    await clickEdit('paneC');
    await waitFor(() => shells('paneC') > 0, 'paneC edit shells');
  }
  window.__setFixture(emptyFixture);
  window.__rerenderPane('paneC');
  await waitFor(() => !toggle('paneC'), 'paneC empty payload');
  check('empty-payload-shows-no-controls',
    shells('paneC') === 0 && !toggle('paneC') && !pane('paneC').querySelector('[data-card-control]'),
    { shells: shells('paneC'), toggle: Boolean(toggle('paneC')) });

  window.__setFixture(fixture);
  window.__rerenderPane('paneC');
  await waitFor(() => toggle('paneC'), 'paneC restored toggle');
  await sleep(80);
  check('empty-payload-does-not-resume-edit',
    pressed('paneC') === 'false' && shells('paneC') === 0,
    { shells: shells('paneC'), pressed: pressed('paneC') });

  // A pane mounted on empty data has no toggle and no edit shells at all.
  window.__setFixture(emptyFixture);
  window.__mountPane('paneD');
  await waitFor(() => pane('paneD'), 'paneD mount');
  await sleep(80);
  check('empty-mount-has-no-edit-affordance',
    !toggle('paneD') && shells('paneD') === 0,
    { shells: shells('paneD'), toggle: Boolean(toggle('paneD')) });

  check('edit-mode-never-writes-storage', window.__store.size === 0,
    { storage: Object.fromEntries(window.__store) });

  const failed = checks.filter((entry) => !entry.ok);
  console.log(JSON.stringify({ checks, failed: failed.map((entry) => entry.name) }, null, 2));
  await dom.window.close();
  fs.rmSync(OUT, { recursive: true, force: true });
  if (failed.length) process.exit(1);
})().catch((error) => {
  console.error(error.stack || String(error));
  fs.rmSync(OUT, { recursive: true, force: true });
  process.exit(1);
});
