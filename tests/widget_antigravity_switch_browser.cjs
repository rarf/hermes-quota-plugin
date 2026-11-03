// Real-Chromium regression for the Antigravity backend switch across mounted
// surfaces and profiles.
//
// The desktop plugin mounts the same settings control twice (route page and
// docked pane) over one focused profile. Two real defects are pinned here:
//
//   1. The write lock was component-local `useState`, so with a write in flight
//      the other surface still accepted a click and sent the opposite
//      `quota switch ... on|off` while the first one was unanswered: the two
//      writes raced and the backend kept whichever landed last.
//   2. The optimistic value was one module-global boolean, so flipping the
//      switch in one profile repainted (and filtered) another profile's cards
//      after a profile switch.
//
// Both surfaces are mounted in one page against a stubbed SDK whose
// `requestProfile` holds `quota switch` calls open until the test resolves
// them, so "in flight" is real, not a race the test hopes to win.
//
//   QUOTA_DESKTOP_DIR=/path/to/apps/desktop node tests/widget_antigravity_switch_browser.cjs
//
// Synthetic data only. Prints a JSON report and exits non-zero on the first
// failed check; tests/test_widget_antigravity_switch_browser.py skips when the
// host build is unavailable.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');

const ROOT = path.resolve(__dirname, '..');
const HOST = process.env.QUOTA_DESKTOP_DIR
  || path.join(os.homedir(), '.hermes', 'hermes-agent', 'apps', 'desktop');
const OUT = fs.mkdtempSync(path.join(os.tmpdir(), 'quota-ag-switch-'));
const req = createRequire(path.join(HOST, 'package.json'));
const { build } = req('esbuild');
const { chromium } = req('playwright');

function newestCss(dir) {
  const assets = path.join(dir, 'dist', 'assets');
  const styles = fs.readdirSync(assets).filter((f) => f.endsWith('.css')).sort();
  // Desktop builds have used both `index-*.css` and `main-*.css`; prefer the
  // entry stylesheet when present, then fall back to the last stylesheet.
  const name = styles.find((f) => /^index-/.test(f))
    || styles.find((f) => /^main-/.test(f))
    || styles[styles.length - 1];
  if (!name) throw new Error(`no stylesheet found in ${assets}`);
  return path.join(assets, name);
}

// The host's Playwright package may pin a Chromium build newer than the one
// installed in the shared cache, and a sandboxed HOME can hide that cache
// entirely. Try every plausible binary, headless shell included: some sandboxed
// runners cannot start the full browser but do run the headless shell.
function chromiumCandidates() {
  let accountHome = os.homedir();
  try {
    accountHome = os.userInfo().homedir;
  } catch {
    /* keep os.homedir() */
  }
  const caches = [...new Set([
    path.join(os.homedir(), '.cache', 'ms-playwright'),
    path.join(accountHome, '.cache', 'ms-playwright'),
  ])];
  const candidates = [];
  for (const cache of caches) {
    let builds = [];
    try {
      builds = fs.readdirSync(cache);
    } catch {
      continue;
    }
    const newestFirst = (prefix) => builds
      .filter((name) => new RegExp(`^${prefix}-\\d+$`).test(name))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
    for (const entry of newestFirst('chromium')) {
      for (const dir of ['chrome-linux64', 'chrome-linux']) {
        const candidate = path.join(cache, entry, dir, 'chrome');
        if (fs.existsSync(candidate)) candidates.push(candidate);
      }
    }
    for (const entry of newestFirst('chromium_headless_shell')) {
      const candidate = path.join(cache, entry, 'chrome-headless-shell-linux64', 'chrome-headless-shell');
      if (fs.existsSync(candidate)) candidates.push(candidate);
    }
  }
  try {
    candidates.push(chromium.executablePath());
  } catch {
    /* Playwright has no bundled browser */
  }
  return [...new Set(candidates.filter(Boolean))];
}

async function launchChromium() {
  const failures = [];
  for (const executablePath of chromiumCandidates()) {
    try {
      return await chromium.launch({ headless: true, executablePath });
    } catch (error) {
      failures.push(`${executablePath}: ${String(error.message).split('\n')[0]}`);
    }
  }
  throw new Error(`no Chromium could be launched: ${failures.join(' | ')}`);
}

const switchStatus = (enabled) => ({
  provider: 'antigravity',
  setting: 'antigravityEnabled',
  enabled,
  config: enabled,
  config_explicit: enabled,
  env: null,
  env_override: false,
  source: enabled ? 'config' : 'default',
  warning: null,
});

const provider = (label) => ({
  label,
  windows: [{ label: '5h', used_percent: 20, reset_at: '2099-01-01T05:00:00Z' }],
});

const payload = (enabled) => ({
  fetched_at: '2026-01-01T00:00:00Z',
  age_s: 1,
  providers: {
    antigravity: provider('antigravity'),
    'antigravity:work': provider('antigravity · Work'),
    anthropic: provider('anthropic'),
  },
  provider_switches: { antigravity: switchStatus(enabled) },
});

// The SDK stub: real atoms with subscriptions, a reactive focused owner and a
// quota query that answers with the focused profile's fixture.
const sdk = `
import React from 'react';
export const atom = (value) => {
  const subs = new Set();
  let current = value;
  return {
    get: () => current,
    set: (next) => {
      if (Object.is(next, current)) return;
      current = next;
      for (const fn of [...subs]) fn();
    },
    subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); },
  };
};
export const useValue = (a) => React.useSyncExternalStore(a.subscribe, a.get, a.get);
const ownerAtom = atom({connectionId: 'local', profile: window.__initialProfile || 'work'});
const dataVersion = atom(0);
window.__owner = ownerAtom;
export const useQuery = (options) => {
  const key = (options && options.queryKey) || [];
  const owner = React.useSyncExternalStore(ownerAtom.subscribe, ownerAtom.get, ownerAtom.get);
  React.useSyncExternalStore(dataVersion.subscribe, dataVersion.get, dataVersion.get);
  if (key[1] === 'monitor') {
    return {data: undefined, isError: false, isPlaceholderData: false, isLoading: false};
  }
  const table = window.__fixtures || {};
  const data = owner && table[owner.profile] ? table[owner.profile] : null;
  return {
    data: options && options.enabled === false ? undefined : data,
    isError: false,
    isPlaceholderData: false,
    isLoading: false,
    queryKey: key,
  };
};
export const useQueryClient = () => ({
  invalidateQueries: () => dataVersion.set(dataVersion.get() + 1),
  setQueryData: () => {},
});
export const queryClient = {getQueriesData: () => [], getQueryCache: () => ({subscribe: () => () => {}})};
export const useMutation = () => ({});
export const usePluginI18n = () => window.__t;
export const useI18n = () => ({locale: 'en'});
export const cn = (...args) => args.filter(Boolean).join(' ');
export const fmtDayTime = new Intl.DateTimeFormat('en-US');
export const PANES_AREA = '', ROUTES_AREA = '', SIDEBAR_NAV_AREA = '', STATUSBAR_AREAS = {right: ''};
export const StatusDot = (props) => React.createElement('span', {'data-tone': props.tone});
export const Input = 'input';
export const Switch = 'input';
export const SegmentedControl = 'span';
export const Tip = (props) => props.children;
export const Popover = (props) => {
  const kids = [].concat(props.children).filter(Boolean);
  return kids.length === 1 ? kids[0] : React.createElement('span', null, kids);
};
export const PopoverTrigger = (props) => props.children;
export const PopoverContent = (props) => React.createElement('div', props);
export const ConfirmDialog = () => null;
export const icons = {RefreshCw: (props) => React.createElement('span', props, 'R')};
export const host = {
  state: {focusedSessionOwner: ownerAtom},
  profileRoutes: async () => [
    {connectionId: 'local', profile: 'work', targetProfile: 'work', mode: 'local'},
    {connectionId: 'local', profile: 'home', targetProfile: 'home', mode: 'local'},
  ],
  requestProfile: (route, method, params) => {
    const argv = (params && params.argv) || [];
    if (argv.includes('switch')) {
      window.__switches.push({profile: route.profile, argv});
      if (window.__failNextSwitch) {
        window.__failNextSwitch = false;
        return Promise.reject(new Error(
          'gateway: reconnecting\\n' + JSON.stringify({ok: false, error: 'managed install: config is read-only'}),
        ));
      }
      return new Promise((resolve) => window.__pending.push({profile: route.profile, argv, resolve}));
    }
    window.__refreshes.push({profile: route.profile, argv});
    return Promise.resolve({code: 0, output: '{}'});
  },
  notify: () => {},
};
`;

(async () => {
  const pluginSource = fs.readFileSync(path.join(ROOT, 'desktop/plugin.js'), 'utf8')
    + '\nexport { QuotaSettings, QuotaPane };';
  await build({
    stdin: {
      contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import plugin, { QuotaSettings, QuotaPane } from 'quota-under-test';
const storage = new Map(Object.entries(window.__seedStorage || {}));
window.__store = storage;
const ctx = {
  storage: {
    get: (k) => storage.get(k),
    set: (k, v) => storage.set(k, v),
    remove: (k) => storage.delete(k),
  },
  i18n: {register: (bundles) => { window.__bundles = bundles; }},
  os: {notify: () => {}, writeClipboard: async () => true},
  register: () => () => {},
};
plugin.register(ctx);
window.__t = (key, ...args) => {
  const table = (window.__bundles && window.__bundles.en) || {};
  const value = key in table ? table[key] : key;
  return typeof value === 'function' ? value(...args) : value;
};
const mount = (id, element) => {
  const root = createRoot(document.getElementById(id));
  root.render(element);
  return root;
};
// The route page and the docked pane mount the same settings component; the
// third root is the quota card list, the surface the switch must repaint.
window.__roots = {
  route: mount('route', React.createElement(QuotaSettings, {})),
  dock: mount('dock', React.createElement(QuotaSettings, {})),
  pane: mount('pane', React.createElement(QuotaPane, {})),
};
window.__setOwner = (profile) => window.__owner.set({connectionId: 'local', profile});
window.__pending = [];
window.__switches = [];
window.__refreshes = [];
`,
      resolveDir: HOST,
      loader: 'jsx',
    },
    bundle: true,
    format: 'iife',
    outfile: path.join(OUT, 'bundle.js'),
    jsx: 'automatic',
    nodePaths: [path.join(HOST, 'node_modules'), path.join(HOST, '..', '..', 'node_modules')],
    plugins: [{
      name: 'offline-sdk',
      setup(b) {
        b.onResolve({ filter: /^quota-under-test$/ }, () => ({ path: 'quota', namespace: 'quota' }));
        b.onLoad({ filter: /.*/, namespace: 'quota' }, () => ({
          contents: pluginSource,
          resolveDir: path.join(ROOT, 'desktop'),
          loader: 'js',
        }));
        b.onResolve({ filter: /^@hermes\/plugin-sdk$/ }, () => ({ path: 'sdk', namespace: 'fixture' }));
        b.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({
          contents: sdk,
          resolveDir: HOST,
          loader: 'js',
        }));
      },
    }],
  });

  const css = fs.readFileSync(newestCss(HOST), 'utf8');
  fs.writeFileSync(path.join(OUT, 'index.html'), `<!doctype html><html><head><meta charset="utf-8">
<style>${css}</style>
<style>
html,body{margin:0;height:100%;background:var(--ui-bg-chrome);color:var(--ui-text-primary);font-family:system-ui,sans-serif}
#route,#dock,#pane{box-sizing:border-box;padding:8px;vertical-align:top;display:inline-block}
#route{width:620px}#dock{width:320px}#pane{width:320px;height:620px;overflow:auto}
</style></head><body><div id="route"></div><div id="dock"></div><div id="pane"></div>
<script>
window.__seedStorage = {};
window.__fixtures = FIXTURES;
</script>
<script src="bundle.js"></script></body></html>`.replace('FIXTURES', JSON.stringify({
    work: payload(true),
    home: payload(true),
  })));

  const browser = await launchChromium();
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (url.origin === 'http://quota.test' && ['index.html', 'bundle.js'].includes(name)) {
      return route.fulfill({
        path: path.join(OUT, name),
        contentType: name.endsWith('.js') ? 'application/javascript' : 'text/html',
      });
    }
    return route.abort();
  });
  await page.goto('http://quota.test/');

  const checks = [];
  const check = (name, ok, detail) => {
    checks.push({ name, ok: Boolean(ok), detail });
    if (!ok) throw new Error(`${name}: ${JSON.stringify(detail)}`);
  };
  const PILL = (root) => `#${root} [data-provider-toggle="antigravity"]`;
  const pillState = (root) => page.$eval(PILL(root), (el) => ({
    pressed: el.getAttribute('aria-pressed') === 'true',
    disabled: el.disabled,
    title: el.getAttribute('title') || '',
  }));
  const paneText = () => page.$eval('#pane', (el) => el.innerText);
  const calls = () => page.evaluate(() => ({
    switches: window.__switches.map((c) => ({ profile: c.profile, argv: c.argv })),
    refreshes: window.__refreshes.map((c) => ({ profile: c.profile, argv: c.argv })),
    pending: window.__pending.length,
  }));

  await page.waitForSelector(PILL('route'), { timeout: 15000 });
  await page.waitForSelector(PILL('dock'), { timeout: 15000 });

  // 1. Both mounted surfaces agree before any click.
  const route0 = await pillState('route');
  const dock0 = await pillState('dock');
  check('both-surfaces-start-on', route0.pressed && dock0.pressed && !route0.disabled && !dock0.disabled,
    { route0, dock0 });
  check('pane-shows-antigravity', (await paneText()).includes('Antigravity'), await paneText());

  // 2. Flip off from the route page: both surfaces repaint before the backend
  //    answers, and the card list drops Antigravity immediately.
  await page.click(PILL('route'));
  await page.waitForFunction(() => window.__pending.length === 1);
  const route1 = await pillState('route');
  const dock1 = await pillState('dock');
  check('optimistic-off-repaints-both-surfaces', !route1.pressed && !dock1.pressed, { route1, dock1 });
  check('pending-write-disables-both-surfaces', route1.disabled && dock1.disabled, { route1, dock1 });
  check('optimistic-off-hides-the-card-now', !(await paneText()).includes('Antigravity'), await paneText());
  const pendingCalls = await calls();
  check('exactly-one-switch-write', pendingCalls.switches.length === 1, pendingCalls.switches);
  check('first-write-is-off-for-work',
    JSON.stringify(pendingCalls.switches[0].argv) === JSON.stringify(['--profile', 'work', 'quota', 'switch', 'antigravity', 'off', '--json']),
    pendingCalls.switches[0]);

  // 3. The docked pane cannot start a competing write while that one is open:
  //    the shared busy flag (not a per-instance `useState`) disables it, so a
  //    click on the other surface is inert instead of sending the opposite flip.
  const inert = await page.evaluate(async () => {
    const el = document.querySelector('#dock [data-provider-toggle="antigravity"]');
    let hits = 0;
    el.addEventListener('click', () => { hits += 1; }, {once: true});
    el.click();
    // A write, if one were accepted, starts after the CLI route lookup.
    await new Promise((resolve) => setTimeout(resolve, 250));
    return {hits, switches: window.__switches.length};
  });
  check('second-surface-click-is-inert-while-busy',
    inert.hits === 0 && inert.switches === 1, inert);
  const route2 = await pillState('route');
  const dock2 = await pillState('dock');
  check('refused-click-keeps-the-optimistic-value', !route2.pressed && !dock2.pressed, { route2, dock2 });

  // 4. Resolving the write frees the slot and refreshes exactly once.
  await page.evaluate(() => window.__pending.shift().resolve({ code: 0, output: '{}' }));
  await page.waitForFunction(() => {
    const el = document.querySelector('#route [data-provider-toggle="antigravity"]');
    return el && !el.disabled;
  });
  const settled = await calls();
  check('still-one-switch-write-after-settling', settled.switches.length === 1, settled.switches);
  check('one-follow-up-refresh',
    settled.refreshes.length === 1
    && JSON.stringify(settled.refreshes[0].argv) === JSON.stringify(['--profile', 'work', 'quota', 'refresh']),
    settled.refreshes);
  const settledDock = await pillState('dock');
  check('busy-cleared-on-the-other-surface', !settledDock.disabled && !settledDock.pressed, settledDock);
  check('settled-off-keeps-the-card-hidden', !(await paneText()).includes('Antigravity'), await paneText());

  // 5. The freed slot accepts the next flip, from the other surface this time.
  await page.click(PILL('dock'));
  await page.waitForFunction(() => window.__pending.length === 1);
  await page.evaluate(() => window.__pending.shift().resolve({ code: 0, output: '{}' }));
  await page.waitForFunction(() => {
    const el = document.querySelector('#route [data-provider-toggle="antigravity"]');
    return el && !el.disabled && el.getAttribute('aria-pressed') === 'true';
  });
  const second = await calls();
  check('second-flip-is-on-and-serialized', second.switches.length === 2
    && JSON.stringify(second.switches[1].argv) === JSON.stringify(['--profile', 'work', 'quota', 'switch', 'antigravity', 'on', '--json']),
  second.switches);
  check('on-restores-the-card', (await paneText()).includes('Antigravity'), await paneText());

  // 6. Profile switch: an optimistic value set in one profile must not repaint
  //    or filter another profile after the focus moves.
  //    First leave work off, then move to home (whose payload is on).
  await page.click(PILL('route'));
  await page.waitForFunction(() => window.__pending.length === 1);
  await page.evaluate(() => window.__pending.shift().resolve({ code: 0, output: '{}' }));
  await page.waitForFunction(() => {
    const el = document.querySelector('#route [data-provider-toggle="antigravity"]');
    return el && !el.disabled;
  });
  check('work-off-before-the-profile-switch', !(await pillState('route')).pressed, await pillState('route'));

  await page.evaluate(() => window.__setOwner('home'));
  await page.waitForFunction(() => {
    const el = document.querySelector('#route [data-provider-toggle="antigravity"]');
    return el && el.getAttribute('aria-pressed') === 'true';
  });
  const homeRoute = await pillState('route');
  const homeDock = await pillState('dock');
  check('profile-switch-shows-the-new-profiles-state', homeRoute.pressed && homeDock.pressed, { homeRoute, homeDock });
  check('profile-switch-keeps-the-new-profiles-cards', (await paneText()).includes('Antigravity'), await paneText());

  // Flip home off, then go back to work: work keeps its own value (off), so
  // home's flip neither wrote work nor painted it.
  await page.click(PILL('route'));
  await page.waitForFunction(() => window.__pending.length === 1);
  check('home-flip-hides-home-cards', !(await paneText()).includes('Antigravity'), await paneText());
  await page.evaluate(() => window.__pending.shift().resolve({ code: 0, output: '{}' }));
  await page.waitForFunction(() => {
    const el = document.querySelector('#route [data-provider-toggle="antigravity"]');
    return el && !el.disabled;
  });
  await page.evaluate(() => window.__setOwner('work'));
  await page.waitForTimeout(150);
  const backToWork = await pillState('route');
  check('profile-switch-back-keeps-work-off', !backToWork.pressed, backToWork);
  check('home-flip-did-not-write-work', !(await paneText()).includes('Antigravity'), await paneText());
  const profiledCalls = await calls();
  check('each-write-carries-its-own-profile',
    profiledCalls.switches.map((c) => c.profile).join(',') === 'work,work,work,home',
    profiledCalls.switches);

  // 7. A failed write names the error on both surfaces of that profile only.
  await page.evaluate(() => { window.__failNextSwitch = true; });
  await page.click(PILL('route'));
  await page.waitForFunction(() => !document.querySelector('#route [data-provider-toggle="antigravity"]').disabled);
  const workBody = await page.$eval('#route', (el) => el.innerText);
  const workDockBody = await page.$eval('#dock', (el) => el.innerText);
  check('failed-write-explains-itself-on-both-surfaces',
    workBody.includes('Could not save the Antigravity switch')
    && workBody.includes('managed install: config is read-only')
    && workDockBody.includes('managed install: config is read-only'),
    { workBody, workDockBody });
  const reverted = await pillState('route');
  check('failed-write-reverts-the-optimistic-value', reverted.pressed === false, reverted);

  await page.evaluate(() => window.__setOwner('home'));
  await page.waitForTimeout(150);
  const homeBody = await page.$eval('#route', (el) => el.innerText);
  const homePill = await pillState('route');
  check('failure-does-not-follow-the-profile-switch',
    !homeBody.includes('managed install: config is read-only') && !homePill.pressed,
    { homeBody, homePill });

  await browser.close();
  process.stdout.write(JSON.stringify({ checks, errors }));
})().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
