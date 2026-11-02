// Real-Chromium regression for the pinned-card band: geometry, single pin
// control, whole-card pointer drag (with control exclusion), edge autoscroll,
// wheel scrolling during a drag, and shelf wheel routing. Also checks that the
// pinned band's opaque backdrop covers the top edge of the card region (no
// unpinned-card sliver above it) without a border or frame, that wheel input
// at either shelf boundary cannot chain into the unpinned list, and that the
// bottom resize edge honours the measured card/window limits for mouse, touch,
// keyboard, viewport and pin-set changes, that a real double-click snaps to
// the card boundary selected by its midpoint (including a scrolled shelf and
// the half-pane cap). Separate native scrollbars are checked for real layout
// gutters, hover paint, pointer drags in both bands (including where the shelf
// bar meets the resize strip), dynamic height, theme and RTL geometry, with
// Playwright's --hide-scrollbars opt-out. The
// default-growth section pins the clarified rule directly: with no saved
// preference the band grows through the last pinned card up to half the pane,
// a click on the edge does not freeze that auto height, and a deliberate
// compact height survives new pins while dragging back to the ceiling returns
// to auto.
//
//   QUOTA_DESKTOP_DIR=/path/to/apps/desktop node tests/widget_card_browser.cjs
//
// Uses the host's compiled Desktop CSS and Playwright; synthetic data only.
// Prints a JSON report and exits non-zero on the first failed check. The
// Python wrapper (tests/test_widget_card_browser.py) skips when the host
// build is unavailable, like the other optional browser suites.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');

const ROOT = path.resolve(__dirname, '..');
const HOST = process.env.QUOTA_DESKTOP_DIR
  || path.join(os.homedir(), '.hermes', 'hermes-agent', 'apps', 'desktop');
const OUT = fs.mkdtempSync(path.join(os.tmpdir(), 'quota-card-browser-'));
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
const fixture = {
  fetched_at: '2026-01-01T00:00:00Z',
  age_s: 12,
  // The monitor's snapshot query reads this same fixture; the version marker
  // makes the summary strip render exactly as it does with a live backend, so
  // the short-pane layout is measured with its real chrome present.
  monitor_version: 1,
  live: { counts: { working: 2, needs_approval: 1 } },
  usage: { today: { totals: { cost: 1.25 } } },
  providers: {
    anthropic: provider(24),
    'anthropic:work': { ...provider(31), account_label: 'Work' },
    deepseek: provider(38),
    openai: provider(52),
    gemini: provider(61),
    cursor: provider(18),
    openrouter: provider(73),
    ollama: provider(44),
  },
};
const ALL = ['anthropic', 'deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama'];

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

function newestCss(dir) {
  const assets = path.join(dir, 'dist', 'assets');
  const name = fs.readdirSync(assets).filter((f) => /^index-.*\.css$/.test(f)).sort().pop();
  return path.join(assets, name);
}

// The host's Playwright package may pin a Chromium build newer than the one
// installed in a shared browser cache; prefer whatever is actually present.
function chromiumPath() {
  const cache = path.join(os.homedir(), '.cache', 'ms-playwright');
  try {
    const builds = fs.readdirSync(cache)
      .filter((name) => /^chromium-\d+$/.test(name))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
    for (const build of builds) {
      const candidate = path.join(cache, build, 'chrome-linux64', 'chrome');
      if (fs.existsSync(candidate)) return candidate;
      const legacy = path.join(cache, build, 'chrome-linux', 'chrome');
      if (fs.existsSync(legacy)) return legacy;
    }
  } catch {
    /* fall back to Playwright's own path */
  }
  return chromium.executablePath();
}

(async () => {
  const pluginSource = fs.readFileSync(path.join(ROOT, 'desktop/plugin.js'), 'utf8')
    + '\nexport { QuotaPane, cardLayoutAtom, pinnedShelfHeightAtom, monitorEnabledAtom };';
  await build({
    stdin: {
      contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import plugin, { QuotaPane, cardLayoutAtom, pinnedShelfHeightAtom, monitorEnabledAtom } from 'quota-under-test';
const storage = new Map(Object.entries(window.__seedStorage || {}));
window.__store = storage;
window.__setLayout = (layout) => cardLayoutAtom.set(layout);
window.__seedLayout = (layout) => { storage.set('cardLayout', layout); cardLayoutAtom.set(layout); };
window.__setShelfHeight = (value) => pinnedShelfHeightAtom.set(value);
window.__setMonitor = (on) => monitorEnabledAtom.set(Boolean(on));
window.__setFixture = (value) => { window.__quotaFixture = value; };
const t = (key, ...args) => { const table = window.__bundles && (window.__bundles.en || {}); const value = table && key in table ? table[key] : key; return typeof value === 'function' ? value(...args) : value; };
window.__t = t;
const ctx = { storage: { get: (k) => storage.get(k), set: (k, v) => storage.set(k, v), remove: (k) => storage.delete(k) }, i18n: { register: (b) => { window.__bundles = b; } }, os: { notify: () => {}, writeClipboard: async () => true }, register: () => () => {} };
plugin.register(ctx);
const paneRoot = createRoot(document.getElementById('root'));
const renderPane = () => paneRoot.render(
  React.createElement('div', { id: 'pane', style: { width: '300px', height: '540px', display: 'flex' } },
    React.createElement(QuotaPane, {})),
);
renderPane();
window.__rerender = () => renderPane();
// A second live surface for the multi-pane observer regression. It stays
// off-screen so it reserves no visual space for the single-pane checks, but
// it still lays out and paints like the docked pane and the route page do.
window.__extraRoots = new Map();
window.__mountExtraPane = (id, width, height) => {
  const host = document.createElement('div');
  host.id = id;
  Object.assign(host.style, {
    position: 'fixed', left: '-2000px', top: '0',
    width: width + 'px', height: height + 'px', display: 'flex',
  });
  document.body.appendChild(host);
  const root = createRoot(host);
  window.__extraRoots.set(id, { root, host });
  root.render(React.createElement(QuotaPane, {}));
  return true;
};
window.__unmountExtraPane = (id) => {
  const entry = window.__extraRoots.get(id);
  if (!entry) return false;
  window.__extraRoots.delete(id);
  entry.root.unmount();
  entry.host.remove();
  return true;
};
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

  const browser = await chromium.launch({
    headless: true,
    executablePath: chromiumPath(),
    // Playwright's headless defaults include --hide-scrollbars, which keeps
    // the layout gutter but removes the painted native bar and its hit
    // target. These regressions drag and screenshot the real scrollbars, so
    // opt out of that default.
    ignoreDefaultArgs: ['--hide-scrollbars'],
  });
  const page = await browser.newPage({ viewport: { width: 700, height: 1000 } });
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

  const checks = [];
  const check = (name, ok, detail) => {
    checks.push({ name, ok: Boolean(ok), detail });
    if (!ok) throw new Error(`${name}: ${JSON.stringify(detail)}`);
  };
  // Opacity of a computed color string, for the native-scrollbar pseudo
  // element state: host CSS reveals the thumb through :hover.
  const alphaOf = (css) => {
    if (!css) return 0;
    if (/transparent|rgba\(0,\s*0,\s*0,\s*0\)/.test(css)) return 0;
    const slash = css.match(/\/\s*([0-9.]+)\s*\)/);
    if (slash) return Number(slash[1]);
    const rgba = css.match(/rgba?\(([^)]+)\)/);
    if (rgba) {
      const parts = rgba[1].split(',');
      return parts.length > 3 ? Number(parts[3]) : 1;
    }
    return 1;
  };
  // Rough relative luminance for a computed `color(srgb …)` / `rgb(…)`
  // string: enough to prove the dark-theme pass really repainted.
  const luminanceOf = (css) => {
    const srgb = String(css).match(/^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
    const channels = srgb
      ? [Number(srgb[1]), Number(srgb[2]), Number(srgb[3])]
      : (() => {
          const rgb = String(css).match(/rgba?\(([^)]+)\)/);
          if (!rgb) return null;
          return rgb[1].split(',').slice(0, 3).map((v) => Number(v) / 255);
        })();
    if (!channels || channels.some((v) => !Number.isFinite(v))) return NaN;
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  // QUOTA_SHOTS=/some/dir keeps visual evidence of the shelf states for a
  // manual review; the normal suite leaves no screenshots behind.
  const shotDir = process.env.QUOTA_SHOTS || '';
  const shot = async (name) => {
    if (!shotDir) return;
    fs.mkdirSync(shotDir, { recursive: true });
    await page.screenshot({ path: path.join(shotDir, `${name}.png`) });
  };
  // Tight evidence for the shelf edge: the full-page shot above cannot make
  // sub-pixel border overlaps obvious at review time.
  const shotElement = async (name, selector) => {
    if (!shotDir) return;
    fs.mkdirSync(shotDir, { recursive: true });
    const el = await page.$(selector);
    if (el) await el.screenshot({ path: path.join(shotDir, `${name}.png`) });
  };
  const size = (w, h, pinned, order) => page.evaluate(({ w, h, pinned, order }) => {
    const pane = document.getElementById('pane');
    pane.style.width = w + 'px';
    pane.style.height = h + 'px';
    window.__setLayout({ pinned, order });
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    if (inner) inner.scrollTop = 0;
    if (scroll) scroll.scrollTop = 0;
  }, { w, h, pinned, order });
  const seedLayout = (pinned, order) => page.evaluate(({ pinned, order }) => {
    window.__seedLayout({ pinned, order });
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    if (inner) inner.scrollTop = 0;
    if (scroll) scroll.scrollTop = 0;
  }, { pinned, order });
  const state = () => page.evaluate(() => {
    const scroll = document.querySelector('[data-quota-scroll]');
    const shelf = document.querySelector('[data-pinned-shelf-scroll]');
    return {
      stored: window.__store.get('cardLayout') || { pinned: [], order: [] },
      list: scroll ? scroll.scrollTop : null,
      shelf: shelf ? shelf.scrollTop : null,
      drop: [...document.querySelectorAll('[data-card-drop]')].map((n) => [n.getAttribute('data-card-shell'), n.getAttribute('data-card-drop')]),
      ghost: Boolean(document.querySelector('[data-card-ghost]')),
      pinnedUI: [...document.querySelectorAll('[data-pinned-shelf] [data-card-shell]')].map((n) => n.getAttribute('data-card-shell')),
      listUI: [...document.querySelectorAll('[data-quota-scroll] [data-card-shell]')].filter((n) => !n.closest('[data-pinned-shelf]')).map((n) => n.getAttribute('data-card-shell')),
    };
  });
  const box = async (selector) => page.locator(selector).first().boundingBox();
  const mouseDrag = async (from, target, opts = {}) => {
    const rect = await page.evaluate(({ selector }) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      el.scrollIntoView({ block: 'nearest' });
      const b = el.getBoundingClientRect();
      return { x: b.x, y: b.y, width: b.width, height: b.height };
    }, { selector: from });
    if (!rect) throw new Error(`drag source not found: ${from}`);
    await page.mouse.move(rect.x + rect.width * (opts.xFrac ?? 0.3), rect.y + (opts.yOffset ?? 14));
    await page.mouse.down();
    await page.mouse.move(rect.x + rect.width * (opts.xFrac ?? 0.3) + 8, rect.y + (opts.yOffset ?? 14) + 8, { steps: 4 });
    await page.waitForTimeout(80);
    let last = null;
    if (target) {
      last = await target();
      await page.waitForTimeout(opts.wait ?? 200);
    }
    if (opts.hold) {
      await page.waitForTimeout(opts.hold);
    }
    return { rect, last };
  };

  const order = ALL.slice(1);
  for (const [w, h] of [[300, 540], [230, 540], [300, 300]]) {
    await size(w, h, ['anthropic'], order);
    await page.waitForTimeout(140);
    const g = await page.evaluate(() => {
      const shelf = document.querySelector('[data-pinned-shelf]');
      const scroll = document.querySelector('[data-quota-scroll]');
      const pinned = [...shelf.querySelectorAll('[data-provider]')];
      const unpinned = [...scroll.querySelectorAll('[data-provider]')].filter((n) => !shelf.contains(n));
      const r = (n) => { const b = n.getBoundingClientRect(); return { x: b.x, right: b.right, width: b.width, top: b.top, bottom: b.bottom }; };
      return {
        shelf: r(shelf), pinned: pinned.map(r), unpinned: unpinned.map(r),
        shelfBg: getComputedStyle(shelf).backgroundColor,
        bodyBg: getComputedStyle(document.body).backgroundColor,
        border: getComputedStyle(shelf).borderBottomWidth,
      };
    });
    check(`aligned-${w}x${h}`, Math.abs(g.pinned[0].x - g.unpinned[0].x) < 0.6
      && Math.abs(g.pinned[0].right - g.unpinned[0].right) < 0.6
      && Math.abs(g.pinned[0].width - g.unpinned[0].width) < 0.6, g);
    check(`no-container-${w}x${h}`, g.border === '0px' && g.shelfBg === g.bodyBg, { border: g.border, shelf: g.shelfBg, body: g.bodyBg });
  }

  // Edit mode: one pin control, and no native draggable source anymore — the
  // whole shell starts a pointer drag, controls stay click targets.
  await size(300, 540, [], ALL);
  await page.waitForTimeout(140);
  await page.click('[data-card-edit]');
  await page.waitForSelector('[data-card-shell]');
  const edit = await page.evaluate(() => {
    const card = document.querySelector('[data-card-shell] [data-provider]');
    const shell = card.closest('[data-card-shell]');
    const handle = shell.querySelector('[data-card-drag-handle]');
    return {
      shellDraggable: shell.draggable,
      handleDraggable: handle.draggable,
      pinButtons: card.querySelectorAll('[data-card-control="pin"]').length,
      badges: card.querySelectorAll('[data-card-pinned]').length,
      pinDraggable: card.querySelector('[data-card-control="pin"]').draggable,
      cursor: shell.style.cursor,
      userSelect: shell.style.userSelect,
    };
  });
  check('pointer-drag-card', edit.shellDraggable === false && edit.handleDraggable === false
    && edit.cursor === 'grab' && edit.userSelect === 'none', edit);
  check('one-pin-affordance', edit.pinButtons === 1 && edit.badges === 0, edit);

  // An arrow that cannot move further stays focusable (aria-disabled, not
  // disabled), so a keyboard reorder does not lose its place at a band edge.
  // Clicking it must be a true no-op: with no stored order yet, it must not
  // materialise one or enable Reset.
  await page.evaluate(() => window.__setLayout({ pinned: [], order: [] }));
  await page.waitForTimeout(180);
  const edgeFocus = await page.evaluate(async () => {
    const button = document.querySelector('[data-card-shell="anthropic"] [data-card-control="up"]');
    button.focus();
    button.click();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const active = document.activeElement;
    const reset = document.querySelector('[data-card-reset]');
    return {
      control: active && active.getAttribute('data-card-control'),
      id: active && active.getAttribute('data-card-id'),
      aria: active && active.getAttribute('aria-disabled'),
      disabled: active && active.disabled,
      stored: window.__store.get('cardLayout') || null,
      resetDisabled: reset ? reset.disabled : null,
    };
  });
  check('edge-arrow-keeps-focus', edgeFocus.control === 'up' && edgeFocus.id === 'anthropic'
    && edgeFocus.aria === 'true' && edgeFocus.disabled === false, edgeFocus);
  check('edge-arrow-writes-nothing', edgeFocus.stored === null && edgeFocus.resetDisabled === true, edgeFocus);

  // Drag the card body (not the grip) onto the lower half of another card.
  await seedLayout([], ALL);
  await page.waitForTimeout(220);
  const bodyDrag = await mouseDrag('[data-card-shell="anthropic"]', async () => {
    const target = await box('[data-card-shell="deepseek"]');
    const list = await page.evaluate(() => {
      const b = document.querySelector('[data-quota-scroll]').getBoundingClientRect();
      return { bottom: b.bottom };
    });
    const y = Math.min(target.y + target.height - 22, list.bottom - 42);
    await page.mouse.move(target.x + target.width / 2, y, { steps: 12 });
    const preview = await state();
    const sourceOpacity = await page.evaluate(() => {
      const shell = document.querySelector('[data-card-shell="anthropic"]');
      return shell ? shell.style.opacity : null;
    });
    return { preview, sourceOpacity };
  });
  check('body-drag-preview', bodyDrag.last.preview.drop.some(([id, edge]) => id === 'deepseek' && edge === 'after')
    && bodyDrag.last.preview.ghost === true && Number(bodyDrag.last.sourceOpacity) < 0.8, bodyDrag.last);
  await page.mouse.up();
  await page.waitForTimeout(250);
  const bodyDrop = await state();
  check('body-drag-persists', bodyDrop.stored.order.slice(0, 2).join(',') === 'deepseek,anthropic'
    && !bodyDrop.ghost, bodyDrop.stored);

  // The grip is still a drag affordance: dragging it starts the same pointer drag.
  await page.waitForTimeout(150);
  await mouseDrag('[data-card-shell="deepseek"] [data-card-drag-handle]', async () => {
    const target = await box('[data-card-shell="anthropic"]');
    await page.mouse.move(target.x + target.width / 2, target.y + target.height - 24, { steps: 10 });
    await page.waitForTimeout(120);
    return state();
  }, { yOffset: 10 });
  await page.mouse.up();
  await page.waitForTimeout(250);
  const gripDrop = await state();
  check('grip-drag-still-works', gripDrop.stored.order.slice(0, 2).join(',') === 'anthropic,deepseek', gripDrop.stored);

  // Releasing in the gap between cards inserts there, and releasing in the
  // tail below the last card appends — neither is a dead zone.
  await page.evaluate(() => { document.getElementById('pane').style.height = '700px'; });
  await seedLayout([], ALL);
  await page.waitForTimeout(250);
  const gapPoint = await page.evaluate(() => {
    const a = document.querySelector('[data-card-shell="deepseek"]').getBoundingClientRect();
    const b = document.querySelector('[data-card-shell="openai"]').getBoundingClientRect();
    return { gap: b.top - a.bottom, x: a.x + a.width / 2, y: (a.bottom + b.top) / 2 };
  });
  check('inter-card-gap-exists', gapPoint.gap > 1, gapPoint);
  await mouseDrag('[data-card-shell="anthropic"]', async () => {
    await page.mouse.move(gapPoint.x, gapPoint.y, { steps: 10 });
    await page.waitForTimeout(180);
    return state();
  });
  const gapPreview = await state();
  check('gap-drop-preview', gapPreview.drop.some(([id, edge]) => id === 'openai' && edge === 'before'), gapPreview);
  await page.mouse.up();
  await page.waitForTimeout(250);
  const gapDrop = await state();
  check('gap-drop-persists', gapDrop.stored.order.slice(0, 3).join(',') === 'deepseek,anthropic,openai', gapDrop.stored);

  const tailPoint = await page.evaluate(() => {
    const scroll = document.querySelector('[data-quota-scroll]');
    scroll.scrollTop = scroll.scrollHeight;
    const r = scroll.getBoundingClientRect();
    const shells = [...scroll.querySelectorAll('[data-card-shell]')].filter((n) => !n.closest('[data-pinned-shelf]'));
    const last = shells[shells.length - 1];
    const lastRect = last.getBoundingClientRect();
    const source = shells[shells.length - 3] || shells[shells.length - 2];
    const sourceRect = source.getBoundingClientRect();
    return {
      lastId: last.getAttribute('data-card-shell'),
      sourceId: source.getAttribute('data-card-shell'),
      sourceX: sourceRect.x + sourceRect.width * 0.3,
      sourceY: sourceRect.y + 14,
      x: r.x + r.width / 2,
      y: lastRect.bottom + 4,
      inside: lastRect.bottom + 4 <= r.bottom,
    };
  });
  check('tail-drop-space', tailPoint.inside && tailPoint.lastId !== tailPoint.sourceId, tailPoint);
  await page.mouse.move(tailPoint.sourceX, tailPoint.sourceY);
  await page.mouse.down();
  await page.mouse.move(tailPoint.sourceX + 8, tailPoint.sourceY + 8, { steps: 4 });
  await page.waitForTimeout(80);
  await page.mouse.move(tailPoint.x, tailPoint.y, { steps: 12 });
  await page.waitForTimeout(180);
  const tailPreview = await state();
  check('tail-drop-preview', tailPreview.drop.some(([id, edge]) => id === tailPoint.lastId && edge === 'after'), tailPreview);
  await page.mouse.up();
  await page.waitForTimeout(250);
  const tailDrop = await state();
  check('tail-drop-appends', tailDrop.stored.order[tailDrop.stored.order.length - 1] === tailPoint.sourceId, tailDrop.stored);
  await page.evaluate(() => { document.getElementById('pane').style.height = '540px'; });
  await page.waitForTimeout(150);

  // Controls never become a drag source: a click and a wobbly click both work.
  await seedLayout([], ALL);
  await page.waitForTimeout(220);
  const pinBox = await box('[data-card-shell="deepseek"] [data-card-control="pin"]');
  await page.mouse.click(pinBox.x + pinBox.width / 2, pinBox.y + pinBox.height / 2);
  await page.waitForTimeout(220);
  const pinned = await state();
  check('pin-click', pinned.stored.pinned.includes('deepseek') && pinned.pinnedUI.includes('deepseek'), pinned.stored);
  const wobbleBox = await box('[data-card-shell="deepseek"] [data-card-control="pin"]');
  await page.mouse.move(wobbleBox.x + wobbleBox.width / 2, wobbleBox.y + wobbleBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(wobbleBox.x + wobbleBox.width / 2 + 4, wobbleBox.y + wobbleBox.height / 2 + 4, { steps: 3 });
  await page.mouse.up();
  await page.waitForTimeout(220);
  const unpinned = await state();
  check('pin-wobble-click', !unpinned.stored.pinned.includes('deepseek')
    && unpinned.stored.order.length === 0
    && unpinned.listUI.join(',') === ALL.join(','), unpinned);

  const accountButtons = page.locator('[data-card-shell="anthropic"] button', { hasText: 'Work' });
  const accountBox = await accountButtons.first().boundingBox();
  await page.mouse.click(accountBox.x + accountBox.width / 2, accountBox.y + accountBox.height / 2);
  await page.waitForTimeout(220);
  const accountSelected = await page.evaluate(() => {
    const n = [...document.querySelectorAll('[data-card-shell="anthropic"] button')].find((b) => /Work/.test(b.textContent || ''));
    return n ? n.getAttribute('aria-pressed') : null;
  });
  const accountState = await state();
  check('account-click', accountSelected === 'true'
    && accountState.stored.order.length === 0
    && accountState.listUI.join(',') === ALL.join(','),
  { accountSelected, stored: accountState.stored, list: accountState.listUI });
  const workAfter = await page.locator('[data-card-shell="anthropic"] button', { hasText: 'Work' }).first().boundingBox();
  await page.mouse.move(workAfter.x + workAfter.width / 2, workAfter.y + workAfter.height / 2);
  await page.mouse.down();
  await page.mouse.move(workAfter.x + workAfter.width / 2 + 4, workAfter.y + workAfter.height / 2 + 4, { steps: 3 });
  await page.mouse.up();
  await page.waitForTimeout(200);
  const accountAfterWobble = await page.evaluate(() => {
    const n = [...document.querySelectorAll('[data-card-shell="anthropic"] button')].find((b) => /Work/.test(b.textContent || ''));
    return n ? n.getAttribute('aria-pressed') : null;
  });
  const accountWobbleState = await state();
  check('account-wobble-click', accountAfterWobble === 'true'
    && accountWobbleState.stored.order.length === 0
    && accountWobbleState.listUI.join(',') === ALL.join(','),
  { accountAfterWobble, stored: accountWobbleState.stored, list: accountWobbleState.listUI });

  // Moving the last available step with the keyboard keeps focus on the same
  // arrow even though it becomes inert at the band edge.
  await seedLayout(['deepseek', 'openai', 'gemini', 'cursor', 'openrouter'], ['anthropic', 'ollama']);
  await page.waitForTimeout(250);
  const arrowMove = await page.evaluate(async () => {
    const button = document.querySelector('[data-card-shell="anthropic"] [data-card-control="down"]');
    button.focus();
    button.click();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await new Promise((resolve) => setTimeout(resolve, 120));
    const active = document.activeElement;
    return {
      order: (window.__store.get('cardLayout') || {}).order,
      control: active && active.getAttribute('data-card-control'),
      id: active && active.getAttribute('data-card-id'),
      aria: active && active.getAttribute('aria-disabled'),
    };
  });
  check('arrow-move-keeps-focus', arrowMove.order.join(',') === 'ollama,anthropic'
    && arrowMove.control === 'down' && arrowMove.id === 'anthropic' && arrowMove.aria === 'true', arrowMove);

  // Autoscroll the unpinned list: hold a dragged card at the bottom edge,
  // then drop onto a card that was outside the viewport when the drag began.
  await seedLayout([], ALL);
  await page.waitForTimeout(220);
  const auto = await mouseDrag('[data-card-shell="anthropic"]', async () => {
    const list = await page.evaluate(() => {
      const b = document.querySelector('[data-quota-scroll]').getBoundingClientRect();
      return { x: b.x + b.width / 2, bottom: b.bottom };
    });
    await page.mouse.move(list.x, list.bottom - 10, { steps: 12 });
    await page.waitForTimeout(120);
    const before = await state();
    return { list, before };
  }, { hold: 600 });
  const autoHeld = await state();
  check('unpinned-autoscroll', autoHeld.list > (auto.last.before.list + 120), { before: auto.last.before.list, after: autoHeld.list });
  const topTarget = await page.evaluate(() => {
    const s = document.querySelector('[data-quota-scroll]');
    const r = s.getBoundingClientRect();
    const shells = [...s.querySelectorAll('[data-card-shell]')].filter((n) => {
      const b = n.getBoundingClientRect();
      return b.top > r.top + 40 && b.bottom < r.bottom - 40;
    });
    const first = shells[0];
    if (!first) return null;
    const b = first.getBoundingClientRect();
    return { id: first.getAttribute('data-card-shell'), x: b.x + b.width / 2, y: b.y + b.height / 2 };
  });
  check('autoscroll-revealed-target', Boolean(topTarget) && topTarget.id !== 'anthropic', topTarget);
  await page.mouse.move(topTarget.x, topTarget.y, { steps: 8 });
  await page.waitForTimeout(180);
  const beforeDrop = await state();
  check('autoscroll-drop-preview', beforeDrop.drop.some(([id]) => id === topTarget.id), beforeDrop);
  await page.mouse.up();
  await page.waitForTimeout(250);
  const autoDrop = await state();
  check('autoscroll-drop-persists', autoDrop.stored.order.indexOf('anthropic') > 0
    && autoDrop.stored.order[0] !== 'anthropic', autoDrop.stored);

  // Wheel input during a pointer drag scrolls the dragged card's band.
  await seedLayout([], ALL);
  await page.waitForTimeout(220);
  await mouseDrag('[data-card-shell="anthropic"]', async () => {
    await page.mouse.move(150, 160, { steps: 6 });
    await page.waitForTimeout(80);
    return state();
  });
  const wheelBefore = await state();
  await page.mouse.wheel(0, 260);
  await page.waitForTimeout(220);
  const wheelAfter = await state();
  check('wheel-during-drag', wheelAfter.list > wheelBefore.list + 180, { before: wheelBefore.list, after: wheelAfter.list });
  await page.mouse.move(10, 40);
  await page.mouse.up();
  await page.waitForTimeout(200);

  // Ctrl/⌘+wheel is zoom, not a card-scroll shortcut: leave it untouched.
  await seedLayout([], ALL);
  await page.waitForTimeout(220);
  await mouseDrag('[data-card-shell="anthropic"]', async () => {
    await page.mouse.move(150, 160, { steps: 6 });
    await page.waitForTimeout(80);
    return state();
  });
  // The page must receive ctrl+wheel as a zoom gesture: our drag handler must
  // leave defaultPrevented false for it (and still prevent the plain wheel).
  await page.evaluate(() => {
    window.__wheelProbe = [];
    window.addEventListener('wheel', (event) => {
      window.__wheelProbe.push({ ctrl: event.ctrlKey, prevented: event.defaultPrevented });
    }, { passive: true });
  });
  // Playwright's keyboard state does not ride on mouse.wheel; CDP carries the
  // Ctrl modifier bit so the page really receives a ctrl+wheel event.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel', x: 150, y: 160, deltaX: 0, deltaY: 260,
    modifiers: 2, pointerType: 'mouse',
  });
  await page.waitForTimeout(200);
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel', x: 150, y: 160, deltaX: 0, deltaY: 120,
    modifiers: 0, pointerType: 'mouse',
  });
  await page.waitForTimeout(200);
  const wheelProbe = await page.evaluate(() => window.__wheelProbe);
  check('ctrl-wheel-not-intercepted',
    wheelProbe.some((e) => e.ctrl && e.prevented === false)
    && wheelProbe.some((e) => !e.ctrl && e.prevented === true), wheelProbe);
  await page.mouse.move(10, 40);
  await page.mouse.up();
  await page.waitForTimeout(200);

  // Pinned shelf: a pinned drag autoscrolls only the shelf, then drops there.
  await page.evaluate(() => { document.getElementById('pane').style.height = '420px'; });
  await seedLayout(['anthropic', 'deepseek', 'openai', 'gemini'], ['cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(350);
  const shelfBox = await page.evaluate(() => {
    const s = document.querySelector('[data-pinned-shelf-scroll]');
    const r = s.getBoundingClientRect();
    s.scrollTop = 0;
    document.querySelector('[data-quota-scroll]').scrollTop = 0;
    return { x: r.x + r.width / 2, bottom: r.bottom, overflow: s.scrollHeight - s.clientHeight };
  });
  check('shelf-overflows', shelfBox.overflow > 80, shelfBox);
  await mouseDrag('[data-pinned-shelf] [data-card-shell="anthropic"]', async () => {
    await page.mouse.move(shelfBox.x, shelfBox.bottom - 10, { steps: 10 });
    await page.waitForTimeout(120);
    return state();
  }, { hold: 600 });
  const shelfHeld = await state();
  check('shelf-autoscroll-only', shelfHeld.shelf > 80 && shelfHeld.list === 0, shelfHeld);
  const shelfTarget = await page.evaluate(() => {
    const s = document.querySelector('[data-pinned-shelf-scroll]');
    const r = s.getBoundingClientRect();
    const el = document.elementFromPoint(r.x + r.width / 2, r.bottom - 30);
    const shell = el && el.closest ? el.closest('[data-card-shell]') : null;
    if (!shell || !shell.closest('[data-pinned-shelf]')) return null;
    const b = shell.getBoundingClientRect();
    return { id: shell.getAttribute('data-card-shell'), x: b.x + b.width / 2, y: Math.min(b.bottom - 12, r.bottom - 20) };
  });
  check('shelf-target-visible', Boolean(shelfTarget) && shelfTarget.id !== 'anthropic', shelfTarget);
  await page.mouse.move(shelfTarget.x, shelfTarget.y, { steps: 8 });
  await page.waitForTimeout(180);
  const shelfPreview = await state();
  check('shelf-drop-preview', shelfPreview.drop.some(([id]) => id === shelfTarget.id), shelfPreview);
  await page.mouse.up();
  await page.waitForTimeout(250);
  const shelfDrop = await state();
  check('shelf-drop-persists',
    shelfDrop.stored.pinned.length === 4
    && new Set(shelfDrop.stored.pinned).size === 4
    && shelfDrop.stored.pinned.join(',') !== 'anthropic,deepseek,openai,gemini'
    && shelfDrop.stored.order.join(',') === 'cursor,openrouter,ollama',
    shelfDrop.stored);

  // A drop is refused across the pin boundary; nothing changes.
  await page.evaluate(() => { document.getElementById('pane').style.height = '540px'; });
  await seedLayout(['anthropic'], ['deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(320);
  await mouseDrag('[data-pinned-shelf] [data-card-shell="anthropic"]', async () => {
    const target = await box('[data-card-shell="deepseek"]');
    await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 10 });
    await page.waitForTimeout(150);
    return state();
  });
  const crossMid = await state();
  check('cross-band-no-preview', crossMid.drop.length === 0, crossMid);
  await page.mouse.up();
  await page.waitForTimeout(220);
  const crossAfter = await state();
  check('cross-band-refused', crossAfter.stored.pinned.join(',') === 'anthropic'
    && crossAfter.stored.order.join(',') === 'deepseek,openai,gemini,cursor,openrouter,ollama', crossAfter.stored);

  // Wheel in the inter-card gap and the shelf padding must scroll the shelf
  // while it overflows, never the unpinned list.
  await size(300, 700, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(200);
  const points = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const shelf = document.querySelector('[data-pinned-shelf]');
    const cards = [...inner.querySelectorAll('[data-provider]')];
    const a = cards[0].getBoundingClientRect();
    const b = cards[1].getBoundingClientRect();
    const i = inner.getBoundingClientRect();
    const s = shelf.getBoundingClientRect();
    return {
      x: i.x + i.width / 2,
      gapY: (a.bottom + b.top) / 2,
      paddingY: Math.min(s.bottom - 3, i.bottom + 3),
      innerBottom: i.bottom,
      hit: (() => { const el = document.elementFromPoint(i.x + i.width / 2, Math.min(s.bottom - 3, i.bottom + 3)); return el ? (el.closest('[data-pinned-shelf-scroll]') ? 'scroll' : 'shelf') : null; })(),
    };
  });
  const readScroll = () => page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    return { inner: inner.scrollTop, outer: scroll.scrollTop };
  });
  await page.mouse.move(points.x, points.gapY);
  await page.mouse.wheel(0, 120);
  await page.waitForTimeout(180);
  const gapWheel = await readScroll();
  check('wheel-gap', gapWheel.inner > 0 && gapWheel.outer === 0, gapWheel);
  await page.evaluate(() => {
    document.querySelector('[data-pinned-shelf-scroll]').scrollTop = 0;
    document.querySelector('[data-quota-scroll]').scrollTop = 0;
  });
  await page.waitForTimeout(80);
  await page.mouse.move(points.x, points.paddingY);
  await page.mouse.wheel(0, 120);
  await page.waitForTimeout(180);
  const padWheel = await readScroll();
  check('wheel-padding', points.hit === 'shelf' && padWheel.inner > 0 && padWheel.outer === 0, { points, padWheel });

  // The fix must not capture ordinary list scrolling.
  const listPoint = await page.evaluate(() => {
    const shelf = document.querySelector('[data-pinned-shelf]');
    const scroll = document.querySelector('[data-quota-scroll]');
    scroll.scrollTop = 0;
    const card = [...scroll.querySelectorAll('[data-provider]')].find((n) => !shelf.contains(n));
    const b = card.getBoundingClientRect();
    return { x: b.x + b.width / 2, y: b.y + 12 };
  });
  await page.mouse.move(listPoint.x, listPoint.y);
  await page.mouse.wheel(0, 140);
  await page.waitForTimeout(180);
  const listWheel = await readScroll();
  check('wheel-list-still-scrolls', listWheel.outer > 0, listWheel);

  // Containment at both boundaries: a wheel inside the pinned band never
  // chains to the unpinned list, even when its own scroller cannot move.
  const boundaryPoint = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    inner.scrollTop = 0;
    scroll.scrollTop = 0;
    window.__boundaryWheel = [];
    window.addEventListener('wheel', (event) => {
      window.__boundaryWheel.push({
        prevented: event.defaultPrevented,
        inShelf: Boolean(event.target && event.target.closest && event.target.closest('[data-pinned-shelf]')),
      });
    }, { passive: true });
    const card = inner.querySelector('[data-provider]');
    const b = card.getBoundingClientRect();
    const i = inner.getBoundingClientRect();
    return { x: i.x + i.width / 2, y: Math.min(b.top + 18, i.bottom - 12) };
  });
  await page.mouse.move(boundaryPoint.x, boundaryPoint.y);
  await page.mouse.wheel(0, -180);
  await page.waitForTimeout(200);
  const topBoundary = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    return {
      inner: inner.scrollTop,
      outer: scroll.scrollTop,
      wheel: window.__boundaryWheel.slice(-1)[0],
    };
  });
  check('wheel-top-contained', topBoundary.inner === 0 && topBoundary.outer === 0
    && topBoundary.wheel && topBoundary.wheel.prevented === true
    && topBoundary.wheel.inShelf === true, topBoundary);

  await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    inner.scrollTop = inner.scrollHeight;
    document.querySelector('[data-quota-scroll]').scrollTop = 0;
    window.__boundaryWheel = [];
  });
  await page.waitForTimeout(120);
  await page.mouse.move(boundaryPoint.x, boundaryPoint.y);
  await page.mouse.wheel(0, 180);
  await page.waitForTimeout(200);
  const bottomBoundary = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    return {
      inner: inner.scrollTop,
      max: inner.scrollHeight - inner.clientHeight,
      outer: scroll.scrollTop,
      wheel: window.__boundaryWheel.slice(-1)[0],
    };
  });
  check('wheel-bottom-contained', bottomBoundary.max > 0
    && bottomBoundary.inner === bottomBoundary.max
    && bottomBoundary.outer === 0
    && bottomBoundary.wheel && bottomBoundary.wheel.prevented === true
    && bottomBoundary.wheel.inShelf === true, bottomBoundary);

  // Containment also holds when the shelf has nothing to scroll: a wheel over
  // the band is still the band's gesture and must not fall through to the list.
  await size(300, 900, ['anthropic'], ['deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const noOverflowPoint = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    inner.scrollTop = 0;
    scroll.scrollTop = 0;
    window.__noOverflowWheel = [];
    window.addEventListener('wheel', (event) => {
      window.__noOverflowWheel.push({
        prevented: event.defaultPrevented,
        inShelf: Boolean(event.target && event.target.closest && event.target.closest('[data-pinned-shelf]')),
      });
    }, { passive: true });
    const i = inner.getBoundingClientRect();
    return {
      x: i.x + i.width / 2,
      y: i.y + i.height / 2,
      innerMax: inner.scrollHeight - inner.clientHeight,
    };
  });
  await page.mouse.move(noOverflowPoint.x, noOverflowPoint.y);
  await page.mouse.wheel(0, 200);
  await page.waitForTimeout(220);
  const noOverflowWheel = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const scroll = document.querySelector('[data-quota-scroll]');
    return {
      inner: inner.scrollTop,
      outer: scroll.scrollTop,
      wheel: window.__noOverflowWheel.slice(-1)[0],
    };
  });
  check('wheel-contained-without-overflow', noOverflowPoint.innerMax <= 1
    && noOverflowWheel.inner === 0
    && noOverflowWheel.outer === 0
    && noOverflowWheel.wheel && noOverflowWheel.wheel.prevented === true
    && noOverflowWheel.wheel.inShelf === true, { noOverflowPoint, noOverflowWheel });

  // ---- resizable pinned shelf: geometry, pointer, keyboard, touch ----
  await size(300, 540, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(320);
  const shelfState = () => page.evaluate(() => {
    const pane = document.getElementById('pane');
    const shelf = document.querySelector('[data-pinned-shelf]');
    const frame = document.querySelector('[data-pinned-shelf-frame]');
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const handle = document.querySelector('[data-pinned-shelf-resize]');
    const grip = document.querySelector('[data-pinned-shelf-grip]');
    const scroll = document.querySelector('[data-quota-scroll]');
    const region = document.querySelector('[data-quota-body]');
    const r = (el) => {
      const b = el.getBoundingClientRect();
      return { top: b.top, bottom: b.bottom, height: b.height, left: b.left, right: b.right, x: b.x + b.width / 2 };
    };
    const cards = inner ? [...inner.children].map((el) => {
      const b = el.getBoundingClientRect();
      const i = inner.getBoundingClientRect();
      return {
        id: el.getAttribute('data-card-shell') || el.getAttribute('data-provider'),
        height: b.height,
        top: b.top,
        topContent: b.top - i.top + inner.scrollTop,
        midContent: (b.top + b.bottom) / 2 - i.top + inner.scrollTop,
        bottomViewport: b.bottom,
        bottom: b.bottom - i.top + inner.scrollTop,
      };
    }) : [];
    return {
      paneHeight: pane.clientHeight,
      // The stable card region that holds both bands, not the shrinking
      // unpinned list: the 50%/72px budgets are expressed against it.
      viewportHeight: region ? region.clientHeight : 0,
      storedHeight: window.__store.get('pinnedShelfHeight') ?? null,
      shelf: shelf ? r(shelf) : null,
      frame: frame ? { ...r(frame), styleHeight: frame.style.height, styleMax: frame.style.maxHeight } : null,
      inner: inner ? { ...r(inner), scrollTop: inner.scrollTop, scrollHeight: inner.scrollHeight, clientHeight: inner.clientHeight } : null,
      handle: handle ? {
        ...r(handle),
        min: Number(handle.getAttribute('aria-valuemin')),
        max: Number(handle.getAttribute('aria-valuemax')),
        now: Number(handle.getAttribute('aria-valuenow')),
        blocked: handle.getAttribute('aria-disabled') === 'true',
        resizing: handle.getAttribute('data-resizing') === 'true',
        label: handle.getAttribute('aria-label'),
        hint: handle.getAttribute('aria-description'),
        title: handle.getAttribute('title'),
        role: handle.getAttribute('role'),
        tabIndex: handle.tabIndex,
        cursor: handle.style.cursor,
      } : null,
      gripOpacity: grip ? Number(getComputedStyle(grip).opacity) : null,
      gripBox: grip ? r(grip) : null,
      cards,
    };
  });
  // Geometry and computed styles of the two real scrollports. Native
  // scrollbars are not in the DOM, so their width is read from the layout
  // gutter and their CSS state from the ::-webkit-scrollbar pseudo elements.
  const scrollbarState = () => page.evaluate(() => {
    const body = document.querySelector('[data-quota-body]');
    const shelf = document.querySelector('[data-pinned-shelf]');
    const frame = document.querySelector('[data-pinned-shelf-frame]');
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const list = document.querySelector('[data-quota-scroll]');
    const handle = document.querySelector('[data-pinned-shelf-resize]');
    const grip = document.querySelector('[data-pinned-shelf-grip]');
    const topCue = document.querySelector('[data-pinned-overflow-cue="top"]')
      || document.querySelector('[data-pinned-overflow-rail="top"]');
    const bottomCue = document.querySelector('[data-pinned-overflow-cue="bottom"]')
      || document.querySelector('[data-pinned-overflow-rail="bottom"]');
    const r = (el) => {
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return { top: b.top, bottom: b.bottom, left: b.left, right: b.right, width: b.width, height: b.height };
    };
    const bar = (el) => ({
      ...r(el),
      gutter: el.offsetWidth - el.clientWidth,
      offsetWidth: el.offsetWidth,
      clientWidth: el.clientWidth,
      clientHeight: el.clientHeight,
      scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop,
      maxScroll: el.scrollHeight - el.clientHeight,
      overflowY: getComputedStyle(el).overflowY,
      scrollbarGutter: getComputedStyle(el).scrollbarGutter,
      scrollbarWidth: getComputedStyle(el).scrollbarWidth,
      barWidth: getComputedStyle(el, '::-webkit-scrollbar').width,
      thumbBg: getComputedStyle(el, '::-webkit-scrollbar-thumb').backgroundColor,
      hover: el.matches(':hover'),
    });
    return {
      pane: r(document.getElementById('pane')),
      body: {
        ...r(body),
        overflowY: getComputedStyle(body).overflowY,
        gutter: body.offsetWidth - body.clientWidth,
      },
      shelf: r(shelf),
      frame: r(frame),
      inner: bar(inner),
      list: bar(list),
      handle: r(handle),
      grip: r(grip),
      topCue: r(topCue),
      bottomCue: r(bottomCue),
      contains: {
        shelfInList: list.contains(shelf),
        innerInShelf: shelf.contains(inner),
        listInShelf: shelf.contains(list),
        shelfInBody: body.contains(shelf),
        listInBody: body.contains(list),
      },
      cardWidths: {
        pinned: inner.querySelector('[data-provider]').getBoundingClientRect().width,
        unpinned: list.querySelector('[data-provider]').getBoundingClientRect().width,
      },
      cardLefts: {
        pinned: inner.querySelector('[data-provider]').getBoundingClientRect().left,
        unpinned: list.querySelector('[data-provider]').getBoundingClientRect().left,
      },
      cardCounts: {
        pinned: inner.querySelectorAll('[data-provider]').length,
        unpinned: list.querySelectorAll('[data-provider]').length,
      },
      resizing: handle.getAttribute('data-resizing'),
      storedHeight: window.__store.get('pinnedShelfHeight') ?? null,
    };
  });
  const handleDrag = async (deltaY) => {
    const h = await box('[data-pinned-shelf-resize]');
    const x = h.x + h.width / 2;
    const y = h.y + h.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x, y + deltaY, { steps: 12 });
    await page.waitForTimeout(120);
    const held = await shelfState();
    await page.mouse.up();
    await page.waitForTimeout(220);
    return { held, after: await shelfState() };
  };
  // Set an exact shelf height through the same atom/storage the app uses, so
  // a double-click test can place the band's edge inside a chosen card half.
  const setShelf = async (height) => {
    await page.evaluate((value) => {
      if (value == null) window.__store.delete('pinnedShelfHeight');
      else window.__store.set('pinnedShelfHeight', value);
      window.__setShelfHeight(value);
    }, height);
    await page.waitForTimeout(280);
    return shelfState();
  };
  // A real Chromium double-click on the resize strip. The strip is only 16px
  // tall; its centre is the band's bottom edge, so the click can target a
  // precise point inside the edge's card half.
  const dblclickShelfEdge = async (fraction = 0.5) => {
    const h = await box('[data-pinned-shelf-resize]');
    await page.mouse.dblclick(h.x + h.width / 2, h.y + h.height * fraction);
    await page.waitForTimeout(300);
    return shelfState();
  };

  const initialShelf = await shelfState();
  check('resizer-present', initialShelf.handle && initialShelf.handle.role === 'separator'
    && initialShelf.handle.tabIndex === 0
    && initialShelf.handle.label === 'Resize pinned section'
    && /arrow keys/.test(initialShelf.handle.hint)
    && initialShelf.handle.blocked === false, initialShelf.handle);
  check('resizer-anchored-to-shelf-bottom', initialShelf.handle
    && Math.abs(initialShelf.handle.top - (initialShelf.inner.bottom - 8)) <= 0.5
    && Math.abs(initialShelf.handle.bottom - initialShelf.shelf.bottom) <= 0.5
    && initialShelf.handle.height === 16
    && initialShelf.handle.cursor === 'ns-resize', {
      handle: initialShelf.handle, inner: initialShelf.inner, shelf: initialShelf.shelf,
    });
  const firstCard = initialShelf.cards[0];
  const lastCard = initialShelf.cards[initialShelf.cards.length - 1];
  check('resizer-range-from-cards', initialShelf.handle
    // The floor is the first card's bottom plus the 8px of the 16px strip
    // that intrudes into the scrollport (16px strip - 8px shelf padding).
    && Math.abs(initialShelf.handle.min - Math.ceil(firstCard.bottom + 8)) <= 1
    && initialShelf.handle.max <= lastCard.bottom + 8.5
    && initialShelf.handle.max <= Math.floor(initialShelf.paneHeight / 2) - 12 + 0.5
    && initialShelf.handle.max <= Math.floor(initialShelf.viewportHeight) - 12 - 72 + 0.5
    && initialShelf.handle.now === initialShelf.handle.max
    && Math.abs(initialShelf.frame.height - initialShelf.handle.now) <= 0.5, {
      handle: initialShelf.handle, firstCard, lastCard, paneHeight: initialShelf.paneHeight, viewportHeight: initialShelf.viewportHeight,
    });
  check('resizer-default-fits-half-window', initialShelf.shelf.height <= initialShelf.paneHeight / 2 + 0.5
    && initialShelf.shelf.height <= initialShelf.viewportHeight + 0.5
    && initialShelf.frame.styleMax === `${initialShelf.handle.max}px`, {
      shelf: initialShelf.shelf, frame: initialShelf.frame, paneHeight: initialShelf.paneHeight,
    });

  // The grip is invisible at rest and appears on hover without changing
  // layout: the shelf is still unframed, and the handle is only an overlay.
  await page.mouse.move(5, 5);
  await page.waitForTimeout(180);
  const gripRest = await shelfState();
  await shot('shelf-rest');
  await page.mouse.move(initialShelf.inner.x, initialShelf.inner.top + 24);
  await page.waitForTimeout(180);
  const gripHover = await shelfState();
  check('resize-grip-on-hover', gripRest.gripOpacity === 0 && gripHover.gripOpacity === 1
    && gripHover.shelf.height === gripRest.shelf.height, {
      rest: gripRest.gripOpacity, hover: gripHover.gripOpacity,
      restHeight: gripRest.shelf.height, hoverHeight: gripHover.shelf.height,
    });
  await shot('shelf-hover');

  // Drag up to the first-card floor. The shelf contracts, the lower cue
  // appears, and no card-drag ghost or order write happens.
  const layoutBeforeResize = await page.evaluate(() => JSON.stringify(window.__store.get('cardLayout') || null));
  const toMin = await handleDrag(-240);
  const floor = toMin.after;
  check('resize-drag-min', !toMin.after.handle.blocked
    && toMin.after.frame.height === toMin.after.handle.min
    && toMin.after.shelf.height <= toMin.after.paneHeight / 2 + 0.5
    && toMin.after.inner.scrollHeight > toMin.after.inner.clientHeight, { held: toMin.held.frame.height, floor: {
      frame: floor.frame.height, min: floor.handle.min, shelf: floor.shelf.height,
    } });
  // The floor must leave the first pinned card entirely inside the
  // scrollport, with the resize strip starting at (not across) its bottom
  // border. Measured from real viewport boxes, not the JS range alone.
  const minCard = floor.cards[0];
  check('resize-min-first-card-fully-visible', !floor.handle.blocked
    && minCard.top >= floor.inner.top - 0.5
    && minCard.bottomViewport <= floor.inner.bottom + 0.5
    && floor.frame.height === floor.handle.min, {
      card: minCard, inner: floor.inner, frame: floor.frame.height, min: floor.handle.min,
    });
  check('resize-min-handle-below-first-card', floor.handle
    && floor.handle.top >= minCard.bottomViewport - 0.5
    && floor.handle.top - minCard.bottomViewport <= 1.5
    && floor.handle.height === 16, {
      cardBottom: minCard.bottomViewport, handleTop: floor.handle.top,
      gap: floor.handle.top - minCard.bottomViewport, handle: floor.handle,
    });
  const cueAtMin = await page.evaluate(() => ({
    cue: Boolean(document.querySelector('[data-pinned-overflow-cue="bottom"]')),
    opacity: document.querySelector('[data-pinned-overflow-cue="bottom"]')
      ? getComputedStyle(document.querySelector('[data-pinned-overflow-cue="bottom"]')).opacity : null,
    ghost: Boolean(document.querySelector('[data-card-ghost]')),
    storedLayout: window.__store.get('cardLayout') || null,
    storedHeight: window.__store.get('pinnedShelfHeight'),
  }));
  check('resize-min-cue-and-isolation', cueAtMin.cue && cueAtMin.opacity === '1'
    && cueAtMin.ghost === false
    && JSON.stringify(cueAtMin.storedLayout) === layoutBeforeResize
    && cueAtMin.storedHeight === floor.handle.min, { ...cueAtMin, layoutBeforeResize });
  await shot('shelf-min');
  await shotElement('shelf-min-edge', '[data-pinned-shelf]');

  // Drag down past the ceiling: it clamps to half the plugin window, not the
  // full card content, and the preference is written once on pointerup.
  const toMax = await handleDrag(320);
  check('resize-drag-max', toMax.after.frame.height === toMax.after.handle.max
    && toMax.after.handle.max <= Math.floor(toMax.after.paneHeight / 2) - 12 + 0.5
    && toMax.after.shelf.height <= toMax.after.paneHeight / 2 + 0.5
    && toMax.after.frame.height > floor.frame.height, {
      held: toMax.held.frame.height, after: toMax.after.frame.height, max: toMax.after.handle.max,
    });
  const storedAfterMax = await page.evaluate(() => window.__store.get('pinnedShelfHeight'));
  check('resize-ceiling-clears-preference', storedAfterMax == null, { storedAfterMax });
  await shot('shelf-max');

  // Keyboard: arrows step, Home/End go to the measured bounds.
  await page.focus('[data-pinned-shelf-resize]');
  await page.keyboard.press('Home');
  await page.waitForTimeout(160);
  const keyHome = await shelfState();
  await page.keyboard.press('End');
  await page.waitForTimeout(160);
  const keyEnd = await shelfState();
  await page.keyboard.press('ArrowUp');
  await page.waitForTimeout(160);
  const keyUp = await shelfState();
  await page.keyboard.press('ArrowDown');
  await page.waitForTimeout(160);
  const keyDown = await shelfState();
  check('resize-keyboard', keyHome.frame.height === keyHome.handle.min
    && keyEnd.frame.height === keyEnd.handle.max
    && keyUp.frame.height === keyEnd.frame.height - 8
    && keyDown.frame.height === keyEnd.frame.height, {
      home: keyHome.frame.height, end: keyEnd.frame.height,
      up: keyUp.frame.height, down: keyDown.frame.height,
    });

  // A mouse press on the handle must focus it even though the drag handler
  // calls preventDefault to suppress text selection. Without that focus the
  // arrow keys keep going to <body> and a click-then-arrow does nothing.
  const clickHandleBox = await box('[data-pinned-shelf-resize]');
  await page.mouse.click(
    clickHandleBox.x + clickHandleBox.width / 2,
    clickHandleBox.y + clickHandleBox.height / 2,
  );
  await page.waitForTimeout(180);
  const clickFocus = await page.evaluate(() => ({
    handle: Boolean(document.activeElement
      && document.activeElement.hasAttribute('data-pinned-shelf-resize')),
    body: document.activeElement === document.body,
  }));
  await page.keyboard.press('ArrowUp');
  await page.waitForTimeout(180);
  const clickArrow = await shelfState();
  check('resize-click-focuses-handle',
    clickFocus.handle && !clickFocus.body
      && clickArrow.frame.height === clickArrow.handle.max - 8, {
      focus: clickFocus,
      frame: clickArrow.frame.height,
      max: clickArrow.handle.max,
    });
  // Restore the automatic height for the touch path below.
  await page.keyboard.press('End');
  await page.waitForTimeout(180);

  // Touch path: pointer events, no mouse. A synthetic touch-pointer sequence
  // runs through the same handler the real touchscreen uses. The delta stays
  // inside the measured range (which can be narrower than 40px on a short
  // shelf) so this checks the touch path rather than the min clamp, which
  // the pointer-drag test above already covers.
  const touchStart = await page.evaluate(() => {
    const handle = document.querySelector('[data-pinned-shelf-resize]');
    const b = handle.getBoundingClientRect();
    const i = document.querySelector('[data-pinned-shelf-scroll]').getBoundingClientRect();
    const min = Number(handle.getAttribute('aria-valuemin'));
    const max = Number(handle.getAttribute('aria-valuemax'));
    return {
      x: b.x + b.width / 2,
      y: b.y + b.height / 2,
      height: i.height,
      delta: Math.max(4, Math.min(20, Math.floor((max - min) / 2))),
    };
  });
  await page.evaluate(({ x, y, delta }) => {
    const handle = document.querySelector('[data-pinned-shelf-resize]');
    const init = { bubbles: true, cancelable: true, clientX: x, clientY: y, pointerId: 77, pointerType: 'touch', isPrimary: true, button: 0 };
    handle.dispatchEvent(new PointerEvent('pointerdown', init));
    window.dispatchEvent(new PointerEvent('pointermove', { ...init, clientY: y - delta }));
    window.dispatchEvent(new PointerEvent('pointerup', { ...init, clientY: y - delta }));
  }, touchStart);
  await page.waitForTimeout(260);
  const touchAfter = await shelfState();
  check('resize-touch', touchAfter.frame.height === touchAfter.handle.max - touchStart.delta
    && touchAfter.frame.height < touchStart.height, {
      before: touchStart.height, after: touchAfter.frame.height, delta: touchStart.delta,
      min: touchAfter.handle.min, max: touchAfter.handle.max,
    });

  // A cancelled gesture (e.g. the touch turns into a system scroll) must
  // discard the live preview and leave the last committed height in place.
  const cancelBefore = await page.evaluate(() => window.__store.get('pinnedShelfHeight'));
  await page.evaluate(({ x, y }) => {
    const handle = document.querySelector('[data-pinned-shelf-resize]');
    const init = { bubbles: true, cancelable: true, clientX: x, clientY: y, pointerId: 78, pointerType: 'touch', isPrimary: true, button: 0 };
    handle.dispatchEvent(new PointerEvent('pointerdown', init));
    window.dispatchEvent(new PointerEvent('pointermove', { ...init, clientY: y - 80 }));
    window.dispatchEvent(new PointerEvent('pointercancel', { ...init, clientY: y - 80 }));
  }, touchStart);
  await page.waitForTimeout(240);
  const cancelAfter = await shelfState();
  const cancelStored = await page.evaluate(() => window.__store.get('pinnedShelfHeight'));
  check('resize-cancel-reverts-preview', cancelAfter.frame.height === touchAfter.frame.height
    && cancelAfter.frame.height === cancelBefore && cancelStored === cancelBefore,
    { committed: touchAfter.frame.height, storedBefore: cancelBefore, storedAfter: cancelStored, afterCancel: cancelAfter.frame.height, resizing: cancelAfter.handle.resizing });

  // Resizing the pane re-clamps immediately; restoring it brings the saved
  // preference back because the stored value is the user's raw choice.
  const beforeShrink = await shelfState();
  await page.evaluate(() => { document.getElementById('pane').style.height = '300px'; });
  await page.waitForTimeout(320);
  const shrunk = await shelfState();
  check('resize-viewport-clamp', shrunk.frame.height <= Math.floor(shrunk.paneHeight / 2) - 12 + 0.5
    && shrunk.shelf.height <= shrunk.paneHeight / 2 + 0.5
    && shrunk.handle.now === shrunk.frame.height
    && shrunk.viewportHeight > 0, { shrunk: {
      paneHeight: shrunk.paneHeight, viewport: shrunk.viewportHeight,
      frame: shrunk.frame.height, max: shrunk.handle.max,
    }, before: beforeShrink.frame.height });
  await page.evaluate(() => { document.getElementById('pane').style.height = '540px'; });
  await page.waitForTimeout(320);
  const restored = await shelfState();
  check('resize-viewport-restores-preference', restored.frame.height === beforeShrink.frame.height
    && restored.handle.now === beforeShrink.frame.height, {
      before: beforeShrink.frame.height, restored: restored.frame.height,
    });

  // Width changes alter the cards' natural height (text wraps), so the range
  // must be re-measured rather than cached from the previous layout.
  await size(300, 700, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(320);
  const wideShelf = await shelfState();
  await page.evaluate(() => { document.getElementById('pane').style.width = '230px'; });
  await page.waitForTimeout(380);
  const narrowShelf = await shelfState();
  check('resize-width-remeasure', narrowShelf.handle.min > wideShelf.handle.min
    && narrowShelf.handle.max <= Math.floor(narrowShelf.paneHeight / 2) - 12 + 0.5
    && narrowShelf.handle.max <= Math.floor(narrowShelf.viewportHeight) - 12 + 0.5
    && narrowShelf.frame.height === narrowShelf.handle.now, {
      wideMin: wideShelf.handle.min, narrowMin: narrowShelf.handle.min,
      narrowMax: narrowShelf.handle.max, narrowFrame: narrowShelf.frame.height,
    });
  await page.evaluate(() => { document.getElementById('pane').style.width = '300px'; document.getElementById('pane').style.height = '540px'; });
  await page.waitForTimeout(340);

  // Dynamic pins: dropping to one card collapses the range and disables the
  // handle; clearing all pins removes the shelf; re-pinning restores the
  // saved height.
  await page.evaluate(() => window.__setLayout({ pinned: ['anthropic'], order: ['deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama'] }));
  await page.waitForTimeout(280);
  const onePin = await shelfState();
  check('resize-one-pin-inert-fit', onePin.handle.blocked === true
    && onePin.handle.max === onePin.handle.min
    && onePin.frame.height === onePin.handle.max
    && /already fit/i.test(onePin.handle.hint)
    && onePin.handle.title === onePin.handle.hint
    && onePin.gripOpacity === 0.25, {
      handle: onePin.handle, frame: onePin.frame.height, gripOpacity: onePin.gripOpacity,
    });
  // Even with no range (one pin, "already fit") the auto height must leave
  // the card border above the strip rather than under it.
  const onePinCard = onePin.cards[0];
  check('resize-one-pin-handle-clear', onePinCard
    && onePin.handle.top >= onePinCard.bottomViewport - 0.5
    && onePin.handle.top - onePinCard.bottomViewport <= 1.5
    && onePinCard.bottomViewport <= onePin.inner.bottom + 0.5, {
      card: onePinCard, innerBottom: onePin.inner.bottom,
      handleTop: onePin.handle.top, gap: onePin.handle.top - onePinCard.bottomViewport,
    });
  await shotElement('shelf-one-pin', '[data-pinned-shelf]');
  const onePinHandleBox = await box('[data-pinned-shelf-resize]');
  await page.mouse.move(onePinHandleBox.x + onePinHandleBox.width / 2, onePinHandleBox.y + onePinHandleBox.height / 2);
  await page.waitForTimeout(200);
  const onePinHover = await shelfState();
  check('resize-inert-grip-stays-faint', onePinHover.gripOpacity === 0.25
    && onePinHover.handle.blocked === true
    && onePinHover.frame.height === onePin.frame.height, {
      rest: onePin.gripOpacity, hover: onePinHover.gripOpacity,
    });

  // Several pinned cards that all fit under the cap are not a collapsed
  // one-card range: the band opens at the full fit and can still be dragged
  // back to the first card's bottom. "Already fits" is reserved for a single
  // card (or a genuinely collapsed range).
  await size(300, 900, ['anthropic', 'deepseek'],
    ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const multiFit = await shelfState();
  const multiFitExpected = multiFit.storedHeight == null
    ? multiFit.handle.max
    : Math.min(multiFit.handle.max, Math.max(multiFit.handle.min, multiFit.storedHeight));
  check('resize-all-fit-multi-range', multiFit.handle.max > multiFit.handle.min + 20
    && multiFit.handle.blocked === false
    && /Drag the bottom edge/.test(multiFit.handle.hint || '')
    && multiFit.frame.height === multiFitExpected, {
      min: multiFit.handle.min, max: multiFit.handle.max,
      frame: multiFit.frame.height, expected: multiFitExpected,
      stored: multiFit.storedHeight, hint: multiFit.handle.hint,
    });
  await size(300, 540, ['anthropic'],
    ['deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(300);
  await page.evaluate(() => window.__setLayout({ pinned: [], order: ['anthropic', 'deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama'] }));
  await page.waitForTimeout(260);
  const noPins = await page.evaluate(() => ({
    shelf: Boolean(document.querySelector('[data-pinned-shelf]')),
    handle: Boolean(document.querySelector('[data-pinned-shelf-resize]')),
    list: Boolean(document.querySelector('[data-quota-scroll] [data-provider]')),
  }));
  check('resize-no-pins-removes-handle', noPins.shelf === false && noPins.handle === false && noPins.list, noPins);
  await page.evaluate(() => window.__setLayout({ pinned: ['anthropic', 'deepseek', 'openai'], order: ['gemini', 'cursor', 'openrouter', 'ollama'] }));
  await page.waitForTimeout(320);
  const repinned = await shelfState();
  check('resize-repin-restores-height', repinned.frame.height === beforeShrink.frame.height
    && repinned.handle.blocked === false, {
      before: beforeShrink.frame.height, after: repinned.frame.height,
    });

  // All pinned: the same half-window ceiling, no unpinned band, and wheel
  // still belongs to the shelf at its top edge.
  await page.evaluate(() => window.__setLayout({ pinned: ['anthropic', 'deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama'], order: [] }));
  await page.waitForTimeout(320);
  const allPinned = await shelfState();
  check('resize-all-pinned-capped', allPinned.shelf.height <= allPinned.paneHeight / 2 + 0.5
    && allPinned.handle.max <= Math.floor(allPinned.paneHeight / 2) - 12 + 0.5
    && (await page.evaluate(() => [...document.querySelectorAll('[data-quota-scroll] [data-provider]')].filter((n) => !n.closest('[data-pinned-shelf]')).length)) === 0, {
      shelf: allPinned.shelf.height, max: allPinned.handle.max, pane: allPinned.paneHeight,
    });
  await shot('shelf-all-pinned');

  // Back to a mixed, overflowing pane for the cue and sticky-edge checks.
  await size(300, 700, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(300);

  // ---- two real, independent native scrollbars -------------------------
  // The pinned band and the list are separate scrollports now. Each owns a
  // native scrollbar whose layout gutter and painted thumb stay inside its
  // own band; dragging one bar must move only that band and must never be
  // captured by the shelf's resize strip. Card widths stay identical because
  // both ports reserve the same `scrollbar-gutter: stable` space.
  await page.mouse.move(4, 4);
  await page.waitForTimeout(160);
  const sb0 = await scrollbarState();
  check('scrollbar-scrollports-separate',
    sb0.contains.shelfInBody && sb0.contains.listInBody
    && !sb0.contains.shelfInList && !sb0.contains.listInShelf
    && sb0.contains.innerInShelf
    && sb0.cardCounts.pinned === 3 && sb0.cardCounts.unpinned === 4,
    sb0.contains);
  check('scrollbar-native-gutters',
    sb0.inner.gutter > 0 && sb0.inner.gutter === sb0.list.gutter
    && sb0.inner.scrollbarGutter === 'stable' && sb0.list.scrollbarGutter === 'stable'
    && sb0.inner.scrollbarWidth !== 'none' && sb0.list.scrollbarWidth !== 'none'
    && sb0.inner.barWidth === `${sb0.inner.gutter}px`
    && sb0.list.barWidth === `${sb0.list.gutter}px`
    && sb0.body.gutter === 0 && sb0.body.overflowY === 'visible', {
      inner: { gutter: sb0.inner.gutter, barWidth: sb0.inner.barWidth, style: sb0.inner.scrollbarGutter },
      list: { gutter: sb0.list.gutter, barWidth: sb0.list.barWidth, style: sb0.list.scrollbarGutter },
      body: { gutter: sb0.body.gutter, overflow: sb0.body.overflowY },
    });
  check('scrollbar-both-bands-overflow',
    sb0.inner.maxScroll > 20 && sb0.list.maxScroll > 20,
    { inner: sb0.inner.maxScroll, list: sb0.list.maxScroll });
  check('scrollbar-confined-to-each-band',
    Math.abs(sb0.inner.right - sb0.list.right) <= 0.5
    && sb0.inner.top >= sb0.shelf.top - 0.5
    && sb0.inner.bottom <= sb0.shelf.bottom - 8 + 0.5
    && sb0.list.top >= sb0.shelf.bottom - 0.5
    && sb0.list.bottom <= sb0.body.bottom + 0.5
    && sb0.handle.right <= sb0.inner.right - sb0.inner.gutter + 0.5
    && sb0.bottomCue.right <= sb0.inner.right - sb0.inner.gutter + 0.5
    && Math.abs((sb0.grip.left + sb0.grip.right) / 2 - (sb0.shelf.left + sb0.shelf.right) / 2) <= 1,
    {
      shelf: sb0.shelf, inner: sb0.inner, list: sb0.list,
      handle: sb0.handle, grip: sb0.grip, cue: sb0.bottomCue,
    });
  check('scrollbar-card-widths-aligned',
    Math.abs(sb0.cardWidths.pinned - sb0.cardWidths.unpinned) <= 0.6
    && Math.abs(sb0.cardLefts.pinned - sb0.cardLefts.unpinned) <= 0.6,
    { widths: sb0.cardWidths, lefts: sb0.cardLefts });

  // Host CSS fades native thumbs in on scrollport hover. Point at the exact
  // scrollbar channel, not a card: the pinned bar must appear only in the
  // pinned band, the list bar only below it, and the other stays transparent.
  const barPoint = async (which) => page.evaluate((which) => {
    const el = which === 'pinned'
      ? document.querySelector('[data-pinned-shelf-scroll]')
      : document.querySelector('[data-quota-scroll]');
    const r = el.getBoundingClientRect();
    const gutter = el.offsetWidth - el.clientWidth;
    return { x: r.right - gutter / 2, y: r.top + 18 };
  }, which);
  const sbPinnedPoint = await barPoint('pinned');
  await page.mouse.move(sbPinnedPoint.x, sbPinnedPoint.y);
  await page.waitForTimeout(180);
  const sbPinnedHover = await scrollbarState();
  check('scrollbar-pinned-visible-on-hover',
    sbPinnedHover.inner.hover && alphaOf(sbPinnedHover.inner.thumbBg) > 0.1
    && alphaOf(sbPinnedHover.list.thumbBg) < 0.05, {
      inner: { hover: sbPinnedHover.inner.hover, thumb: sbPinnedHover.inner.thumbBg },
      list: { hover: sbPinnedHover.list.hover, thumb: sbPinnedHover.list.thumbBg },
    });
  await shot('scrollbars-pinned-hover');
  const sbListPoint = await barPoint('list');
  await page.mouse.move(sbListPoint.x, sbListPoint.y);
  await page.waitForTimeout(180);
  const sbListHover = await scrollbarState();
  check('scrollbar-list-visible-on-hover',
    sbListHover.list.hover && alphaOf(sbListHover.list.thumbBg) > 0.1
    && alphaOf(sbListHover.inner.thumbBg) < 0.05, {
      inner: { hover: sbListHover.inner.hover, thumb: sbListHover.inner.thumbBg },
      list: { hover: sbListHover.list.hover, thumb: sbListHover.list.thumbBg },
    });
  await shot('scrollbars-list-hover');

  // A real pointer drag on each bar moves that band's own scrollTop only.
  const dragScrollbar = async (which, deltaY) => {
    const point = await barPoint(which);
    await page.mouse.move(point.x, point.y);
    await page.waitForTimeout(120);
    await page.mouse.down();
    await page.mouse.move(point.x, point.y + deltaY, { steps: 10 });
    await page.mouse.up();
    await page.waitForTimeout(220);
    return point;
  };
  await page.evaluate(() => {
    document.querySelector('[data-pinned-shelf-scroll]').scrollTop = 0;
    document.querySelector('[data-quota-scroll]').scrollTop = 0;
  });
  await page.waitForTimeout(120);
  const dragBase = await scrollbarState();
  await dragScrollbar('pinned', 60);
  const afterPinnedDrag = await scrollbarState();
  check('scrollbar-drag-pinned-only',
    afterPinnedDrag.inner.scrollTop > 20
    && afterPinnedDrag.list.scrollTop === 0
    && afterPinnedDrag.frame.height === dragBase.frame.height
    && afterPinnedDrag.storedHeight === dragBase.storedHeight
    && afterPinnedDrag.resizing === null, {
      inner: afterPinnedDrag.inner.scrollTop, list: afterPinnedDrag.list.scrollTop,
      frame: [dragBase.frame.height, afterPinnedDrag.frame.height],
      stored: [dragBase.storedHeight, afterPinnedDrag.storedHeight],
      resizing: afterPinnedDrag.resizing,
    });
  await dragScrollbar('list', 40);
  const afterListDrag = await scrollbarState();
  check('scrollbar-drag-list-only',
    afterListDrag.list.scrollTop > 20
    && afterListDrag.inner.scrollTop === afterPinnedDrag.inner.scrollTop
    && afterListDrag.frame.height === dragBase.frame.height
    && afterListDrag.resizing === null, {
      inner: [afterPinnedDrag.inner.scrollTop, afterListDrag.inner.scrollTop],
      list: afterListDrag.list.scrollTop,
      frame: afterListDrag.frame.height,
    });

  // The very bottom of the shelf scrollbar overlaps the resize strip's
  // vertical range. Dragging the bar there must scroll, not resize: the
  // strip now stops at the scrollbar gutter.
  await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    inner.scrollTop = inner.scrollHeight;
  });
  await page.waitForTimeout(160);
  const bottomBase = await scrollbarState();
  const bottomPoint = await page.evaluate(() => {
    const el = document.querySelector('[data-pinned-shelf-scroll]');
    const r = el.getBoundingClientRect();
    const gutter = el.offsetWidth - el.clientWidth;
    return { x: r.right - gutter / 2, y: r.bottom - 4 };
  });
  await page.mouse.move(bottomPoint.x, bottomPoint.y);
  await page.waitForTimeout(120);
  await page.mouse.down();
  await page.mouse.move(bottomPoint.x, bottomPoint.y - 30, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(220);
  const afterBottomDrag = await scrollbarState();
  check('scrollbar-drag-at-resizer-edge',
    afterBottomDrag.inner.scrollTop < bottomBase.inner.scrollTop - 5
    && afterBottomDrag.frame.height === bottomBase.frame.height
    && afterBottomDrag.storedHeight === bottomBase.storedHeight
    && afterBottomDrag.resizing === null, {
      before: bottomBase.inner.scrollTop, after: afterBottomDrag.inner.scrollTop,
      frame: [bottomBase.frame.height, afterBottomDrag.frame.height],
      stored: [bottomBase.storedHeight, afterBottomDrag.storedHeight],
      resizing: afterBottomDrag.resizing,
    });

  // Resizing the band moves both bars with it: the pinned bar's track stays
  // exactly the shelf scrollport, while the list bar starts at the new shelf
  // bottom. The split never overlaps and the gutters stay identical.
  await page.focus('[data-pinned-shelf-resize]');
  await page.keyboard.press('Home');
  await page.waitForTimeout(260);
  const sbMin = await scrollbarState();
  await page.keyboard.press('End');
  await page.waitForTimeout(260);
  const sbMax = await scrollbarState();
  check('scrollbar-dynamic-height',
    sbMax.frame.height > sbMin.frame.height
    && sbMin.inner.bottom <= sbMin.shelf.bottom - 8 + 0.5
    && sbMax.inner.bottom <= sbMax.shelf.bottom - 8 + 0.5
    && Math.abs(sbMin.list.top - sbMin.shelf.bottom) <= 0.5
    && Math.abs(sbMax.list.top - sbMax.shelf.bottom) <= 0.5
    && Math.abs(sbMin.inner.gutter - sbMin.list.gutter) <= 0.5
    && Math.abs(sbMax.inner.gutter - sbMax.list.gutter) <= 0.5
    && sbMin.inner.maxScroll > 0 && sbMax.inner.maxScroll > 0
    && sbMax.list.maxScroll > 0, {
      min: { frame: sbMin.frame.height, innerMax: sbMin.inner.maxScroll, listTop: sbMin.list.top, shelfBottom: sbMin.shelf.bottom },
      max: { frame: sbMax.frame.height, innerMax: sbMax.inner.maxScroll, listTop: sbMax.list.top, shelfBottom: sbMax.shelf.bottom },
      gutters: [sbMin.inner.gutter, sbMin.list.gutter, sbMax.inner.gutter, sbMax.list.gutter],
    });

  // Theme: the bars are host-styled native scrollbars, so their paint follows
  // the theme's scrollbar tokens. Emulate the theme provider's dark seeds
  // (class + inline variables) and verify geometry, contrast and thumb paint,
  // then restore the light state exactly.
  const themeBefore = await page.evaluate(() => ({
    style: document.documentElement.getAttribute('style'),
    bg: getComputedStyle(document.body).backgroundColor,
    fg: getComputedStyle(document.body).color,
  }));
  await page.evaluate(() => {
    const root = document.documentElement;
    root.classList.add('dark');
    root.style.colorScheme = 'dark';
    root.style.setProperty('--theme-background-seed', '#0d0d0e');
    root.style.setProperty('--theme-sidebar-seed', '#0a0a0b');
    root.style.setProperty('--theme-card-seed', '#161618');
    root.style.setProperty('--theme-elevated-seed', '#1c1c20');
    root.style.setProperty('--theme-bubble-seed', '#18181b');
    root.style.setProperty('--theme-foreground', '#f2f2f5');
    root.style.setProperty('--theme-primary', '#7aa2ff');
    root.style.setProperty('--theme-midground', '#7aa2ff');
  });
  await page.waitForTimeout(320);
  const themeDark = await page.evaluate(() => ({
    scheme: getComputedStyle(document.documentElement).colorScheme,
    bg: getComputedStyle(document.body).backgroundColor,
    fg: getComputedStyle(document.body).color,
  }));
  const sbDark = await scrollbarState();
  check('scrollbar-theme-gutters-and-alignment',
    themeDark.scheme === 'dark'
    && luminanceOf(themeBefore.bg) > 0.7
    && luminanceOf(themeDark.bg) < 0.2
    && luminanceOf(themeDark.fg) > 0.6
    && sbDark.inner.gutter === sbDark.list.gutter
    && sbDark.inner.gutter > 0
    && Math.abs(sbDark.cardWidths.pinned - sbDark.cardWidths.unpinned) <= 0.6, {
      scheme: themeDark.scheme,
      luminance: { light: luminanceOf(themeBefore.bg), dark: luminanceOf(themeDark.bg), fg: luminanceOf(themeDark.fg) },
      gutters: [sbDark.inner.gutter, sbDark.list.gutter],
      widths: sbDark.cardWidths,
      light: themeBefore, dark: themeDark,
    });
  const darkPoint = await barPoint('pinned');
  await page.mouse.move(darkPoint.x, darkPoint.y);
  await page.waitForTimeout(200);
  const sbDarkHover = await scrollbarState();
  check('scrollbar-theme-thumb-painted',
    alphaOf(sbDarkHover.inner.thumbBg) > 0.1
    && alphaOf(sbDarkHover.list.thumbBg) < 0.05, {
      thumb: sbDarkHover.inner.thumbBg, other: sbDarkHover.list.thumbBg,
    });
  await shot('scrollbars-dark-pinned-hover');
  const darkListPoint = await barPoint('list');
  await page.mouse.move(darkListPoint.x, darkListPoint.y);
  await page.waitForTimeout(200);
  await shot('scrollbars-dark-list-hover');
  await page.evaluate((before) => {
    const root = document.documentElement;
    root.classList.remove('dark');
    if (before.style === null) root.removeAttribute('style');
    else root.setAttribute('style', before.style);
  }, themeBefore);
  await page.waitForTimeout(220);
  const themeRestored = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  check('scrollbar-theme-restored', themeRestored === themeBefore.bg, {
    before: themeBefore.bg, after: themeRestored,
  });

  // Logical insets keep the same split in RTL: the scrollbar moves to the
  // left edge, and the resize strip/cue stop at that gutter instead of
  // covering it. Cards in both bands stay the same width.
  await page.evaluate(() => document.querySelector('[data-quota-layout]').setAttribute('dir', 'rtl'));
  await page.waitForTimeout(260);
  const sbRtl = await scrollbarState();
  check('scrollbar-rtl-confined',
    sbRtl.inner.gutter > 0 && sbRtl.inner.gutter === sbRtl.list.gutter
    && Math.abs(sbRtl.inner.left + sbRtl.inner.gutter - sbRtl.handle.left) <= 0.5
    && Math.abs(sbRtl.handle.right - sbRtl.inner.right) <= 0.5
    && Math.abs(sbRtl.inner.left + sbRtl.inner.gutter - sbRtl.bottomCue.left) <= 0.5
    && Math.abs((sbRtl.grip.left + sbRtl.grip.right) / 2 - (sbRtl.shelf.left + sbRtl.shelf.right) / 2) <= 1
    && Math.abs(sbRtl.cardWidths.pinned - sbRtl.cardWidths.unpinned) <= 0.6, {
      gutters: [sbRtl.inner.gutter, sbRtl.list.gutter],
      inner: sbRtl.inner, handle: sbRtl.handle, cue: sbRtl.bottomCue,
      widths: sbRtl.cardWidths,
    });
  await shot('scrollbars-rtl');
  await page.evaluate(() => document.querySelector('[data-quota-layout]').setAttribute('dir', 'ltr'));
  await page.waitForTimeout(200);

  // Cues are overlays: they paint over the clipping edge and never change the
  // scroller's height, so scrolling cannot make the shelf jiggle.
  await page.evaluate(() => { document.querySelector('[data-pinned-shelf-scroll]').scrollTop = 0; });
  await page.waitForTimeout(320);
  const cueBox = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const cue = document.querySelector('[data-pinned-overflow-cue="bottom"]');
    const i = inner.getBoundingClientRect();
    const c = cue && cue.getBoundingClientRect();
    return {
      has: Boolean(cue),
      innerHeight: i.height,
      cueTop: c && c.top,
      cueBottom: c && c.bottom,
      innerTop: i.top,
      innerBottom: i.bottom,
      position: cue && getComputedStyle(cue).position,
      gradient: cue && getComputedStyle(cue).backgroundImage,
      opacity: cue && getComputedStyle(cue).opacity,
    };
  });
  check('cue-below-content', cueBox.has && cueBox.position === 'absolute'
    && cueBox.cueTop >= cueBox.innerTop - 0.5 && cueBox.cueBottom <= cueBox.innerBottom + 0.5
    && /gradient/.test(cueBox.gradient || '') && Number(cueBox.opacity) > 0.95, cueBox);
  await page.evaluate(() => { document.querySelector('[data-pinned-shelf-scroll]').scrollTop = 99999; });
  await page.waitForTimeout(180);
  const topCue = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const cue = document.querySelector('[data-pinned-overflow-cue="top"]');
    const i = inner.getBoundingClientRect();
    const c = cue && cue.getBoundingClientRect();
    return {
      has: Boolean(cue),
      innerHeight: i.height,
      cueTop: c && c.top,
      cueBottom: c && c.bottom,
      innerTop: i.top,
      innerBottom: i.bottom,
      position: cue && getComputedStyle(cue).position,
    };
  });
  check('top-cue-reserved', topCue.has && topCue.position === 'absolute'
    && topCue.cueTop >= topCue.innerTop - 0.5 && topCue.cueBottom <= topCue.innerBottom + 0.5, topCue);
  check('cue-no-jiggle', cueBox.innerHeight === topCue.innerHeight && topCue.innerHeight > 0,
    { top: cueBox.innerHeight, bottom: topCue.innerHeight });

  // The pinned band is now its own scrollport above the list's. Scrolling the
  // list must not move it, no unpinned content may pass above it, and the
  // band's opaque background still paints the region's top edge. The list
  // scrollport begins exactly at the shelf's bottom edge, so its scrollbar is
  // confined below the pinned cards.
  await page.evaluate(() => {
    document.querySelector('[data-pinned-shelf-scroll]').scrollTop = 0;
    document.querySelector('[data-quota-scroll]').scrollTop = 260;
  });
  await page.waitForTimeout(220);
  const topMask = await page.evaluate(() => {
    const body = document.querySelector('[data-quota-body]');
    const shelf = document.querySelector('[data-pinned-shelf]');
    const list = document.querySelector('[data-quota-scroll]');
    const b = body.getBoundingClientRect();
    const s = shelf.getBoundingClientRect();
    const l = list.getBoundingClientRect();
    const style = getComputedStyle(shelf);
    const x = b.x + b.width / 2;
    const hits = [];
    for (let dy = 0; dy <= 8; dy += 0.5) {
      const el = document.elementFromPoint(x, b.top + dy);
      hits.push({
        dy,
        inShelf: Boolean(el && shelf.contains(el)),
        el: el ? el.getAttribute('data-provider') || el.getAttribute('data-pinned-shelf') || el.tagName : null,
      });
    }
    return {
      listTop: list.scrollTop,
      bodyTop: b.top,
      shelfTop: s.top,
      shelfBottom: s.bottom,
      listTopEdge: l.top,
      gap: s.top - b.top,
      scrollPaddingTop: getComputedStyle(list).paddingTop,
      background: style.backgroundColor,
      borderTop: style.borderTopWidth,
      boxShadow: style.boxShadow,
      paddingTop: style.paddingTop,
      hits,
    };
  });
  check('pinned-top-edge-flush', topMask.listTop > 200
    && Math.abs(topMask.gap) <= 0.5
    && Math.abs(topMask.shelfBottom - topMask.listTopEdge) <= 0.5
    && topMask.scrollPaddingTop === '0px'
    && topMask.hits.every((hit) => hit.inShelf), topMask);
  check('pinned-top-mask-opaque-no-frame', !/rgba\(0, 0, 0, 0\)|transparent/.test(topMask.background)
    && topMask.borderTop === '0px' && topMask.boxShadow === 'none', {
    background: topMask.background,
    borderTop: topMask.borderTop,
    boxShadow: topMask.boxShadow,
  });

  // The shelf stays opaque and keeps a real padding strip between a clipped
  // pinned card and the unpinned list, so the two surfaces cannot read as one.
  const boundary = await page.evaluate(() => {
    const shelf = document.querySelector('[data-pinned-shelf]');
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const style = getComputedStyle(shelf);
    const i = inner.getBoundingClientRect();
    const s = shelf.getBoundingClientRect();
    return {
      paddingBottom: parseFloat(style.paddingBottom),
      background: style.backgroundColor,
      gap: s.bottom - i.bottom,
    };
  });
  check('pinned-boundary-clear', boundary.paddingBottom >= 8 && boundary.gap >= 7
    && !/rgba\(0, 0, 0, 0\)|transparent/.test(boundary.background), boundary);

  // Shrinking the pinned set resets a stale shelf offset.
  await page.evaluate(() => window.__setLayout({ pinned: ['deepseek'], order: ['openai', 'gemini', 'cursor', 'openrouter', 'ollama'] }));
  await page.waitForTimeout(180);
  const resetScroll = await page.evaluate(() => document.querySelector('[data-pinned-shelf-scroll]').scrollTop);
  check('unpin-resets-shelf-scroll', resetScroll === 0, { resetScroll });

  // Short pane: the first pinned card is taller than the available cap, so
  // the safe fallback keeps a readable 72px slice of the unpinned list and
  // lets the too-tall card scroll inside the shelf. The resize range
  // collapses and reports the blocked state.
  await size(300, 240, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(220);
  const short = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const region = document.querySelector('[data-quota-body]');
    const card = inner.querySelector('[data-provider]');
    const i = inner.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    return {
      inner: i.height,
      cardVisible: Math.min(c.bottom, i.bottom) - Math.max(c.top, i.top),
      cue: Boolean(document.querySelector('[data-pinned-overflow-cue="bottom"]')),
      viewport: region.clientHeight,
    };
  });
  check('short-pane-readable-fallback', short.inner > 20 && short.cue
    && short.viewport - (short.inner + 10) >= 50, short);
  const shortResize = await page.evaluate(() => {
    const handle = document.querySelector('[data-pinned-shelf-resize]');
    const frame = document.querySelector('[data-pinned-shelf-frame]');
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const card = inner.querySelector('[data-provider]');
    const i = inner.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    return {
      blocked: handle.getAttribute('aria-disabled') === 'true',
      hint: handle.getAttribute('aria-description'),
      min: Number(handle.getAttribute('aria-valuemin')),
      max: Number(handle.getAttribute('aria-valuemax')),
      frame: frame.getBoundingClientRect().height,
      cardHeight: c.height,
      visible: Math.min(c.bottom, i.bottom) - Math.max(c.top, i.top),
      overflow: inner.scrollHeight - inner.clientHeight,
    };
  });
  check('short-pane-resize-blocked-and-scrollable', shortResize.blocked === true
    && shortResize.min === shortResize.max
    && shortResize.min <= shortResize.max
    && Math.abs(shortResize.frame - shortResize.max) <= 0.5
    && /cannot be resized/.test(shortResize.hint)
    && shortResize.visible < shortResize.cardHeight - 1
    && shortResize.overflow > 0, shortResize);
  await shot('shelf-short');

  // Edit mode adds the layout toolbar, so a 180/200px pane has even less
  // card region than the quiet view. The split must still leave both bands
  // scrollable instead of the old 72px list reserve crushing the shelf to 0.
  for (const paneHeight of [180, 200]) {
    await size(300, paneHeight, ['anthropic', 'deepseek'],
      ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
    await page.waitForTimeout(240);
    const compact = await page.evaluate(() => {
      const inner = document.querySelector('[data-pinned-shelf-scroll]');
      const list = document.querySelector('[data-quota-scroll]');
      const card = inner.querySelector('[data-provider]');
      const i = inner.getBoundingClientRect();
      const c = card.getBoundingClientRect();
      const shelfBefore = inner.scrollTop;
      inner.scrollTop = 12;
      const shelfScrolled = inner.scrollTop;
      inner.scrollTop = shelfBefore;
      const listBefore = list.scrollTop;
      list.scrollTop = 12;
      const listScrolled = list.scrollTop;
      list.scrollTop = listBefore;
      return {
        inner: i.height,
        list: list.clientHeight,
        visible: Math.min(c.bottom, i.bottom) - Math.max(c.top, i.top),
        shelfScrolled,
        listScrolled,
        shells: document.querySelectorAll('[data-card-shell]').length,
      };
    });
    check(`short-pane-${paneHeight}-edit-mode-nonzero`,
      compact.shells > 0 && compact.inner >= 18 && compact.list >= 12
        && compact.visible >= 12 && compact.shelfScrolled > 0
        && compact.listScrolled > 0, compact);
  }

  // A monitor-enabled pane adds a tab row and (with a snapshot) the summary
  // strip above the cards. At 180/200px that chrome used to consume the whole
  // body (a 0px list, pinned cards below the pane edge). The compact layout
  // steps the summary and footer aside and keeps the tabs on one row, so both
  // bands stay readable and scrollable in the real viewport.
  await page.evaluate(() => window.__setMonitor(true));
  await page.waitForTimeout(320);
  // Compact mode is a short-pane rule, not a permanent diet: a normal 540px
  // pane keeps the summary strip and footer.
  await size(300, 540, ['anthropic', 'deepseek'],
    ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(420);
  const monitorFull = await page.evaluate(() => ({
    body: document.querySelector('[data-quota-body]').clientHeight,
    summary: Boolean(document.querySelector('[data-summary-strip]')),
    footer: Boolean(document.querySelector('[data-quota-checked]')),
    compact: document.querySelector('[data-monitor-tabs]').getAttribute('data-compact'),
  }));
  check('monitor-chrome-restored-tall-pane',
    monitorFull.body >= 300 && monitorFull.summary && monitorFull.footer
      && monitorFull.compact === null, monitorFull);
  for (const paneHeight of [180, 200]) {
    await size(300, paneHeight, ['anthropic', 'deepseek'],
      ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
    await page.waitForTimeout(460);
    const compact = await page.evaluate(() => {
      const pane = document.getElementById('pane');
      const body = document.querySelector('[data-quota-body]');
      const inner = document.querySelector('[data-pinned-shelf-scroll]');
      const frame = document.querySelector('[data-pinned-shelf-frame]');
      const list = document.querySelector('[data-quota-scroll]');
      const card = inner.querySelector('[data-provider]');
      const tabs = document.querySelector('[data-monitor-tabs]');
      const tablist = tabs ? tabs.querySelector('[role="tablist"]') : null;
      const paneRect = pane.getBoundingClientRect();
      const bodyRect = body.getBoundingClientRect();
      const tabsRect = tabs.getBoundingClientRect();
      const innerRect = inner.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      const shelfBefore = inner.scrollTop;
      inner.scrollTop = 12;
      const shelfScrolled = inner.scrollTop;
      inner.scrollTop = shelfBefore;
      const listBefore = list.scrollTop;
      list.scrollTop = 12;
      const listScrolled = list.scrollTop;
      list.scrollTop = listBefore;
      return {
        paneHeight: paneRect.height,
        body: body.clientHeight,
        bodyTop: bodyRect.top,
        bodyBottom: bodyRect.bottom,
        tabsHeight: tabsRect.height,
        tabsBottom: tabsRect.bottom,
        tabCount: tablist ? tablist.querySelectorAll('[data-tab]').length : 0,
        tablistHeight: tablist ? tablist.getBoundingClientRect().height : 0,
        summary: Boolean(document.querySelector('[data-summary-strip]')),
        footer: Boolean(document.querySelector('[data-quota-checked]')),
        compact: tabs ? tabs.getAttribute('data-compact') : null,
        inner: innerRect.height,
        frame: frame.getBoundingClientRect().height,
        list: list.clientHeight,
        visible: Math.min(cardRect.bottom, innerRect.bottom) - Math.max(cardRect.top, innerRect.top),
        shelfScrolled,
        listScrolled,
        shells: document.querySelectorAll('[data-card-shell]').length,
        paneTop: paneRect.top,
        paneBottom: paneRect.bottom,
        headerTop: document.querySelector('[data-quota-layout] > div').getBoundingClientRect().top,
      };
    });
    check(`short-pane-monitor-${paneHeight}-compact-chrome`,
      compact.compact === 'true'
        && compact.body >= 60
        && compact.inner >= 28 && compact.frame === compact.inner
        && compact.list >= 20
        && compact.tabsHeight <= 30 && compact.tablistHeight <= 30
        && compact.tabCount === 6
        && compact.summary === false && compact.footer === false
        && compact.tabsBottom <= compact.bodyTop + 0.5
        && compact.bodyBottom <= compact.paneBottom + 0.5
        && compact.headerTop >= compact.paneTop - 0.5, compact);
    check(`short-pane-monitor-${paneHeight}-both-bands-usable`,
      compact.shells > 0 && compact.visible >= 20
        && compact.shelfScrolled > 0 && compact.listScrolled > 0, compact);
  }
  // The compact rule is not edit-only: the quiet view keeps the same usable
  // split with the tabs present.
  await page.click('[data-card-edit]');
  await page.waitForTimeout(260);
  await size(300, 180, ['anthropic', 'deepseek'],
    ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(460);
  const compactQuiet = await page.evaluate(() => {
    const body = document.querySelector('[data-quota-body]');
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const list = document.querySelector('[data-quota-scroll]');
    const tabs = document.querySelector('[data-monitor-tabs]');
    return {
      body: body.clientHeight,
      shelf: inner.clientHeight,
      list: list.clientHeight,
      shells: document.querySelectorAll('[data-card-shell]').length,
      tabsHeight: tabs.getBoundingClientRect().height,
      summary: Boolean(document.querySelector('[data-summary-strip]')),
      footer: Boolean(document.querySelector('[data-quota-checked]')),
    };
  });
  check('short-pane-monitor-180-quiet-compact',
    compactQuiet.body >= 90 && compactQuiet.shelf >= 32
      && compactQuiet.list >= 24 && compactQuiet.shells === 0
      && compactQuiet.tabsHeight <= 30 && !compactQuiet.summary
      && !compactQuiet.footer, compactQuiet);
  // Restore the state the following resize section expects: edit mode on,
  // monitor chrome off.
  await page.click('[data-card-edit]');
  await page.waitForTimeout(200);
  await page.evaluate(() => window.__setMonitor(false));
  await page.waitForTimeout(260);

  // A pane just too short for the card plus the strip, but tall enough to
  // show the card itself: the floor is infeasible and the strip must still
  // overlap. The handle must report blocked instead of "already fit".
  // Derive the pane height from the measured card so this stays true if host
  // font metrics shift the card height.
  await size(300, 540, ['anthropic'], ALL.filter((id) => id !== 'anthropic'));
  await page.waitForTimeout(280);
  const tightCardBottom = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const card = inner.querySelector('[data-provider]');
    return card.getBoundingClientRect().bottom - inner.getBoundingClientRect().top;
  });
  // Cap = floor(pane/2) - 12; put it 4px above the card bottom, inside the
  // 8px strip that still needs to sit below the card.
  const tightPane = 2 * (Math.ceil(tightCardBottom) + 4 + 12);
  await size(300, tightPane, ['anthropic'], ALL.filter((id) => id !== 'anthropic'));
  await page.waitForTimeout(280);
  const tightFloor = await page.evaluate(() => {
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const handle = document.querySelector('[data-pinned-shelf-resize]');
    const frame = document.querySelector('[data-pinned-shelf-frame]');
    const card = inner.querySelector('[data-provider]');
    const i = inner.getBoundingClientRect();
    const h = handle.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    return {
      blocked: handle.getAttribute('aria-disabled') === 'true',
      hint: handle.getAttribute('aria-description'),
      min: Number(handle.getAttribute('aria-valuemin')),
      max: Number(handle.getAttribute('aria-valuemax')),
      frame: frame.getBoundingClientRect().height,
      cardTop: c.top,
      cardBottom: c.bottom,
      innerTop: i.top,
      innerBottom: i.bottom,
      handleTop: h.top,
    };
  });
  check('resize-tight-pane-floor-blocked', tightFloor.blocked === true
    && tightFloor.min === tightFloor.max
    && Math.abs(tightFloor.frame - tightFloor.max) <= 0.5
    && /cannot be resized/.test(tightFloor.hint)
    && tightFloor.cardTop >= tightFloor.innerTop - 0.5
    && tightFloor.cardBottom <= tightFloor.innerBottom + 0.5
    && tightFloor.handleTop < tightFloor.cardBottom - 0.5, tightFloor);
  await shotElement('shelf-tight-floor', '[data-pinned-shelf]');

  // The handle belongs to the shelf, not to edit mode: card shells disappear
  // in the quiet view but the same measured range and drag keep working.
  await size(300, 540, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.click('[data-card-edit]');
  await page.waitForTimeout(300);
  const quietBefore = await shelfState();
  // Drag by the measured distance to the floor: a fixed -30 only landed on
  // the minimum because the old card height happened to make the two equal.
  const quietDrag = await handleDrag(quietBefore.handle.min - quietBefore.frame.height);
  const quietShells = await page.evaluate(() => Boolean(document.querySelector('[data-card-shell]')));
  check('resize-works-in-quiet-view', quietBefore.handle && !quietBefore.handle.blocked
    && quietBefore.frame.height > quietBefore.handle.min
    && quietDrag.after.frame.height === quietDrag.after.handle.min
    && quietShells === false, {
      before: quietBefore.frame.height, min: quietBefore.handle.min,
      after: quietDrag.after.frame.height, afterMin: quietDrag.after.handle.min,
      shells: quietShells,
    });

  // Very short quiet panes used to give the unpinned list the entire 72px
  // budget and leave the pinned scrollport at 0px in a 180px pane (18px at
  // 200px), so pinned cards disappeared behind their own band. Both bands
  // must keep a usable, scrollable slice within the real physical budget.
  for (const paneHeight of [180, 200]) {
    await size(300, paneHeight, ['anthropic', 'deepseek'],
      ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
    await page.waitForTimeout(240);
    const tiny = await page.evaluate(() => {
      const inner = document.querySelector('[data-pinned-shelf-scroll]');
      const frame = document.querySelector('[data-pinned-shelf-frame]');
      const card = inner.querySelector('[data-provider]');
      const list = document.querySelector('[data-quota-scroll]');
      const handle = document.querySelector('[data-pinned-shelf-resize]');
      const i = inner.getBoundingClientRect();
      const c = card.getBoundingClientRect();
      const visible = Math.min(c.bottom, i.bottom) - Math.max(c.top, i.top);
      const shelfBefore = inner.scrollTop;
      inner.scrollTop = 30;
      const shelfScrolled = inner.scrollTop;
      inner.scrollTop = shelfBefore;
      const listBefore = list.scrollTop;
      list.scrollTop = 30;
      const listScrolled = list.scrollTop;
      list.scrollTop = listBefore;
      return {
        inner: i.height,
        frame: frame.getBoundingClientRect().height,
        list: list.clientHeight,
        visible,
        cue: Boolean(document.querySelector('[data-pinned-overflow-cue="bottom"]')),
        blocked: handle.getAttribute('aria-disabled') === 'true',
        hint: handle.getAttribute('aria-description'),
        min: Number(handle.getAttribute('aria-valuemin')),
        max: Number(handle.getAttribute('aria-valuemax')),
        shelfScrolled,
        listScrolled,
        shelfOverflow: inner.scrollHeight - inner.clientHeight,
        listOverflow: list.scrollHeight - list.clientHeight,
        shells: Boolean(document.querySelector('[data-card-shell]')),
      };
    });
    check(`short-pane-${paneHeight}-pinned-usable`,
      tiny.shells === false
        && tiny.inner >= 30 && tiny.frame === tiny.inner
        && tiny.visible >= 24 && tiny.cue && tiny.blocked
        && /cannot be resized/.test(tiny.hint || '')
        && tiny.min === tiny.max && tiny.shelfScrolled > 0
        && tiny.shelfOverflow > 0, tiny);
    check(`short-pane-${paneHeight}-list-reachable`,
      tiny.list >= 24 && tiny.listScrolled > 0 && tiny.listOverflow > 0,
      { list: tiny.list, listScrolled: tiny.listScrolled });
  }

  // ---- the natural default follows pins until the pane's half-height ----
  // The default is not a fixed pixel height: with no stored preference the
  // shelf grows through the last pinned card, and the 50% pane ceiling wins
  // once the cards no longer fit. A real pin click must produce that growth;
  // a stray click on the resize edge must not freeze the current auto height
  // into a manual preference.
  const reloadFresh = async () => {
    await page.reload();
    await page.waitForSelector('[data-card-edit]');
    await page.waitForTimeout(300);
  };
  const enterEditMode = async () => {
    await page.click('[data-card-edit]');
    await page.waitForTimeout(260);
  };
  const clickPin = async (id) => {
    await page.locator(`[data-card-shell="${id}"] [data-card-control="pin"]`).first().click();
    await page.waitForTimeout(380);
  };
  const defaultFit = (st) =>
    Math.min(
      // The auto ceiling fits the last card plus the 8px resize strip that
      // intrudes into the scrollport, so the handle never cuts the card.
      st.cards.length ? Math.ceil(st.cards[st.cards.length - 1].bottom + 8) : 0,
      Math.floor(st.paneHeight / 2) - 12,
      Math.floor(st.viewportHeight) - 12,
    );

  await reloadFresh();
  await size(300, 900, [], ALL);
  await page.waitForTimeout(300);
  await enterEditMode();
  const tallGrowth = [];
  for (const id of ['anthropic', 'deepseek', 'openai', 'gemini']) {
    await clickPin(id);
    tallGrowth.push(await shelfState());
  }
  check('pin-growth-tall-default', tallGrowth.every((st) =>
    st.storedHeight == null
    && st.handle.now === st.frame.height
    && Math.abs(st.frame.height - defaultFit(st)) <= 1), tallGrowth.map((st) => ({
      frame: st.frame.height, fit: defaultFit(st), now: st.handle.now, stored: st.storedHeight,
    })));
  check('pin-growth-tall-half-cap', tallGrowth[2].frame.height === Math.floor(tallGrowth[2].paneHeight / 2) - 12
    && tallGrowth[3].frame.height === tallGrowth[2].frame.height
    && tallGrowth[2].handle.max === tallGrowth[2].frame.height, tallGrowth.map((st) => ({
      pane: st.paneHeight, frame: st.frame.height, max: st.handle.max,
    })));

  await reloadFresh();
  await size(300, 540, [], ALL);
  await page.waitForTimeout(300);
  await enterEditMode();
  const normalGrowth = [];
  for (const id of ['anthropic', 'deepseek', 'openai']) {
    await clickPin(id);
    normalGrowth.push(await shelfState());
  }
  check('pin-growth-normal-default', normalGrowth.every((st) =>
    st.storedHeight == null
    && st.handle.now === st.frame.height
    && Math.abs(st.frame.height - defaultFit(st)) <= 1), normalGrowth.map((st) => ({
      frame: st.frame.height, fit: defaultFit(st), stored: st.storedHeight,
    })));
  check('pin-growth-normal-half-cap', normalGrowth[1].frame.height === Math.floor(normalGrowth[1].paneHeight / 2) - 12
    && normalGrowth[2].frame.height === normalGrowth[1].frame.height, normalGrowth.map((st) => st.frame.height));

  // A short but not pathological pane still grows through the first cards and
  // then hits the same half-pane ceiling (the 72px list slice only takes over
  // in the very short fallback covered above).
  await reloadFresh();
  await size(300, 480, ['anthropic'], ALL.filter((id) => id !== 'anthropic'));
  await page.waitForTimeout(360);
  const shortOne = await shelfState();
  await size(300, 480, ['anthropic', 'deepseek'], ALL.filter((id) => id !== 'anthropic' && id !== 'deepseek'));
  await page.waitForTimeout(360);
  const shortTwo = await shelfState();
  check('pin-growth-short-default', shortOne.storedHeight == null && shortTwo.storedHeight == null
    && Math.abs(shortOne.frame.height - defaultFit(shortOne)) <= 1
    && Math.abs(shortTwo.frame.height - defaultFit(shortTwo)) <= 1
    && shortTwo.frame.height > shortOne.frame.height
    && shortTwo.frame.height === Math.floor(shortTwo.paneHeight / 2) - 12, {
      one: { frame: shortOne.frame.height, fit: defaultFit(shortOne) },
      two: { frame: shortTwo.frame.height, fit: defaultFit(shortTwo) },
    });

  // Clicking the resize edge (or its usual 1px jitter) must not turn the
  // current auto height into a stored preference. Otherwise the shelf stops
  // following new pins and pane resizes for the rest of the app session.
  await reloadFresh();
  await size(300, 900, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const beforeHandleClick = await shelfState();
  let handleBox = await box('[data-pinned-shelf-resize]');
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
  await page.mouse.down();
  await page.mouse.up();
  await page.waitForTimeout(220);
  const afterHandleClick = await shelfState();
  handleBox = await box('[data-pinned-shelf-resize]');
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2 + 1);
  await page.mouse.up();
  await page.waitForTimeout(220);
  const afterHandleJitter = await shelfState();
  check('resize-click-keeps-auto', beforeHandleClick.storedHeight == null
    && afterHandleClick.storedHeight == null
    && afterHandleJitter.storedHeight == null
    && afterHandleClick.frame.height === beforeHandleClick.frame.height
    && afterHandleJitter.frame.height === beforeHandleClick.frame.height, {
      before: beforeHandleClick.frame.height,
      click: afterHandleClick.frame.height,
      jitter: afterHandleJitter.frame.height,
      stored: afterHandleClick.storedHeight,
    });
  await page.evaluate(() => window.__setLayout({
    pinned: ['anthropic', 'deepseek', 'openai'],
    order: ['gemini', 'cursor', 'openrouter', 'ollama'],
  }));
  await page.waitForTimeout(420);
  const grownAfterClick = await shelfState();
  check('pin-growth-after-handle-click', grownAfterClick.storedHeight == null
    && grownAfterClick.frame.height === Math.floor(grownAfterClick.paneHeight / 2) - 12
    && grownAfterClick.frame.height > afterHandleJitter.frame.height, {
      frame: grownAfterClick.frame.height,
      fit: defaultFit(grownAfterClick),
      stored: grownAfterClick.storedHeight,
    });

  // A deliberate compact height is a preference: new pins keep it (clamped
  // when needed), and it returns when a smaller pin set leaves room again.
  await reloadFresh();
  await size(300, 900, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const manualStart = await shelfState();
  const manualCompact = await handleDrag(-100);
  check('resize-manual-height-persists', manualCompact.after.storedHeight === manualCompact.after.frame.height
    && manualCompact.after.frame.height === manualStart.frame.height - 100, {
      start: manualStart.frame.height,
      after: manualCompact.after.frame.height,
      stored: manualCompact.after.storedHeight,
    });
  await page.evaluate(() => window.__setLayout({
    pinned: ['anthropic', 'deepseek', 'openai'],
    order: ['gemini', 'cursor', 'openrouter', 'ollama'],
  }));
  await page.waitForTimeout(420);
  const manualWithThird = await shelfState();
  check('resize-manual-survives-new-pins', manualWithThird.storedHeight === manualCompact.after.storedHeight
    && manualWithThird.frame.height === manualCompact.after.frame.height
    && manualWithThird.handle.max > manualWithThird.frame.height, {
      stored: manualWithThird.storedHeight,
      frame: manualWithThird.frame.height,
      max: manualWithThird.handle.max,
    });
  await page.evaluate(() => window.__setLayout({
    pinned: ['anthropic'],
    order: ['deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama'],
  }));
  await page.waitForTimeout(420);
  const manualOnePin = await shelfState();
  await page.evaluate(() => window.__setLayout({
    pinned: ['anthropic', 'deepseek'],
    order: ['openai', 'gemini', 'cursor', 'openrouter', 'ollama'],
  }));
  await page.waitForTimeout(420);
  const manualRestored = await shelfState();
  check('resize-manual-reclamps-and-restores', manualOnePin.storedHeight === manualCompact.after.storedHeight
    && manualOnePin.frame.height === manualOnePin.handle.max
    && manualRestored.storedHeight === manualCompact.after.storedHeight
    && manualRestored.frame.height === manualCompact.after.frame.height, {
      one: { frame: manualOnePin.frame.height, max: manualOnePin.handle.max },
      restored: manualRestored.frame.height,
      stored: manualRestored.storedHeight,
    });

  // Dragging back up to the ceiling means "as tall as this pane allows", so
  // the preference returns to automatic and later pins can grow it again.
  const toCeiling = await handleDrag(2000);
  check('resize-ceiling-returns-to-auto', toCeiling.after.storedHeight == null
    && toCeiling.after.frame.height === toCeiling.after.handle.max
    && toCeiling.after.handle.now === toCeiling.after.frame.height, {
      frame: toCeiling.after.frame.height,
      max: toCeiling.after.handle.max,
      stored: toCeiling.after.storedHeight,
    });
  await page.evaluate(() => window.__setLayout({
    pinned: ['anthropic', 'deepseek', 'openai'],
    order: ['gemini', 'cursor', 'openrouter', 'ollama'],
  }));
  await page.waitForTimeout(420);
  const grownAfterCeiling = await shelfState();
  check('pin-growth-after-ceiling-drag', grownAfterCeiling.storedHeight == null
    && grownAfterCeiling.frame.height === defaultFit(grownAfterCeiling)
    && grownAfterCeiling.frame.height > toCeiling.after.frame.height, {
      frame: grownAfterCeiling.frame.height,
      fit: defaultFit(grownAfterCeiling),
      before: toCeiling.after.frame.height,
    });

  // A legacy preference frozen exactly at the old auto ceiling (for example
  // by a click in an earlier build) must not hide the next pinned card: the
  // pin action recognises it as automatic and lets the band grow again.
  await reloadFresh();
  await size(300, 900, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(320);
  await enterEditMode();
  await page.waitForTimeout(320);
  const legacyCeiling = await shelfState();
  await page.evaluate((height) => {
    window.__store.set('pinnedShelfHeight', height);
    window.__setShelfHeight(height);
  }, legacyCeiling.frame.height);
  await page.waitForTimeout(260);
  const legacyBefore = await shelfState();
  await clickPin('openai');
  const legacyAfter = await shelfState();
  check('pin-growth-heals-legacy-ceiling', legacyBefore.storedHeight === legacyCeiling.frame.height
    && legacyBefore.frame.height === legacyCeiling.frame.height
    && legacyAfter.storedHeight == null
    && legacyAfter.frame.height > legacyBefore.frame.height
    && legacyAfter.frame.height === defaultFit(legacyAfter), {
      legacy: legacyBefore.storedHeight,
      before: legacyBefore.frame.height,
      after: legacyAfter.frame.height,
      fit: defaultFit(legacyAfter),
      storedAfter: legacyAfter.storedHeight,
    });

  // A saved compact height that is merely clamped by a temporarily short
  // pane is still the user's preference. Pinning while short must not discard
  // it; growing the pane back must restore the chosen height, not the ceiling.
  await reloadFresh();
  await size(300, 900, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const clampedStart = await shelfState();
  const clampedDrag = await handleDrag(-56);
  const savedCompact = clampedDrag.after.storedHeight;
  check('resize-clamped-preference-set', savedCompact === clampedStart.frame.height - 56
    && clampedDrag.after.frame.height === savedCompact, {
      start: clampedStart.frame.height,
      frame: clampedDrag.after.frame.height, stored: savedCompact,
    });
  await size(300, 300, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const clampedShort = await shelfState();
  await enterEditMode();
  await page.evaluate(() => {
    const button = document.querySelector('[data-card-shell="openai"] [data-card-control="pin"]');
    if (button) button.click();
  });
  await page.waitForTimeout(420);
  const clampedPinnedShort = await shelfState();
  check('resize-clamped-preference-kept-on-pin', clampedShort.storedHeight === savedCompact
    && clampedPinnedShort.storedHeight === savedCompact
    && clampedPinnedShort.frame.height === clampedPinnedShort.handle.max
    && clampedPinnedShort.handle.max < savedCompact, {
      shortFrame: clampedShort.frame.height,
      max: clampedPinnedShort.handle.max,
      stored: clampedPinnedShort.storedHeight,
    });
  await size(300, 900, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(420);
  const clampedRestored = await shelfState();
  check('resize-clamped-preference-restores', clampedRestored.storedHeight === savedCompact
    && clampedRestored.frame.height === savedCompact
    && clampedRestored.handle.max > savedCompact, {
      frame: clampedRestored.frame.height,
      max: clampedRestored.handle.max,
      stored: clampedRestored.storedHeight,
    });

  // ---- double-click snap: nearest card boundary, midpoint tipping point ----
  // The resize strip's centre is the band's bottom edge. A real double-click
  // there snap-heights to the boundary selected by the card under that edge:
  // above the card's midpoint -> previous card's bottom, below -> this card's
  // bottom. Every result still goes through the measured floor/ceiling clamp.
  const layoutSnapshot = () => page.evaluate(() => JSON.stringify(window.__store.get('cardLayout') || null));
  await reloadFresh();
  await size(300, 900, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const snapBase = await setShelf(null);
  const snapLayout = await layoutSnapshot();
  const snapFirst = snapBase.cards[0];
  const snapSecond = snapBase.cards[1];
  const snapThird = snapBase.cards[2];
  const firstBoundary = Math.ceil(snapFirst.bottom + 8);
  const secondBoundary = Math.ceil(snapSecond.bottom + 8);
  const thirdBoundary = Math.ceil(snapThird.bottom + 8);
  check('snap-boundaries-measured', firstBoundary === snapBase.handle.min
    && secondBoundary > firstBoundary && thirdBoundary > secondBoundary
    && snapBase.handle.max > secondBoundary && snapBase.handle.max < thirdBoundary, {
      firstBoundary, secondBoundary, thirdBoundary,
      min: snapBase.handle.min, max: snapBase.handle.max,
    });

  // The same rule for a later card: the top half of the third card snaps to
  // the second card's bottom. This proves the midpoint walk is not specific
  // to the second card.
  const thirdTopY = Math.round(
    snapThird.topContent + (snapThird.midContent - snapThird.topContent) * 0.4,
  );
  const thirdCut = await setShelf(thirdTopY);
  const thirdSnap = await dblclickShelfEdge();
  check('snap-top-half-of-third-card', thirdCut.handle.now === thirdTopY
    && thirdTopY < snapThird.midContent
    && thirdSnap.frame.height === secondBoundary
    && thirdSnap.storedHeight === secondBoundary
    && thirdSnap.cards[1].bottomViewport <= thirdSnap.inner.bottom + 0.5
    && thirdSnap.handle.top >= thirdSnap.cards[1].bottomViewport - 0.5
    && thirdSnap.handle.top - thirdSnap.cards[1].bottomViewport <= 1.5, {
      cut: thirdTopY, midpoint: snapThird.midContent,
      frame: thirdSnap.frame.height, boundary: secondBoundary,
      cardBottom: thirdSnap.cards[1].bottomViewport,
      handleTop: thirdSnap.handle.top,
    });

  // Top half of the second card: snap up to the first card's bottom, which is
  // the measured floor. The handle must land below that card's border and the
  // grip must stay clear of it.
  const topHalfY = Math.round(
    snapSecond.topContent + (snapSecond.midContent - snapSecond.topContent) * 0.4,
  );
  const topCut = await setShelf(topHalfY);
  const topSnap = await dblclickShelfEdge();
  const topLayout = await layoutSnapshot();
  check('snap-top-half-to-previous-card', topCut.handle.now === topHalfY
    && topHalfY < snapSecond.midContent
    && topSnap.frame.height === firstBoundary
    && topSnap.frame.height === topSnap.handle.min
    && topSnap.storedHeight === firstBoundary
    && topSnap.handle.now === topSnap.frame.height
    && topSnap.cards[0].bottomViewport <= topSnap.inner.bottom + 0.5
    && topSnap.handle.top >= topSnap.cards[0].bottomViewport - 0.5
    && topSnap.handle.top - topSnap.cards[0].bottomViewport <= 1.5
    && topSnap.gripBox.top >= topSnap.cards[0].bottomViewport - 0.5
    && topSnap.handle.resizing === false
    && topSnap.storedHeight !== snapBase.handle.max
    && topLayout === snapLayout, {
      cut: topHalfY, midpoint: snapSecond.midContent, frame: topSnap.frame.height,
      min: topSnap.handle.min, cardBottom: topSnap.cards[0].bottomViewport,
      handleTop: topSnap.handle.top, gripTop: topSnap.gripBox.top,
    });
  await shot('shelf-snap-first');

  // Bottom half of the same card: snap down to its own bottom. The card is
  // fully visible above the strip and the preference is stored, not the
  // ceiling (a third pinned card still follows).
  const bottomHalfY = Math.round(
    snapSecond.midContent + (snapSecond.bottom - snapSecond.midContent) * 0.4,
  );
  const bottomCut = await setShelf(bottomHalfY);
  const bottomSnap = await dblclickShelfEdge();
  const bottomLayout = await layoutSnapshot();
  check('snap-bottom-half-to-card-bottom', bottomCut.handle.now === bottomHalfY
    && bottomHalfY > snapSecond.midContent
    && bottomSnap.frame.height === secondBoundary
    && bottomSnap.storedHeight === secondBoundary
    && bottomSnap.handle.now === bottomSnap.frame.height
    && bottomSnap.cards[1].bottomViewport <= bottomSnap.inner.bottom + 0.5
    && bottomSnap.handle.top >= bottomSnap.cards[1].bottomViewport - 0.5
    && bottomSnap.handle.top - bottomSnap.cards[1].bottomViewport <= 1.5
    && bottomSnap.gripBox.top >= bottomSnap.cards[1].bottomViewport - 0.5
    && bottomSnap.inner.scrollHeight > bottomSnap.inner.clientHeight
    && bottomSnap.handle.resizing === false
    && bottomLayout === snapLayout, {
      cut: bottomHalfY, midpoint: snapSecond.midContent, frame: bottomSnap.frame.height,
      boundary: secondBoundary, cardBottom: bottomSnap.cards[1].bottomViewport,
      handleTop: bottomSnap.handle.top, gripTop: bottomSnap.gripBox.top,
    });
  await shot('shelf-snap-second');

  // Scrolled shelf: the same strip position must decide from the card in the
  // scrolled content, not from viewport coordinates. At scrollTop 0 the edge
  // cuts the second card's top half (floor); after scrolling past its midpoint
  // the same edge sits in the bottom half (second card's bottom).
  const scrollOffset = Math.ceil(snapSecond.midContent - topHalfY) + 12;
  await setShelf(topHalfY);
  const unscrolledSnap = await dblclickShelfEdge();
  await setShelf(topHalfY);
  await page.evaluate((offset) => {
    document.querySelector('[data-pinned-shelf-scroll]').scrollTop = offset;
  }, scrollOffset);
  await page.waitForTimeout(140);
  const scrollReady = await shelfState();
  const scrolledSnap = await dblclickShelfEdge();
  check('snap-scrolled-shelf-uses-content', scrollReady.inner.scrollTop === scrollOffset
    && scrollReady.inner.scrollTop + scrollReady.inner.clientHeight > snapSecond.midContent
    && scrollReady.inner.scrollTop + scrollReady.inner.clientHeight < snapSecond.bottom
    && unscrolledSnap.frame.height === firstBoundary
    && scrolledSnap.frame.height === secondBoundary
    && scrolledSnap.storedHeight === secondBoundary
    && scrolledSnap.handle.now === secondBoundary
    && scrolledSnap.cards[1].bottomViewport <= scrolledSnap.handle.top + 0.5
    && scrolledSnap.gripBox.top >= scrolledSnap.cards[1].bottomViewport - 0.5, {
      scrollTop: scrollReady.inner.scrollTop, edge: scrollReady.inner.scrollTop + scrollReady.inner.clientHeight,
      offset: scrollOffset, unscrolled: unscrolledSnap.frame.height, scrolled: scrolledSnap.frame.height,
      boundary: secondBoundary, cardBottom: scrolledSnap.cards[1].bottomViewport,
      handleTop: scrolledSnap.handle.top,
    });
  await shot('shelf-snap-scrolled');

  // Partial ceiling: at this pane height the 50% cap cuts the last card below
  // its midpoint, so a double-click asks for a bottom beyond the cap. The cap
  // wins exactly, the request is not rounded past it, and the ceiling keeps
  // its "automatic fit" storage meaning.
  await reloadFresh();
  await size(300, 680, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const capBase = await setShelf(null);
  const capCard = capBase.cards[1];
  const capRaw = Math.ceil(capCard.bottom + 8);
  const capMax = capBase.handle.max;
  const capCutY = Math.round((capCard.midContent + Math.min(capRaw, capMax)) / 2);
  const capBefore = await setShelf(capCutY);
  const capSnap = await dblclickShelfEdge();
  check('snap-beyond-half-pane-cap-clamps', capMax === Math.floor(capBase.paneHeight / 2) - 12
    && capRaw > capMax
    && capMax > capCard.midContent
    && capCutY > capCard.midContent && capCutY < capMax
    && capBefore.handle.now === capCutY
    && capSnap.frame.height === capMax
    && capSnap.storedHeight == null
    && capSnap.handle.now === capMax
    && capSnap.shelf.height <= capSnap.paneHeight / 2 + 0.5
    // The cap necessarily clips part of the last card; the handle cannot be
    // below a border that is beyond the cap. This is the documented tradeoff.
    && capSnap.cards[1].bottomViewport > capSnap.handle.top + 0.5, {
      capMax, capRaw, cut: capCutY, midpoint: capCard.midContent,
      frame: capSnap.frame.height, shelf: capSnap.shelf.height,
      pane: capSnap.paneHeight, cardBottom: capSnap.cards[1].bottomViewport,
      handleTop: capSnap.handle.top, stored: capSnap.storedHeight,
    });

  // The last card's bottom is the natural ceiling when it fits under the cap.
  // Snapping there is the automatic height, so no stale preference is stored.
  await reloadFresh();
  await size(300, 900, ['anthropic', 'deepseek'], ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  const lastBase = await setShelf(null);
  const lastSnapCard = lastBase.cards[1];
  const lastBoundary = Math.ceil(lastSnapCard.bottom + 8);
  const lastCutY = Math.round((lastSnapCard.midContent + lastSnapCard.bottom) / 2);
  const lastBefore = await setShelf(lastCutY);
  const lastSnap = await dblclickShelfEdge();
  check('snap-last-card-ceiling-auto', lastBase.handle.max === lastBoundary
    && lastCutY > lastSnapCard.midContent && lastCutY < lastBoundary
    && lastBefore.handle.now === lastCutY
    && lastSnap.frame.height === lastBoundary
    && lastSnap.frame.height === lastSnap.handle.max
    && lastSnap.storedHeight == null
    && lastSnap.handle.now === lastSnap.frame.height
    && lastSnap.handle.top >= lastSnap.cards[1].bottomViewport - 0.5
    && lastSnap.handle.top - lastSnap.cards[1].bottomViewport <= 1.5
    && lastSnap.gripBox.top >= lastSnap.cards[1].bottomViewport - 0.5, {
      boundary: lastBoundary, max: lastSnap.handle.max, cut: lastCutY,
      frame: lastSnap.frame.height, stored: lastSnap.storedHeight,
      cardBottom: lastSnap.cards[1].bottomViewport, handleTop: lastSnap.handle.top,
    });

  // Live card heights: narrowing the pane wraps the card text and moves every
  // boundary. The next double-click must use the new measurement, while the
  // height chosen on the wide pane is retained (clamped) across the change.
  await reloadFresh();
  await size(300, 900, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(360);
  await setShelf(350);
  const wideSnap = await dblclickShelfEdge();
  await size(230, 900, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(420);
  const narrowBase = await shelfState();
  const narrowRetained = Math.min(
    narrowBase.handle.max,
    Math.max(narrowBase.handle.min, wideSnap.frame.height),
  );
  const narrowFirst = narrowBase.cards[0];
  const narrowSecond = narrowBase.cards[1];
  const narrowFirstBoundary = Math.ceil(narrowFirst.bottom + 8);
  const narrowSecondBoundary = Math.ceil(narrowSecond.bottom + 8);
  await setShelf(Math.round(
    narrowSecond.topContent + (narrowSecond.midContent - narrowSecond.topContent) * 0.4,
  ));
  const narrowTopSnap = await dblclickShelfEdge();
  await setShelf(Math.round(
    narrowSecond.midContent + (narrowSecond.bottom - narrowSecond.midContent) * 0.4,
  ));
  const narrowBottomSnap = await dblclickShelfEdge();
  check('snap-uses-live-card-heights', wideSnap.frame.height === secondBoundary
    && wideSnap.storedHeight === secondBoundary
    && narrowBase.frame.height === narrowRetained
    && narrowBase.storedHeight === wideSnap.storedHeight
    && narrowFirstBoundary !== firstBoundary
    && narrowSecondBoundary !== secondBoundary
    && narrowTopSnap.frame.height === narrowFirstBoundary
    && narrowBottomSnap.frame.height === narrowSecondBoundary
    && narrowBottomSnap.storedHeight === narrowSecondBoundary
    && narrowBottomSnap.cards[1].bottomViewport <= narrowBottomSnap.inner.bottom + 0.5
    && narrowBottomSnap.handle.top >= narrowBottomSnap.cards[1].bottomViewport - 0.5
    && narrowBottomSnap.handle.top - narrowBottomSnap.cards[1].bottomViewport <= 1.5
    && narrowBottomSnap.gripBox.top >= narrowBottomSnap.cards[1].bottomViewport - 0.5, {
      wide: { frame: wideSnap.frame.height, boundary: secondBoundary },
      narrow: { min: narrowFirstBoundary, second: narrowSecondBoundary },
      retained: narrowBase.frame.height, top: narrowTopSnap.frame.height,
      bottom: narrowBottomSnap.frame.height,
    });

  // The snapped preference survives a pane width change and a normal drag
  // still takes over from the double-click result.
  await size(300, 900, ['anthropic', 'deepseek', 'openai'], ['gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(420);
  const restoredSnap = await shelfState();
  const restoredFrame = Math.min(
    restoredSnap.handle.max,
    Math.max(restoredSnap.handle.min, narrowSecondBoundary),
  );
  const draggedAfterSnap = await handleDrag(-14);
  check('snap-preference-survives-pane-width', restoredSnap.storedHeight === narrowSecondBoundary
    && restoredSnap.frame.height === restoredFrame
    && restoredSnap.handle.now === restoredSnap.frame.height, {
      stored: restoredSnap.storedHeight, frame: restoredSnap.frame.height,
      boundary: narrowSecondBoundary,
    });
  check('snap-then-drag-still-works', draggedAfterSnap.after.frame.height === restoredSnap.frame.height - 14
    && draggedAfterSnap.after.storedHeight === restoredSnap.frame.height - 14
    && draggedAfterSnap.after.handle.now === draggedAfterSnap.after.frame.height, {
      before: restoredSnap.frame.height,
      held: draggedAfterSnap.held.frame.height,
      after: draggedAfterSnap.after.frame.height,
      stored: draggedAfterSnap.after.storedHeight,
    });

  // ---- quiet grouped card: the shelf observes the replacement node ----
  // Switching a grouped card's account remounts ProviderRow under a key. The
  // observer used to stay attached to the detached node, so a taller new
  // account never re-measured the shelf. The stable group wrapper must keep
  // the observation alive in every mounted pane, not just the clicked one.
  await page.reload();
  await page.waitForSelector('[data-card-edit]');
  await page.waitForTimeout(300);
  await size(300, 900, ['anthropic'],
    ['deepseek', 'openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(350);
  await page.evaluate(() => window.__mountExtraPane('pane2', 300, 900));
  await page.waitForTimeout(450);
  const observerState = (rootId) => page.evaluate((id) => {
    const root = document.getElementById(id);
    const shelf = root.querySelector('[data-pinned-shelf]');
    const inner = root.querySelector('[data-pinned-shelf-scroll]');
    const card = inner.querySelector('[data-provider]');
    const handle = root.querySelector('[data-pinned-shelf-resize]');
    const selected = [...shelf.querySelectorAll('button[aria-pressed="true"]')]
      .map((b) => (b.textContent || '').trim()).filter(Boolean)[0] || null;
    return {
      max: Number(handle.getAttribute('aria-valuemax')),
      frame: root.querySelector('[data-pinned-shelf-frame]').getBoundingClientRect().height,
      card: card.getBoundingClientRect().height,
      overflow: inner.scrollHeight - inner.clientHeight,
      hint: handle.getAttribute('aria-description'),
      selected,
    };
  }, rootId);
  const observerBefore = {
    pane: await observerState('pane'),
    pane2: await observerState('pane2'),
  };
  await page.locator('#pane [data-pinned-shelf] button').filter({ hasText: 'Work' }).first().click();
  await page.waitForTimeout(350);
  // Grow the replacement ProviderRow without touching layout or data: only a
  // live ResizeObserver on the stable wrapper can pick this up.
  await page.evaluate(() => {
    for (const id of ['pane', 'pane2']) {
      const card = document.querySelector(`#${id} [data-pinned-shelf-scroll] [data-provider]`);
      if (card) card.style.minHeight = '700px';
    }
  });
  await page.waitForTimeout(500);
  const observerAfter = {
    pane: await observerState('pane'),
    pane2: await observerState('pane2'),
  };
  const observerGrew = (before, after) => after.max > before.max + 150
    && after.card >= 690 && after.frame === after.max
    && after.overflow > 0 && !/already fit/.test(after.hint || '');
  check('shelf-reobserves-account-switch',
    observerBefore.pane.selected !== 'Work'
      && observerAfter.pane.selected === 'Work'
      && observerGrew(observerBefore.pane, observerAfter.pane),
    { before: observerBefore.pane, after: observerAfter.pane });
  check('shelf-reobserves-second-pane',
    observerAfter.pane2.selected === 'Work'
      && observerGrew(observerBefore.pane2, observerAfter.pane2),
    { before: observerBefore.pane2, after: observerAfter.pane2 });
  await page.evaluate(() => window.__unmountExtraPane('pane2'));

  // ---- Reset honesty: a pin followed by its unpin is not a custom layout ----
  await page.reload();
  await page.waitForSelector('[data-card-edit]');
  await page.waitForTimeout(300);
  await size(300, 900, [], ALL);
  await page.click('[data-card-edit]');
  await page.waitForTimeout(250);
  const resetProbe = () => page.evaluate(() => {
    const reset = document.querySelector('[data-card-reset]');
    return {
      disabled: reset.disabled,
      stored: window.__store.get('cardLayout') || null,
      list: [...document.querySelectorAll('[data-quota-scroll] [data-provider]')]
        .map((n) => n.getAttribute('data-provider')),
    };
  });
  const clickPinControl = async (id) => {
    await page.evaluate((pid) => {
      const node = document.querySelector(
        `[data-card-control="pin"][data-card-id="${pid}"]`,
      );
      if (!node) throw new Error(`no pin control for ${pid}`);
      node.click();
    }, id);
    await page.waitForTimeout(220);
  };
  const resetBefore = await resetProbe();
  await clickPinControl('openai');
  const resetPinned = await resetProbe();
  await clickPinControl('openai');
  const resetUnpinned = await resetProbe();
  check('reset-honesty-after-pin-unpin',
    resetBefore.disabled === true
      && resetPinned.disabled === false
      && resetUnpinned.disabled === true
      && resetUnpinned.stored
      && resetUnpinned.stored.pinned.length === 0
      && resetUnpinned.stored.order.length === 0
      && resetUnpinned.list.join(',') === ALL.join(','),
    { before: resetBefore, pinned: resetPinned, unpinned: resetUnpinned });

  // A provider discovered by a later poll must not be frozen behind the pin
  // snapshot. `cursor` moves to the front of the payload while `openai` is
  // pinned; the unpinned list must follow the payload, and the matching unpin
  // must leave no custom order behind.
  const withoutCursor = {
    ...fixture,
    providers: Object.fromEntries(
      Object.entries(fixture.providers).filter(([id]) => id !== 'cursor'),
    ),
  };
  const cursorFirst = {
    ...fixture,
    providers: {
      cursor: fixture.providers.cursor,
      ...withoutCursor.providers,
    },
  };
  await page.evaluate((value) => {
    window.__setFixture(value);
    window.__rerender();
  }, withoutCursor);
  await page.waitForTimeout(280);
  await page.evaluate(() => window.__setLayout({ pinned: [], order: [] }));
  await page.waitForTimeout(200);
  await clickPinControl('openai');
  const pinSnapshot = await resetProbe();
  await page.evaluate((value) => {
    window.__setFixture(value);
    window.__rerender();
  }, cursorFirst);
  await page.waitForTimeout(320);
  const afterDiscovery = await resetProbe();
  await clickPinControl('openai');
  const afterUnpin = await resetProbe();
  check('reset-honesty-new-provider-not-frozen',
    pinSnapshot.stored
      && pinSnapshot.stored.order.length === 0
      && pinSnapshot.stored.pinSlots
      && pinSnapshot.stored.pinSlots.openai !== undefined
      && afterDiscovery.list[0] === 'cursor'
      && afterDiscovery.list.join(',')
        === ['cursor', 'anthropic', 'deepseek', 'gemini', 'openrouter', 'ollama'].join(',')
      && afterUnpin.list.join(',')
        === ['cursor', 'anthropic', 'deepseek', 'openai', 'gemini', 'openrouter', 'ollama'].join(',')
      && afterUnpin.disabled === true
      && afterUnpin.stored
      && afterUnpin.stored.order.length === 0,
    { pinSnapshot, afterDiscovery, afterUnpin });
  await page.evaluate((value) => {
    window.__setFixture(value);
    window.__rerender();
  }, fixture);
  await page.waitForTimeout(240);

  // A stored rank for an absent provider is a real preference: normalizing the
  // materialised default must not drop it, and Reset must stay enabled.
  await page.evaluate((ids) => {
    window.__setLayout({ pinned: [], order: ['ghost', ...ids] });
  }, ALL);
  await page.waitForTimeout(220);
  await clickPinControl('openai');
  await clickPinControl('openai');
  const resetKept = await resetProbe();
  check('reset-honesty-keeps-absent-slot',
    resetKept.disabled === false
      && resetKept.stored
      && resetKept.stored.order[0] === 'ghost',
    resetKept);

  // ---- keyboard focus stays visible on every layout control ----
  // The host resets `:focus,:focus-visible{outline:none}` globally. These
  // controls carry no host focus utility, so the plugin must paint its own.
  await page.reload();
  await page.waitForSelector('[data-card-edit]');
  await page.waitForTimeout(300);
  // The grouped card's account buttons are plain <button>s with plugin
  // chrome too. The host's global focus reset strips their outline, so they
  // need the same plugin-supplied ring as the layout controls.
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  });
  let accountFocus = null;
  for (let i = 0; i < 30 && !accountFocus; i += 1) {
    await page.keyboard.press('Tab');
    accountFocus = await page.evaluate(() => {
      const a = document.activeElement;
      if (!a || !a.hasAttribute || !a.hasAttribute('data-card-account')) return null;
      const s = getComputedStyle(a);
      return {
        id: a.getAttribute('data-card-account'),
        focusVisible: a.matches(':focus-visible'),
        outlineStyle: s.outlineStyle,
        outlineWidth: parseFloat(s.outlineWidth),
        outlineColor: s.outlineColor,
      };
    });
  }
  check('focus-visible-grouped-account',
    accountFocus && accountFocus.focusVisible
      && accountFocus.outlineStyle === 'solid'
      && accountFocus.outlineWidth >= 2,
    accountFocus);

  // A keyboard account switch remounts ProviderRow (its key is the selected
  // login), so the focused button is destroyed. Focus must land on the newly
  // selected account button, not fall back to <body>, and the explicit
  // selection must survive the remount.
  await page.evaluate(() => {
    const button = document.querySelector('[data-card-account="anthropic:work"]');
    if (!button) throw new Error('grouped Work account button missing');
    button.focus();
  });
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);
  const keyboardAccount = await page.evaluate(() => {
    const active = document.activeElement;
    const selected = document.querySelector('[data-provider="anthropic:work"]');
    const rect = selected ? selected.getBoundingClientRect() : null;
    const style = active ? getComputedStyle(active) : null;
    return {
      account: active && active.getAttribute ? active.getAttribute('data-card-account') : null,
      pressed: active && active.getAttribute ? active.getAttribute('aria-pressed') : null,
      focusVisible: Boolean(active && active.matches && active.matches(':focus-visible')),
      outlineStyle: style ? style.outlineStyle : null,
      outlineWidth: style ? parseFloat(style.outlineWidth) : 0,
      selectedCard: Boolean(selected),
      selectedVisible: Boolean(rect && rect.width > 0 && rect.height > 0),
    };
  });
  check('keyboard-account-switch-keeps-focus',
    keyboardAccount.account === 'anthropic:work'
      && keyboardAccount.pressed === 'true'
      && keyboardAccount.focusVisible
      && keyboardAccount.outlineStyle === 'solid'
      && keyboardAccount.outlineWidth >= 2
      && keyboardAccount.selectedCard && keyboardAccount.selectedVisible,
    keyboardAccount);

  await page.evaluate(() => window.__setLayout({ pinned: ['deepseek'], order: [] }));
  await page.waitForTimeout(200);
  await page.click('[data-card-edit]');
  await page.waitForTimeout(250);
  // Reset is disabled (and not tabbable) for the default layout, so give the
  // focus sweep a real custom layout while keeping every control present.
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  });
  const focusSeen = {};
  for (let i = 0; i < 40; i += 1) {
    await page.keyboard.press('Tab');
    const item = await page.evaluate(() => {
      const a = document.activeElement;
      if (!a) return null;
      const key = a.hasAttribute('data-card-edit') ? 'edit'
        : a.hasAttribute('data-card-sort') ? `sort-${a.getAttribute('data-card-sort')}`
          : a.hasAttribute('data-card-reset') ? 'reset'
            : a.getAttribute('data-card-control') || null;
      if (!key) return null;
      const s = getComputedStyle(a);
      return {
        key,
        focusVisible: a.matches(':focus-visible'),
        outlineStyle: s.outlineStyle,
        outlineWidth: parseFloat(s.outlineWidth),
        outlineColor: s.outlineColor,
      };
    });
    if (item) focusSeen[item.key] = item;
  }
  const focusRequired = ['edit', 'sort-asc', 'sort-desc', 'reset', 'pin', 'up', 'down'];
  const focusMissing = focusRequired.filter((key) => !focusSeen[key]);
  const focusBad = Object.entries(focusSeen).filter(([, item]) =>
    !item.focusVisible || item.outlineStyle !== 'solid' || item.outlineWidth < 2);
  check('focus-visible-layout-controls',
    focusMissing.length === 0 && focusBad.length === 0,
    { missing: focusMissing, bad: focusBad, seen: focusSeen });

  // ---- A sorted band with a stale stored rank: drag follows the drawn band ----
  // The stored order still starts with OpenAI, while the A-Z sort (persisted
  // after OpenAI was hidden) draws OpenAI last. A drag must use the drawn
  // neighbours; swapping stored indices teleported OpenAI to the front and
  // left the new custom order matching a band the user never saw.
  await page.evaluate(() => {
    window.__seedLayout({
      pinned: [],
      order: ['openai', 'anthropic', 'deepseek', 'gemini'],
      sort: 'asc',
    });
    document.getElementById('pane').style.width = '300px';
    document.getElementById('pane').style.height = '900px';
  });
  await page.waitForTimeout(350);
  const sortedBefore = await state();
  check('sorted-band-drawn-alphabetically',
    sortedBefore.listUI.join(',') ===
      'anthropic,cursor,deepseek,gemini,ollama,openai,openrouter',
    sortedBefore);
  // Dropping a card into the slot it already owns is a no-op: the persisted
  // sort direction must survive it.
  await mouseDrag('[data-card-shell="anthropic"]', async () => {
    const target = await box('[data-card-shell="cursor"]');
    await page.mouse.move(target.x + target.width / 2, target.y + 12, { steps: 10 });
    await page.waitForTimeout(150);
    return state();
  });
  await page.mouse.up();
  await page.waitForTimeout(250);
  const sortedNoop = await state();
  check('sorted-noop-drag-keeps-sort',
    sortedNoop.stored.sort === 'asc'
      && sortedNoop.listUI.join(',') === sortedBefore.listUI.join(','),
    sortedNoop);
  // A real move: Anthropic down past DeepSeek.
  await mouseDrag('[data-card-shell="anthropic"]', async () => {
    const target = await box('[data-card-shell="deepseek"]');
    const list = await page.evaluate(() => ({
      bottom: document.querySelector('[data-quota-scroll]').getBoundingClientRect().bottom,
    }));
    const y = Math.min(target.y + target.height - 22, list.bottom - 42);
    await page.mouse.move(target.x + target.width / 2, y, { steps: 12 });
    await page.waitForTimeout(150);
    return state();
  });
  await page.mouse.up();
  await page.waitForTimeout(300);
  const sortedAfter = await state();
  check('sorted-drag-uses-the-drawn-band',
    sortedAfter.listUI.join(',') ===
      'cursor,deepseek,anthropic,gemini,ollama,openai,openrouter'
      && sortedAfter.stored.sort === undefined
      && sortedAfter.stored.order.join(',') === sortedAfter.listUI.join(','),
    sortedAfter);

  // ---- 160px-tall edit pane: compact chrome keeps both bands usable ----
  // A 160px pane is below the 180/200px cases above and adds the edit
  // toolbar. Without the compact layout the body collapsed to 28px (a 10px
  // pinned viewport, a 6px list) and the pin control sat entirely outside the
  // shelf viewport. Compact chrome hides the footer and tightens padding, so
  // the first card's header and its controls stay reachable in the pinned
  // band while the list keeps a scrollable slice of its own.
  await page.reload();
  await page.waitForSelector('[data-card-edit]');
  await page.waitForTimeout(300);
  await page.click('[data-card-edit]');
  await page.waitForTimeout(220);
  await size(300, 160, ['anthropic', 'deepseek'],
    ['openai', 'gemini', 'cursor', 'openrouter', 'ollama']);
  await page.waitForTimeout(500);
  const ultraShort = await page.evaluate(() => {
    const pane = document.getElementById('pane');
    const root = document.querySelector('[data-quota-layout]');
    const body = document.querySelector('[data-quota-body]');
    const inner = document.querySelector('[data-pinned-shelf-scroll]');
    const frame = document.querySelector('[data-pinned-shelf-frame]');
    const list = document.querySelector('[data-quota-scroll]');
    const tools = document.querySelector('[data-card-layout-tools]');
    const shell = document.querySelector('[data-pinned-shelf] [data-card-shell]');
    const pin = shell ? shell.querySelector('[data-card-control="pin"]') : null;
    const card = inner ? inner.querySelector('[data-provider]') : null;
    const i = inner.getBoundingClientRect();
    const p = pin ? pin.getBoundingClientRect() : null;
    const c = card ? card.getBoundingClientRect() : null;
    const b = body.getBoundingClientRect();
    const t = tools ? tools.getBoundingClientRect() : null;
    const shelfBefore = inner.scrollTop;
    inner.scrollTop = 12;
    const shelfScrolled = inner.scrollTop;
    inner.scrollTop = shelfBefore;
    const listBefore = list.scrollTop;
    list.scrollTop = 12;
    const listScrolled = list.scrollTop;
    list.scrollTop = listBefore;
    return {
      paneWidth: pane.getBoundingClientRect().width,
      rootWidth: root.getBoundingClientRect().width,
      body: b.height,
      inner: i.height,
      list: list.clientHeight,
      frame: frame.getBoundingClientRect().height,
      toolsBottom: t ? t.bottom : null,
      bodyTop: b.top,
      pinTop: p ? p.top : null,
      pinBottom: p ? p.bottom : null,
      innerTop: i.top,
      innerBottom: i.bottom,
      cardTextVisible: c ? Math.min(c.bottom, i.bottom) - Math.max(c.top, i.top) : 0,
      footer: Boolean(document.querySelector('[data-quota-checked]')),
      shells: document.querySelectorAll('[data-card-shell]').length,
      shelfScrolled,
      listScrolled,
      overflow: root.scrollHeight - root.clientHeight,
    };
  });
  check('short-pane-160-edit-usable',
    ultraShort.paneWidth === 300 && ultraShort.rootWidth === 300
      && ultraShort.body >= 80 && ultraShort.inner >= 36
      && ultraShort.list >= 28 && Math.abs(ultraShort.frame - ultraShort.inner) <= 0.5
      && ultraShort.cardTextVisible >= 30 && ultraShort.shells >= 2
      && ultraShort.footer === false
      && ultraShort.toolsBottom <= ultraShort.bodyTop + 0.5
      && ultraShort.overflow <= 1, ultraShort);
  check('short-pane-160-edit-controls-and-bands-reachable',
    ultraShort.shelfScrolled > 0 && ultraShort.listScrolled > 0
      && ultraShort.pinTop >= ultraShort.innerTop - 0.5
      && ultraShort.pinBottom <= ultraShort.innerBottom + 0.5,
    ultraShort);
  await shot('shelf-160-edit');

  check('no-page-errors', errors.length === 0, errors);
  await browser.close();
  fs.rmSync(OUT, { recursive: true, force: true });
  console.log(JSON.stringify({ checks, errors }, null, 2));
})().catch((e) => {
  console.error(e.stack || String(e));
  process.exit(1);
});
