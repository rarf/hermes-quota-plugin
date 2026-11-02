// Real-Chromium responsiveness regression for the sort/reset confirmation.
//
//   QUOTA_DESKTOP_DIR=/path/to/apps/desktop node tests/widget_card_confirm_browser.cjs
//
// Builds the real desktop/plugin.js against the host's compiled CSS AND the
// host's real ConfirmDialog (Radix + dialog shell), then measures click ->
// dialog paint, click -> close, and pointer -> hover fill with React Profiler
// commit times. The card list must not re-render just to open or cancel the
// question; confirming one layout change may re-render it once. Hover must
// reach its final fill within a frame or two (the dialog scopes off the host
// Button's 100 ms colour ramp), on Cancel and Confirm alike. Synthetic data
// only.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');

const ROOT = path.resolve(__dirname, '..');
const HOST = process.env.QUOTA_DESKTOP_DIR
  || path.join(os.homedir(), '.hermes', 'hermes-agent', 'apps', 'desktop');
const OUT = fs.mkdtempSync(path.join(os.tmpdir(), 'quota-confirm-browser-'));
const req = createRequire(path.join(HOST, 'package.json'));
const { build } = req('esbuild');
const { chromium } = req('playwright');

const provider = (used, plan = 'Pro') => ({
  plan,
  windows: [
    { label: 'Session', used_percent: used, reset_at: '2099-01-01T00:00:00Z' },
    { label: 'Weekly', used_percent: Math.min(99, used + 9), reset_at: '2099-01-02T00:00:00Z' },
  ],
  details: ['Synthetic detail line for layout only'],
});
const ids = ['anthropic', 'deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama', 'mistral', 'groq', 'xai'];
const fixture = {
  fetched_at: '2026-01-01T00:00:00Z',
  age_s: 12,
  providers: Object.fromEntries(ids.map((id, i) => [id, provider(10 + i * 6)])),
};

const sdk = `
import React from 'react';
import { ConfirmDialog as HostConfirmDialog } from '@/components/ui/confirm-dialog';
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
export const ConfirmDialog = HostConfirmDialog; export const icons = { RefreshCw: (props) => React.createElement('span', props, '↻') };
`;

function newestCss(dir) {
  const assets = path.join(dir, 'dist', 'assets');
  const name = fs.readdirSync(assets).filter((f) => /^index-.*\.css$/.test(f)).sort().pop();
  return path.join(assets, name);
}
function chromiumPath() {
  const cache = path.join(os.homedir(), '.cache', 'ms-playwright');
  try {
    const builds = fs.readdirSync(cache)
      .filter((name) => /^chromium-\d+$/.test(name))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
    for (const buildDir of builds) {
      for (const candidate of [path.join(cache, buildDir, 'chrome-linux64', 'chrome'), path.join(cache, buildDir, 'chrome-linux', 'chrome')]) {
        if (fs.existsSync(candidate)) return candidate;
      }
    }
  } catch {
    /* fall through */
  }
  return chromium.executablePath();
}

(async () => {
  const pluginSource = fs.readFileSync(path.join(ROOT, 'desktop/plugin.js'), 'utf8')
    + '\nexport { QuotaPane, cardLayoutAtom };';
  await build({
    stdin: {
      contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import plugin, { QuotaPane, cardLayoutAtom } from 'quota-under-test';
const storage = new Map(Object.entries(window.__seedStorage || {}));
window.__store = storage;
window.__setLayout = (layout) => cardLayoutAtom.set(layout);
const t = (key, ...args) => { const table = window.__bundles && (window.__bundles.en || {}); const value = table && key in table ? table[key] : key; return typeof value === 'function' ? value(...args) : value; };
window.__t = t;
window.__commits = [];
const ctx = { storage: { get: (k) => storage.get(k), set: (k, v) => storage.set(k, v), remove: (k) => storage.delete(k) }, i18n: { register: (b) => { window.__bundles = b; } }, os: { notify: () => {}, writeClipboard: async () => true }, register: () => () => {} };
plugin.register(ctx);
window.__mountSecondPane = () => {
  const el = document.createElement('div');
  el.id = 'paneB';
  el.style.cssText = 'width:300px;height:540px;display:flex;margin-top:12px';
  document.getElementById('root').appendChild(el);
  createRoot(el).render(React.createElement(QuotaPane, {}));
  return true;
};
createRoot(document.getElementById('root')).render(
  React.createElement('div', { id: 'pane', style: { width: '300px', height: '540px', display: 'flex' } },
    React.createElement(React.Profiler, {
      id: 'pane',
      onRender: (id, phase, actualDuration, baseDuration, startTime, commitTime) => {
        window.__commits.push({ id, phase, actualDuration, baseDuration, startTime, commitTime });
      },
    }, React.createElement(QuotaPane, {}))),
);
`,
      resolveDir: HOST,
      loader: 'jsx',
    },
    bundle: true,
    format: 'iife',
    outfile: path.join(OUT, 'bundle.js'),
    jsx: 'automatic',
    tsconfig: path.join(HOST, 'tsconfig.json'),
    nodePaths: [path.join(HOST, 'node_modules'), path.join(HOST, '..', '..', 'node_modules')],
    plugins: [{
      name: 'offline-sdk',
      setup(b) {
        b.onResolve({ filter: /^quota-under-test$/ }, () => ({ path: 'quota', namespace: 'quota' }));
        b.onLoad({ filter: /.*/, namespace: 'quota' }, () => ({ contents: pluginSource, resolveDir: path.join(ROOT, 'desktop'), loader: 'js' }));
        b.onResolve({ filter: /^@hermes\/plugin-sdk$/ }, () => ({ path: 'sdk', namespace: 'fixture' }));
        b.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({ contents: sdk, resolveDir: HOST, loader: 'js' }));
      },
    }],
  });
  const css = fs.readFileSync(newestCss(HOST), 'utf8');
  fs.writeFileSync(path.join(OUT, 'index.html'), `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style>
<style>html,body{margin:0;height:100%;background:var(--ui-bg-chrome);color:var(--ui-text-primary);font-family:system-ui,sans-serif}#pane{box-sizing:border-box}</style>
</head><body><div id="root"></div><script>window.__seedStorage=${JSON.stringify({})}; window.__quotaFixture=${JSON.stringify(fixture)};</script><script src="bundle.js"></script></body></html>`);

  const browser = await chromium.launch({ headless: true, executablePath: chromiumPath() });
  const page = await browser.newPage({ viewport: { width: 700, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (url.origin === 'http://quota.test' && ['index.html', 'bundle.js'].includes(name)) {
      return route.fulfill({ path: path.join(OUT, name), contentType: name.endsWith('.js') ? 'application/javascript' : 'text/html' });
    }
    return route.abort();
  });
  await page.goto('http://quota.test/');
  await page.waitForSelector('[data-card-edit]');
  await page.waitForTimeout(300);

  const checks = [];
  const check = (name, ok, detail) => {
    checks.push({ name, ok: Boolean(ok), detail });
    if (!ok) throw new Error(`${name}: ${JSON.stringify(detail)}`);
  };

  // Baseline: a full card-tree render (entering edit mode). Prompt opens and
  // cancels must cost a small fraction of this.
  const baseline = await page.evaluate(async () => {
    const start = window.__commits.length;
    const before = performance.now();
    document.querySelector('[data-card-edit]').click();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const commits = window.__commits.slice(start);
    return {
      paint: performance.now() - before,
      maxCommit: Math.max(0, ...commits.map((c) => c.actualDuration)),
      totalCommit: commits.reduce((sum, c) => sum + c.actualDuration, 0),
      commits: commits.length,
    };
  });
  await page.waitForSelector('[data-card-shell]');
  await page.waitForTimeout(300);

  // Hover must be answered on the next frame. The host Button paints every
  // state change through `transition-all duration-100`; the confirmation
  // scopes that ramp off for its own footer buttons, so a real pointer move
  // reaches the final fill within a frame or two instead of ~130 ms later.
  const hoverButton = async (which) => {
    const box = await page.evaluate((which) => {
      const dialog = document.querySelector('[data-slot="dialog-content"]');
      const buttons = [...dialog.querySelectorAll('button')];
      const isCancel = (b) => /^cancel$/i.test((b.textContent || '').trim());
      const button = which === 'cancel'
        ? buttons.find(isCancel)
        : buttons.filter((b) => !isCancel(b) && b.getAttribute('aria-label') !== 'Close').pop();
      if (!button) throw new Error(`missing ${which} button`);
      const r = button.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    }, which);
    await page.mouse.move(2, 2);
    await page.waitForTimeout(220);
    const base = await page.evaluate((which) => {
      const dialog = document.querySelector('[data-slot="dialog-content"]');
      const buttons = [...dialog.querySelectorAll('button')];
      const isCancel = (b) => /^cancel$/i.test((b.textContent || '').trim());
      const button = which === 'cancel'
        ? buttons.find(isCancel)
        : buttons.filter((b) => !isCancel(b) && b.getAttribute('aria-label') !== 'Close').pop();
      const style = getComputedStyle(button);
      return {
        bg: style.backgroundColor,
        transitionProperty: style.transitionProperty,
        transitionDuration: style.transitionDuration,
        transitionDelay: style.transitionDelay,
      };
    }, which);
    await page.evaluate((which) => {
      const dialog = document.querySelector('[data-slot="dialog-content"]');
      const buttons = [...dialog.querySelectorAll('button')];
      const isCancel = (b) => /^cancel$/i.test((b.textContent || '').trim());
      const button = which === 'cancel'
        ? buttons.find(isCancel)
        : buttons.filter((b) => !isCancel(b) && b.getAttribute('aria-label') !== 'Close').pop();
      window.__hover = { samples: [], running: true };
      const t0 = performance.now();
      const tick = () => {
        if (!window.__hover.running || !button.isConnected) {
          window.__hover.running = false;
          return;
        }
        window.__hover.samples.push({
          t: performance.now() - t0,
          bg: getComputedStyle(button).backgroundColor,
        });
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }, which);
    await page.mouse.move(box.x, box.y);
    await page.waitForTimeout(260);
    const measured = await page.evaluate(({ which, baseBg }) => {
      window.__hover.running = false;
      const dialog = document.querySelector('[data-slot="dialog-content"]');
      const buttons = [...dialog.querySelectorAll('button')];
      const isCancel = (b) => /^cancel$/i.test((b.textContent || '').trim());
      const button = which === 'cancel'
        ? buttons.find(isCancel)
        : buttons.filter((b) => !isCancel(b) && b.getAttribute('aria-label') !== 'Close').pop();
      const r = button.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      const samples = window.__hover.samples;
      const finalBg = samples.length ? samples[samples.length - 1].bg : null;
      // Compare against the pre-move fill: the first rAF sample can already
      // carry the hover fill if the pointer move beat the frame callback.
      const change = samples.find((sample) => sample.bg !== baseBg) || null;
      const settled = samples.find((sample) => sample.bg === finalBg) || null;
      return {
        hit: Boolean(hit && (hit === button || button.contains(hit))),
        hitTag: hit ? hit.getAttribute('data-slot') || hit.tagName : null,
        samples,
        firstChange: change,
        settledAt: settled ? settled.t : null,
        finalBg,
      };
    }, { which, baseBg: base.bg });
    return { box, base, ...measured };
  };

  await page.evaluate(() => document.querySelector('[data-card-sort="asc"]').click());
  await page.waitForSelector('[data-slot="dialog-content"]');
  await page.waitForTimeout(350);
  const cancelHover = await hoverButton('cancel');
  const confirmHover = await hoverButton('confirm');
  const hoverInstant = (probe) => probe.base.transitionDuration === '0s'
    && probe.base.transitionDelay === '0s'
    && probe.hit
    && probe.firstChange
    && probe.firstChange.bg !== probe.base.bg
    && probe.settledAt != null
    && probe.settledAt - probe.firstChange.t <= 40;
  check('confirm-hover-cancel-instant', hoverInstant(cancelHover), {
    transition: `${cancelHover.base.transitionDuration} ${cancelHover.base.transitionDelay}`,
    baseBg: cancelHover.base.bg,
    firstChange: cancelHover.firstChange,
    settledAt: cancelHover.settledAt,
    hit: cancelHover.hit,
    hitTag: cancelHover.hitTag,
  });
  check('confirm-hover-confirm-instant', hoverInstant(confirmHover), {
    transition: `${confirmHover.base.transitionDuration} ${confirmHover.base.transitionDelay}`,
    baseBg: confirmHover.base.bg,
    firstChange: confirmHover.firstChange,
    settledAt: confirmHover.settledAt,
    hit: confirmHover.hit,
    hitTag: confirmHover.hitTag,
  });
  // The hover probe must not leave the question open or apply the layout.
  const cancelPoint = await page.evaluate(() => {
    const b = [...document.querySelectorAll('[data-slot="dialog-content"] button')]
      .find((n) => /^cancel$/i.test((n.textContent || '').trim()));
    const r = b.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  await page.mouse.click(cancelPoint.x, cancelPoint.y);
  await page.waitForTimeout(200);
  const hoverDialogGone = await page.evaluate(() => !document.querySelector('[data-slot="dialog-content"]'));
  const hoverStored = await page.evaluate(() => window.__store.get('cardLayout') || null);
  check('confirm-hover-cancel-closes', hoverDialogGone && !hoverStored,
    { gone: hoverDialogGone, stored: hoverStored });

  const measurePrompt = async (openSelector, action) => page.evaluate(async ({ openSelector, action }) => {
    const button = document.querySelector(openSelector);
    const content = () => document.querySelector('[data-slot="dialog-content"]');
    const cancel = () => [...document.querySelectorAll('[data-slot="dialog-content"] button')]
      .find((b) => /^cancel$/i.test((b.textContent || '').trim()));
    const confirm = () => {
      const buttons = [...document.querySelectorAll('[data-slot="dialog-content"] button')]
        .filter((b) => !/^cancel$/i.test((b.textContent || '').trim()))
        .filter((b) => b.getAttribute('aria-label') !== 'Close');
      return buttons[buttons.length - 1];
    };
    const t0 = performance.now();
    const start = window.__commits.length;
    const marks = {};
    const mo = new MutationObserver(() => {
      if (content() && marks.insert == null) marks.insert = performance.now() - t0;
    });
    mo.observe(document.body, { childList: true, subtree: true });
    button.click();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    marks.paint = performance.now() - t0;
    marks.openCommits = window.__commits.slice(start);
    marks.hasCancel = Boolean(cancel());
    marks.hasConfirm = Boolean(confirm());
    marks.text = content() ? content().textContent : '';
    await new Promise((r) => setTimeout(r, 250));
    const target = action === 'cancel' ? cancel() : confirm();
    if (!target) throw new Error('dialog target missing');
    const t1 = performance.now();
    const closeStart = window.__commits.length;
    target.click();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    marks.closePaint = performance.now() - t1;
    marks.gone = !content();
    await new Promise((r) => setTimeout(r, 120));
    marks.goneAfter120 = !content();
    marks.closeCommits = window.__commits.slice(closeStart);
    marks.stored = window.__store.get('cardLayout') || null;
    mo.disconnect();
    return marks;
  }, { openSelector, action });

  const sum = (commits) => commits.reduce((total, c) => total + c.actualDuration, 0);
  const max = (commits) => Math.max(0, ...commits.map((c) => c.actualDuration));
  const promptBudget = baseline.maxCommit * 0.5;

  const sortOpen = await measurePrompt('[data-card-sort="asc"]', 'cancel');
  check('sort-dialog-present', sortOpen.hasCancel && sortOpen.hasConfirm
    && sortOpen.text.includes('Replace custom unpinned order?')
    && sortOpen.text.includes('Sorting the unpinned cards replaces their custom order.'),
  { hasCancel: sortOpen.hasCancel, hasConfirm: sortOpen.hasConfirm, text: sortOpen.text.slice(0, 160) });
  check('sort-cancel-no-list-render', max(sortOpen.openCommits) < promptBudget
    && max(sortOpen.closeCommits) < promptBudget,
  { baseline: baseline.maxCommit, budget: promptBudget, openMax: max(sortOpen.openCommits), closeMax: max(sortOpen.closeCommits) });
  check('sort-cancel-responsive', sortOpen.paint < 150 && sortOpen.closePaint < 100 && !sortOpen.stored,
    { openPaint: sortOpen.paint, closePaint: sortOpen.closePaint, stored: sortOpen.stored });

  const sortConfirm = await measurePrompt('[data-card-sort="asc"]', 'confirm');
  const sorted = (sortConfirm.stored && sortConfirm.stored.order) || [];
  check('sort-confirm-applies', sortConfirm.gone && sortConfirm.goneAfter120
    && sorted.length === ids.length && sorted[0] === 'anthropic', { sorted, gone: sortConfirm.gone });
  check('sort-confirm-responsive', sortConfirm.closePaint < 150,
    { openPaint: sortConfirm.paint, closePaint: sortConfirm.closePaint, confirmCommit: max(sortConfirm.closeCommits) });

  const reset = await measurePrompt('[data-card-reset]', 'confirm');
  check('reset-dialog-present', reset.hasCancel && reset.hasConfirm
    && reset.text.includes('Clear all pins and reset the card order?')
    && reset.text.includes('This removes every pin and the custom card order'),
  { hasCancel: reset.hasCancel, hasConfirm: reset.hasConfirm, text: reset.text.slice(0, 160) });
  check('reset-confirm-applies', reset.gone && reset.goneAfter120
    && reset.stored && reset.stored.pinned.length === 0 && reset.stored.order.length === 0,
  { stored: reset.stored, gone: reset.gone });
  check('reset-confirm-responsive', reset.paint < 150 && reset.closePaint < 150,
    { openPaint: reset.paint, closePaint: reset.closePaint });

  // Two mounted quota surfaces share the prompt atom but must never paint two
  // dialogs: the prompt names the pane that opened it. Edit mode is pane-local,
  // so the fresh pane arrives quiet even while the first one is editing; it
  // must then enter its own edit session without disturbing the first.
  await page.evaluate(() => window.__mountSecondPane());
  await page.waitForTimeout(300);
  const twoPaneStart = await page.evaluate(() => ['pane', 'paneB'].map((id) => {
    const pane = document.getElementById(id);
    const toggle = pane.querySelector('[data-card-edit]');
    return {
      id,
      shells: pane.querySelectorAll('[data-card-shell]').length,
      editPressed: toggle && toggle.getAttribute('aria-pressed'),
    };
  }));
  check('second-pane-starts-quiet',
    twoPaneStart[0].shells > 0 && twoPaneStart[0].editPressed === 'true'
      && twoPaneStart[1].shells === 0 && twoPaneStart[1].editPressed === 'false',
    twoPaneStart);
  await page.evaluate(() => document.querySelector('#paneB [data-card-edit]').click());
  await page.waitForTimeout(250);
  const twoPaneEditing = await page.evaluate(() => ['pane', 'paneB'].map((id) => {
    const pane = document.getElementById(id);
    return {
      id,
      shells: pane.querySelectorAll('[data-card-shell]').length,
      editPressed: pane.querySelector('[data-card-edit]').getAttribute('aria-pressed'),
    };
  }));
  check('two-panes-edit-independently',
    twoPaneEditing[0].shells === twoPaneStart[0].shells
      && twoPaneEditing[0].editPressed === 'true'
      && twoPaneEditing[1].shells > 0 && twoPaneEditing[1].editPressed === 'true',
    { start: twoPaneStart, editing: twoPaneEditing });
  const sortButtons = await page.evaluate(() => document.querySelectorAll('[data-card-sort="asc"]').length);
  check('second-pane-mounted', sortButtons === 2, { sortButtons });
  await page.evaluate(() => {
    const buttons = [...document.querySelectorAll('[data-card-sort="asc"]')];
    buttons[buttons.length - 1].click();
  });
  await page.waitForTimeout(250);
  const dialogCount = await page.evaluate(() => document.querySelectorAll('[data-slot="dialog-content"]').length);
  check('single-confirmation-with-two-panes', dialogCount === 1, { dialogCount });
  await page.evaluate(() => {
    const cancel = [...document.querySelectorAll('[data-slot="dialog-content"] button')]
      .find((b) => /^cancel$/i.test((b.textContent || '').trim()));
    if (cancel) cancel.click();
  });
  await page.waitForTimeout(200);
  const dialogAfterCancel = await page.evaluate(() => document.querySelectorAll('[data-slot="dialog-content"]').length);
  check('two-pane-cancel-closes', dialogAfterCancel === 0, { dialogAfterCancel });

  check('no-page-errors', errors.length === 0, errors);
  await browser.close();
  fs.rmSync(OUT, { recursive: true, force: true });
  console.log(JSON.stringify({
    baseline,
    checks,
    timings: {
      sortOpen: { insert: sortOpen.insert, paint: sortOpen.paint, closePaint: sortOpen.closePaint, openCommitMax: max(sortOpen.openCommits), closeCommitMax: max(sortOpen.closeCommits) },
      sortConfirm: { insert: sortConfirm.insert, paint: sortConfirm.paint, closePaint: sortConfirm.closePaint, confirmCommitMax: max(sortConfirm.closeCommits) },
      reset: { insert: reset.insert, paint: reset.paint, closePaint: reset.closePaint },
    },
    errors,
  }, null, 2));
})().catch((e) => {
  console.error(e.stack || String(e));
  process.exit(1);
});
