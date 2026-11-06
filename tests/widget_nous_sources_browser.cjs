// Real-Chromium visual/contract check for the two-source Nous card.
//
// Renders the real `desktop/plugin.js` QuotaPane with the host's compiled CSS
// and a synthetic, realistic Free Billing snapshot plus a cached master row
// carrying the old JWT rate/spend facts. Asserts the pane shows Billing's
// `Free` plan together with the cached rate limits/tool pool, and that a
// stale paid cached row never leaks paid facts into a Billing Free card.
//
//   QUOTA_DESKTOP_DIR=/path/to/apps/desktop node tests/widget_nous_sources_browser.cjs
//   QUOTA_SHOTS=/some/dir node tests/widget_nous_sources_browser.cjs
//
// Synthetic data only; no credentials, no network. Prints a JSON report.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');

const ROOT = path.resolve(__dirname, '..');
const HOST = process.env.QUOTA_DESKTOP_DIR
  || path.join(os.homedir(), '.hermes', 'hermes-agent', 'apps', 'desktop');
const OUT = fs.mkdtempSync(path.join(os.tmpdir(), 'quota-nous-browser-'));
const req = createRequire(path.join(HOST, 'package.json'));
const { build } = req('esbuild');
const { chromium } = req('playwright');

const freeMaster = {
  age_s: 8,
  providers: {
    nous: {
      label: 'nous',
      plan: 'Free',
      windows: [],
      details: [
        'Free tier - free models only',
        'Rate limits: 50 RPM · 500k TPM · 2.1k RPH',
        'Tool pool: browser-use, firecrawl',
      ],
      detail_facts: {
        free_tier: 'Free tier - free models only',
        rate_limits: 'Rate limits: 50 RPM · 500k TPM · 2.1k RPH',
        tool_pool: 'Tool pool: browser-use, firecrawl',
      },
      access_state: 'free',
      api_calls_available: null,
      account_org_key: '1523bbad0010557d',
    },
    anthropic: { windows: [] },
  },
};

const freeMasterNoPool = {
  age_s: 8,
  providers: {
    nous: {
      ...freeMaster.providers.nous,
      details: ['Free tier - free models only'],
      detail_facts: { free_tier: 'Free tier - free models only' },
    },
  },
};

const freeMasterStalePool = {
  age_s: 4000,
  providers: {
    nous: {
      ...freeMaster.providers.nous,
    },
  },
};

const freeMasterNoRates = {
  age_s: 8,
  providers: {
    nous: {
      ...freeMaster.providers.nous,
      details: ['Free tier - free models only'],
      detail_facts: { free_tier: 'Free tier - free models only' },
    },
  },
};

const plusMaster = {
  age_s: 8,
  providers: {
    nous: {
      label: 'nous',
      plan: 'Plus',
      windows: [],
      details: ['Rate limits: 400 RPM · 4M TPM · 16.8k RPH'],
      detail_facts: {
        rate_limits: 'Rate limits: 400 RPM · 4M TPM · 16.8k RPH',
      },
      access_state: 'active',
      api_calls_available: null,
      account_org_key: '1523bbad0010557d',
    },
  },
};

// The live Plus account snapshot after the CLI fix: four usage-window
// gauges, the purchased credit balance and the subscription period facts.
// `account_org_key` is the FNV-1a digest of "synthetic-org".
const plusWindowMaster = {
  age_s: 8,
  providers: {
    nous: {
      label: 'nous',
      plan: 'Hermes Plus',
      windows: [
        { label: 'Daily', used_percent: 0, reset_at: '2026-11-07T13:09:44Z' },
        { label: 'Weekly', used_percent: 0, reset_at: '2026-11-13T13:09:44Z' },
        { label: 'Monthly', used_percent: 0, reset_at: '2026-12-06T13:00:47Z' },
        { label: 'Large models (Monthly)', used_percent: 0, reset_at: '2026-12-06T13:00:47Z' },
      ],
      details: [
        'Cycle ends: 2026-12-06T13:00:47.000Z',
        'Spend this period: $0.00 (no cap reported)',
      ],
      detail_facts: {
        cycle_ends: 'Cycle ends: 2026-12-06T13:00:47.000Z',
        spend: 'Spend this period: $0.00 (no cap reported)',
      },
      account_balances: [
        { currency: 'USD', total_balance: '9.99', label: 'Account balance' },
      ],
      access_state: 'active',
      api_calls_available: true,
      account_org_key: '1523bbad0010557d',
    },
  },
};

const plusWindowMasterStale = {
  ...plusWindowMaster,
  age_s: 4000,
  providers: { nous: { ...plusWindowMaster.providers.nous } },
};

const plusWindowMasterWrongOrg = {
  ...plusWindowMaster,
  providers: {
    nous: {
      ...plusWindowMaster.providers.nous,
      account_org_key: '0000000000000000',
    },
  },
};

const plusWindowBilling = {
  ok: true,
  logged_in: true,
  free_tier_account: false,
  free_tier_model: null,
  usage: { ok: true, available: false },
  plan_usage: {
    kind: 'usage_window',
    plan: 'Hermes Plus',
    gauges: [
      { label: 'day', left_fraction: 1.0, style: 'green',
        resets_at_ms: Date.parse('2026-11-07T13:09:44Z'), scope: 'all', title: 'Today' },
      { label: 'week', left_fraction: 1.0, style: 'green',
        resets_at_ms: Date.parse('2026-11-13T13:09:44Z'), scope: 'all', title: 'This week' },
      { label: 'month', left_fraction: 1.0, style: 'green',
        resets_at_ms: Date.parse('2026-12-06T13:00:47Z'), scope: 'all', title: 'This month' },
      { label: 'top_tier', left_fraction: 1.0, style: 'green',
        resets_at_ms: Date.parse('2026-12-06T13:00:47Z'), scope: 'top_tier',
        title: 'Large models (monthly)' },
    ],
    top_tier_model_ids: [],
    subscription_model_ids: [],
    featured_model_ids: [
      'deepseek/deepseek-v4-flash-0731',
      'z-ai/glm-5.3',
      'stepfun/step-3.7-flash:free',
    ],
    paused_reason: null,
    credits_pay: false,
  },
  monthly_cap: {
    limit_usd: '1000', limit_display: '$1000',
    spent_this_month_usd: '0', spent_display: '$0',
    is_default_ceiling: true,
  },
  keep_going_on_credits: false,
  // The live account wallet, independent of the unavailable dollar-credit
  // `usage` model. Billing reports it on every account state.
  balance_display: '$9.99',
  balance_usd: '9.993238',
  org_slug: 'org_synthetic',
  org: { id: 'org_synthetic', slug: 'synthetic-org', name: 'SECRET-ORG' },
  org_name: 'SECRET-ORG',
  card: { brand: 'Visa', last4: '9999' },
  payment_method: { kind: 'card', brand: 'Visa', last4: '9999' },
};

// A paused Plus plan: the cached row's total still counts the included
// subscription credits the pause withholds ($18.00), while Billing's own
// wallet is explicitly empty. The card must keep Billing's $0.00.
const pausedWindowMaster = {
  ...plusWindowMaster,
  providers: {
    nous: {
      ...plusWindowMaster.providers.nous,
      access_state: 'paused',
      api_calls_available: true,
      account_balances: [
        { currency: 'USD', total_balance: '18.00', label: 'Account balance' },
      ],
    },
  },
};

const pausedWindowBilling = {
  ...plusWindowBilling,
  balance_display: '$0.00',
  balance_usd: '0',
  keep_going_on_credits: true,
  plan_usage: {
    ...plusWindowBilling.plan_usage,
    paused_reason: 'payment_past_due',
    credits_pay: true,
  },
};

// The real live Plus `billing.state` shape (sanitized): the dollar-credit
// model is unavailable for a usage-window plan, while `plan_usage` carries
// the four server-id gauges and their resets.
const freeBilling = {
  ok: true,
  logged_in: true,
  free_tier_account: false,
  free_tier_model: null,
  usage: {
    ok: true,
    available: true,
    status: 'depleted',
    plan_name: 'Free',
    renews_display: 'Dec 3, 2026',
  },
  plan_usage: null,
  // Decoys: the card must never render or persist these.
  org_slug: 'org_synthetic',
  org: { id: 'org_synthetic', slug: 'synthetic-org', name: 'SECRET-ORG' },
  org_name: 'SECRET-ORG',
  card: { brand: 'Visa', last4: '9999' },
  payment_method: { kind: 'card', brand: 'Visa', last4: '9999' },
};

const freeTopupBilling = {
  ok: true,
  logged_in: true,
  free_tier_account: false,
  free_tier_model: null,
  usage: {
    ok: true,
    available: true,
    status: 'healthy',
    plan_name: 'Free',
    renews_display: 'Jan 1, 2027',
    subscription_remaining_display: '$0.00',
    topup_remaining_display: '$10.00',
    total_spendable_display: '$10.00',
    has_topup: true,
    topup_bar: {
      kind: 'topup', remaining_display: '$10.00', total_display: '$10.00',
      spent_display: '$0.00', pct_used: null, fill_fraction: 1.0,
    },
  },
  plan_usage: null,
  org_slug: 'org_synthetic',
  org: { id: 'org_synthetic', slug: 'synthetic-org', name: 'SECRET-ORG' },
  org_name: 'SECRET-ORG',
  card: { brand: 'Visa', last4: '9999' },
};

const freeTopupMaster = {
  age_s: 8,
  providers: {
    nous: {
      label: 'nous',
      plan: 'Free',
      windows: [],
      details: [
        'Free tier - paid models via purchased credits',
        'Rate limits: 50 RPM · 500k TPM · 2.1k RPH',
        'Tool pool: browser-use, firecrawl',
      ],
      detail_facts: {
        free_tier: 'Free tier - paid models via purchased credits',
        rate_limits: 'Rate limits: 50 RPM · 500k TPM · 2.1k RPH',
        tool_pool: 'Tool pool: browser-use, firecrawl',
      },
      account_balances: [
        { currency: 'USD', total_balance: '10.00', label: 'Account balance' },
      ],
      access_state: 'free',
      api_calls_available: true,
      account_org_key: '1523bbad0010557d',
    },
  },
};

const paidBilling = {
  ok: true,
  logged_in: true,
  free_tier_account: false,
  free_tier_model: null,
  usage: {
    ok: true,
    available: true,
    status: 'healthy',
    plan_name: 'Pro',
    renews_display: 'Dec 3, 2026',
    plan_bar: {
      kind: 'plan', remaining_display: '$14.00', total_display: '$20.00',
      spent_display: '$6.00', pct_used: 30, fill_fraction: 0.7,
    },
    topup_bar: {
      kind: 'topup', remaining_display: '$12.00', total_display: '$12.00',
      spent_display: '$0.00', pct_used: null, fill_fraction: 1.0,
    },
    subscription_remaining_display: '$14.00',
    topup_remaining_display: '$12.00',
    total_spendable_display: '$26.00',
    has_topup: true,
  },
  plan_usage: null,
  monthly_cap: null,
  org_slug: 'org_synthetic',
  org: { id: 'org_synthetic', slug: 'synthetic-org', name: 'SECRET-ORG' },
  org_name: 'SECRET-ORG',
  card: { brand: 'Visa', last4: '9999' },
};

const stalePaidMaster = {
  age_s: 8,
  providers: {
    nous: {
      label: 'nous',
      plan: 'Tier 9',
      windows: [{ label: 'Stale paid window', used_percent: 5 }],
      details: [
        'Subscription credits: $14.00',
        'Total usable: $26.00',
        'Spend this period: $6.00',
        'Rate limits: 50 RPM · 500k TPM · 2.1k RPH',
      ],
      detail_facts: {
        subscription_credits: 'Subscription credits: $14.00',
        total_usable: 'Total usable: $26.00',
        spend: 'Spend this period: $6.00',
        rate_limits: 'Rate limits: 50 RPM · 500k TPM · 2.1k RPH',
      },
      access_state: 'active',
      api_calls_available: null,
      account_org_key: '1523bbad0010557d',
    },
  },
};

const sdk = `
import React from 'react';
export const atom = (value) => { const subs = new Set(); return { get: () => value, set: (next) => { if (Object.is(next, value)) return; value = next; for (const fn of [...subs]) fn(); }, subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); } }; };
export const useValue = (a) => React.useSyncExternalStore(a.subscribe, a.get, a.get);
export const useQuery = (options) => {
  const key = (options && options.queryKey) || [];
  const billing = key[1] === 'billing';
  return { data: billing ? window.__billingFixture : window.__quotaFixture, isError: false, isPlaceholderData: false, isLoading: false };
};
export const useQueryClient = () => ({ invalidateQueries: () => {}, setQueryData: () => {} });
export const queryClient = { getQueriesData: () => [], getQueryCache: () => ({ subscribe: () => () => {} }) };
export const useMutation = () => ({});
export const usePluginI18n = () => window.__t;
export const useI18n = () => ({ locale: 'en' });
export const cn = (...args) => args.filter(Boolean).join(' ');
export const fmtDayTime = new Intl.DateTimeFormat('en-US');
export const host = { state: { focusedSessionOwner: atom({connectionId: 'local', profile: 'work'}) }, profileRoutes: async () => [], requestProfile: async () => ({code: 0, output: '{}'}), notify: () => {} };
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
  const names = fs.readdirSync(assets).filter((f) => f.endsWith('.css'));
  const main = names.filter((f) => /^main-.*\.css$/.test(f)).sort().pop();
  const name = main || names.sort().pop();
  if (!name) throw new Error(`no compiled CSS under ${assets}`);
  return path.join(assets, name);
}

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
    + '\nexport { QuotaPane };';
  await build({
    stdin: {
      contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import plugin, { QuotaPane } from 'quota-under-test';
const storage = new Map();
window.__store = storage;
const t = (key, ...args) => { const table = window.__bundles && (window.__bundles.en || {}); const value = table && key in table ? table[key] : key; return typeof value === 'function' ? value(...args) : value; };
window.__t = t;
const ctx = { storage: { get: (k) => storage.get(k), set: (k, v) => storage.set(k, v), remove: (k) => storage.delete(k) }, i18n: { register: (b) => { window.__bundles = b; } }, os: { notify: () => {}, writeClipboard: async () => true }, register: () => () => {} };
plugin.register(ctx);
const root = createRoot(document.getElementById('root'));
const render = () => root.render(
  React.createElement('div', { id: 'pane', style: { width: '340px', height: '720px', display: 'flex' } },
    React.createElement(QuotaPane, {})),
);
window.__setFixtures = (quota, billing) => { window.__quotaFixture = quota; window.__billingFixture = billing; render(); };
render();
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
</head><body><div id="root"></div><script>window.__seedStorage=${JSON.stringify({ paneStyle: 'rings' })}; window.__quotaFixture=null; window.__billingFixture=null;</script><script src="bundle.js"></script></body></html>`);

  const browser = await chromium.launch({ headless: true, executablePath: chromiumPath() });
  const page = await browser.newPage({ viewport: { width: 460, height: 820 } });
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
  await page.waitForSelector('#pane');
  const checks = [];
  const check = (name, ok, detail) => {
    checks.push({ name, ok: Boolean(ok), detail });
    if (!ok) throw new Error(`${name}: ${JSON.stringify(detail)}`);
  };
  const nousText = async () => (await page.locator('[data-provider="nous"]').innerText()).replace(/\s+/g, ' ');
  const shots = process.env.QUOTA_SHOTS || '';
  const shot = async (name) => {
    if (!shots) return;
    fs.mkdirSync(shots, { recursive: true });
    await page.locator('[data-provider="nous"]').screenshot({ path: path.join(shots, `${name}.png`) });
  };

  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [freeMaster, freeBilling]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Free'));
  const free = await nousText();
  check('free-plan-from-billing', free.includes('Free'), free);
  check('free-rate-limits-merged-from-account',
    free.includes('Rate limits: 50 RPM · 500k TPM · 2.1k RPH'), free);
  check('free-no-published-ceiling-when-live-claim-exists', !free.includes('Rate ceiling'), free);
  check('free-tool-pool-from-master', free.includes('Tool pool: browser-use, firecrawl'), free);
  check('free-tool-pool-once', (free.match(/Tool pool/g) || []).length === 1, free);
  check('free-has-no-stale-paid-plan', !free.includes('Tier 9'), free);
  check('free-has-no-stale-paid-window', !free.includes('Stale paid window'), free);
  check('free-has-no-depleted-warning', !free.includes('access depleted'), free);
  check('private-billing-fields-not-rendered', !/SECRET-ORG|9999|payment_method/.test(free), free);
  await shot('nous-free-billing-with-master-facts');

  // With no live account claim, an unambiguous Billing plan shows the
  // published tier ceiling and marks it as published.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [freeMasterNoRates, freeBilling]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Rate ceiling'));
  const freePublished = await nousText();
  check('free-published-ceiling-when-live-claim-absent',
    freePublished.includes('Rate ceiling: 50 RPM / 500k TPM (published)'), freePublished);
  check('free-published-does-not-claim-live-rates', !freePublished.includes('Rate limits'), freePublished);

  // A disabled or stale cached pool never produces the line. The pool is
  // emitted by the account fetcher only on a live `tool_access.enabled: true`;
  // absence is disabled/unknown, and an age beyond the tool-pool bound is
  // stale, not a current entitlement.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [freeMasterNoPool, freeBilling]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Free'));
  const noPool = await nousText();
  check('disabled-tool-pool-not-shown', !noPool.includes('Tool pool'), noPool);

  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [freeMasterStalePool, freeBilling]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Free'));
  const stalePool = await nousText();
  check('stale-tool-pool-not-shown', !stalePool.includes('Tool pool'), stalePool);

  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [stalePaidMaster, freeBilling]);
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-provider="nous"]');
    return el && !el.innerText.includes('Tier 9');
  });
  const stale = await nousText();
  check('stale-paid-master-hidden-under-free-billing',
    !/Tier 9|Stale paid window|Rate limits|Total usable|Account balance|Spend this period|Tool pool/.test(stale), stale);
  check('free-billing-uses-its-own-published-ceiling',
    stale.includes('Rate ceiling: 50 RPM / 500k TPM (published)'), stale);
  await shot('nous-free-billing-stale-paid-master-hidden');

  // Billing failure falls back to the whole cached row, so an enabled pool
  // remains visible without being mixed with a Billing plan.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [freeMaster, null]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Tool pool'));
  const fallback = await nousText();
  check('tool-pool-survives-billing-failure', fallback.includes('Tool pool: browser-use, firecrawl'), fallback);
  check('rate-limits-survive-billing-failure',
    fallback.includes('Rate limits: 50 RPM · 500k TPM · 2.1k RPH'), fallback);
  check('tool-pool-fallback-is-single-source', !fallback.includes('Total usable'), fallback);

  // Free plan funded by a $10 purchased wallet: one prominent Account
  // balance; the plan stays Free; the copy says paid models are available via
  // those purchased credits. There is no separate top-up line, zero
  // subscription line or padded renewal date. The cached master row
  // contributes only its own rate/pool facts.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [freeTopupMaster, freeTopupBilling]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Account balance'));
  const freeTopup = await nousText();
  check('free-topup-plan-still-free', /Free/.test(freeTopup) && !/Tier 9/.test(freeTopup), freeTopup);
  check('free-topup-copy-names-purchased-credits',
    freeTopup.includes('Free tier - paid models via purchased credits') && !freeTopup.includes('free models only'), freeTopup);
  check('free-topup-no-component-line', !freeTopup.includes('Top-up credits'), freeTopup);
  check('free-topup-no-zero-subscription-component', !/Subscription credits|\$0\.00/.test(freeTopup), freeTopup);
  check('free-topup-account-balance-visible', freeTopup.includes('Account balance') && freeTopup.includes('$10.00 USD'), freeTopup);
  check('free-topup-available-yes', freeTopup.includes('API calls available: yes'), freeTopup);
  check('free-topup-no-cycle-end', !freeTopup.includes('Cycle ends'), freeTopup);
  check('free-topup-tool-pool-merged', freeTopup.includes('Tool pool: browser-use, firecrawl'), freeTopup);
  check('free-topup-rate-limits-merged',
    freeTopup.includes('Rate limits: 50 RPM · 500k TPM · 2.1k RPH'), freeTopup);
  check('free-topup-no-published-ceiling', !freeTopup.includes('Rate ceiling'), freeTopup);
  check('free-topup-no-duplicate-balance', (freeTopup.match(/Account balance/g) || []).length === 1, freeTopup);
  check('free-topup-no-duplicate-tool-pool', (freeTopup.match(/Tool pool/g) || []).length === 1, freeTopup);
  await shot('nous-free-topup-account-balance');

  // Paid plan: one Account balance from Billing; the renewal retained.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [stalePaidMaster, paidBilling]);
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-provider="nous"]');
    return el && el.innerText.includes('Account balance');
  });
  const paid = await nousText();
  check('paid-plan-from-billing', paid.includes('Pro'), paid);
  check('paid-account-balance-visible', paid.includes('Account balance') && paid.includes('$26.00 USD'), paid);
  check('paid-renewal-retained', paid.includes('Cycle ends: Dec 3, 2026'), paid);
  check('paid-balance-not-duplicated', (paid.match(/Account balance/g) || []).length === 1, paid);
  check('paid-no-component-lines', !/subscription credits left|Top-up credits/.test(paid), paid);
  check('paid-rate-limits-not-merged', !paid.includes('Rate limits'), paid);
  check('paid-unknown-plan-has-no-published-ceiling', !paid.includes('Rate ceiling'), paid);
  check('paid-free-tool-pool-not-merged', !paid.includes('Tool pool'), paid);
  check('paid-private-billing-fields-not-rendered', !/SECRET-ORG|9999|payment_method/.test(paid), paid);
  await shot('nous-paid-account-balance');

  // Same-org, same-plan paid account: the live JWT line (including RPH) is the
  // one cached fact allowed through beside Billing.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [plusMaster, {
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: 'healthy', plan_name: 'Plus',
      renews_display: 'Dec 3, 2026',
      total_spendable_display: '$26.00',
    },
    plan_usage: null,
    org_slug: 'org_synthetic',
  org: { id: 'org_synthetic', slug: 'synthetic-org', name: 'SECRET-ORG' },
    card: { brand: 'Visa', last4: '9999' },
  }]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Plus'));
  const plus = await nousText();
  check('paid-live-rate-limits-merged',
    plus.includes('Rate limits: 400 RPM · 4M TPM · 16.8k RPH'), plus);
  check('paid-live-rate-limits-keep-rph', plus.includes('16.8k RPH'), plus);
  check('paid-live-rate-limits-no-published-duplicate', !plus.includes('Rate ceiling'), plus);
  check('paid-private-billing_fields_not_rendered_after_merge',
    !/SECRET-ORG|9999/.test(plus), plus);
  await shot('nous-paid-live-rates-with-billing');

  // Live Plus usage-window shape: Billing has the plan and four gauges but
  // `usage.available: false`; the compatible fresh account row carries one
  // Account balance plus renewal/spend facts. The card must use the exact
  // window names, not the server's raw ids, and must not invent rate limits,
  // a tool pool or component credit lines (absent/disabled on this account).
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing),
    [plusWindowMaster, plusWindowBilling]);
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-provider="nous"]');
    return el && el.innerText.includes('Hermes Plus') && el.innerText.includes('Account balance');
  });
  const plusWindow = await nousText();
  for (const label of ['Daily', 'Weekly', 'Monthly', 'Large models (Monthly)']) {
    check(`plus-window-label-${label.toLowerCase().replace(/[^a-z]+/g, '-')}`,
      plusWindow.includes(label), plusWindow);
  }
  check('plus-window-raw-ids-not-rendered',
    !/\bday\b|\bweek\b|top_tier/.test(plusWindow), plusWindow);
  check('plus-window-account-balance', plusWindow.includes('Account balance') &&
    plusWindow.includes('$9.99 USD'), plusWindow);
  check('plus-window-balance-once',
    (plusWindow.match(/Account balance/g) || []).length === 1, plusWindow);
  check('plus-window-no-component-lines',
    !plusWindow.includes('Top-up credits') &&
    !plusWindow.includes('Subscription credits'), plusWindow);
  check('plus-window-no-automatic-wallet-claim',
    !plusWindow.includes('paid models via purchased credits') &&
    !plusWindow.includes('credits still apply'), plusWindow);
  check('plus-window-renewal', plusWindow.includes('Cycle ends: 2026-12-06T13:00:47.000Z'), plusWindow);
  check('plus-window-spend-normalized',
    plusWindow.includes('Spend this period: $0.00') && !plusWindow.includes('no cap reported'), plusWindow);
  check('plus-window-no-rate-limits', !plusWindow.includes('Rate limits'), plusWindow);
  check('plus-window-no-tool-pool', !plusWindow.includes('Tool pool'), plusWindow);
  check('plus-window-private-fields-hidden', !/SECRET-ORG|9999|payment_method/.test(plusWindow), plusWindow);
  await shot('nous-live-plus-window-account-balance');

  // Billing-only Plus: no cached account row at all. The Account balance is
  // Billing's own wallet (`balance_display`), so it must still be visible;
  // renewal/spend facts are absent because the cache is absent.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing),
    [{ age_s: 20, providers: { anthropic: { windows: [] } } }, plusWindowBilling]);
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-provider="nous"]');
    return el && el.innerText.includes('Hermes Plus') && el.innerText.includes('Account balance');
  });
  const plusBillingOnly = await nousText();
  check('plus-billing-only-account-balance',
    plusBillingOnly.includes('Account balance') && plusBillingOnly.includes('$9.99 USD'),
    plusBillingOnly);
  check('plus-billing-only-balance-once',
    (plusBillingOnly.match(/Account balance/g) || []).length === 1, plusBillingOnly);
  for (const label of ['Daily', 'Weekly', 'Monthly', 'Large models (Monthly)']) {
    check(`plus-billing-only-label-${label.toLowerCase().replace(/[^a-z]+/g, '-')}`,
      plusBillingOnly.includes(label), plusBillingOnly);
  }
  check('plus-billing-only-no-cached-facts',
    !plusBillingOnly.includes('Cycle ends:') && !plusBillingOnly.includes('Spend this period'),
    plusBillingOnly);
  check('plus-billing-only-no-component-lines',
    !plusBillingOnly.includes('Top-up credits') &&
    !plusBillingOnly.includes('Subscription credits'), plusBillingOnly);

  // A stale account cache contributes no renewal/spend facts, but Billing's
  // own wallet must still paint the Account balance.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing),
    [plusWindowMasterStale, plusWindowBilling]);
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-provider="nous"]');
    return el && el.innerText.includes('Hermes Plus') && el.innerText.includes('Account balance');
  });
  const plusStaleCache = await nousText();
  check('plus-stale-cache-billing-balance',
    plusStaleCache.includes('Account balance') && plusStaleCache.includes('$9.99 USD'),
    plusStaleCache);
  check('plus-stale-cache-facts-withheld',
    !plusStaleCache.includes('Cycle ends:') && !plusStaleCache.includes('Spend this period'),
    plusStaleCache);
  check('plus-stale-cache-balance-once',
    (plusStaleCache.match(/Account balance/g) || []).length === 1, plusStaleCache);

  // A paused plan withholds its included allowance, yet the cached row's total
  // still counts those credits. Billing's explicitly empty wallet is the
  // spendable amount and must not be replaced by the cached $18.00.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing),
    [pausedWindowMaster, pausedWindowBilling]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('paused'));
  const pausedRendered = await nousText();
  check('paused-cache-total-not-rendered', !pausedRendered.includes('$18.00'), pausedRendered);
  check('paused-billing-wallet-rendered',
    pausedRendered.includes('$0.00 USD'), pausedRendered);
  check('paused-empty-wallet-not-claimed-available',
    !pausedRendered.includes('API calls available: yes'), pausedRendered);
  check('paused-balance-once',
    (pausedRendered.match(/Account balance/g) || []).length === 1, pausedRendered);
  await shot('nous-plus-paused-billing-wallet');

  // A mismatched organisation also withholds cached facts without taking the
  // Billing wallet with it.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing),
    [plusWindowMasterWrongOrg, plusWindowBilling]);
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-provider="nous"]');
    return el && el.innerText.includes('Hermes Plus') && el.innerText.includes('Account balance');
  });
  const plusWrongOrg = await nousText();
  check('plus-unmatched-cache-billing-balance',
    plusWrongOrg.includes('Account balance') && plusWrongOrg.includes('$9.99 USD'),
    plusWrongOrg);
  check('plus-unmatched-cache-facts-withheld',
    !plusWrongOrg.includes('Cycle ends:') && !plusWrongOrg.includes('Spend this period'),
    plusWrongOrg);
  await shot('nous-plus-billing-wallet-stale-unmatched');

  // A low balance maps to an unknown access state, but the exact paid plan
  // label still gets its documented published ceiling. The cached active row
  // must not supply a live rate line across that state mismatch.
  await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [plusMaster, {
    ok: true, logged_in: true, free_tier_account: false,
    usage: {
      ok: true, available: true, status: 'low', plan_name: 'Plus',
      total_spendable_display: '$3.40',
    },
    plan_usage: null,
    org_slug: 'org_synthetic',
  }]);
  await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Low balance'));
  const lowPaid = await nousText();
  check('low-paid-keeps-published-ceiling',
    lowPaid.includes('Rate ceiling: 400 RPM / 4M TPM (published)'), lowPaid);
  check('low-paid-does-not-merge-active-live-rates', !lowPaid.includes('Rate limits:'), lowPaid);
  await shot('nous-low-paid-published-ceiling');

  // Optional live-path fixture: a real sanitized `billing.state` response and
  // the real `quota status --json --cached` payload. Only values the plugin is
  // allowed to read are present; card/payment/organization-name fields are
  // stripped by the fixture producer.
  const billingFixture = process.env.QUOTA_BILLING_FIXTURE;
  const quotaFixture = process.env.QUOTA_QUOTA_FIXTURE;
  if (billingFixture && quotaFixture) {
    const realBilling = JSON.parse(fs.readFileSync(billingFixture, 'utf8'));
    const realQuota = JSON.parse(fs.readFileSync(quotaFixture, 'utf8'));
    await page.evaluate(([quota, billing]) => window.__setFixtures(quota, billing), [realQuota, realBilling]);
    await page.waitForFunction(() => document.querySelector('[data-provider="nous"]')?.innerText.includes('Nous Portal'));
    const real = await nousText();
    const realProvider = (realQuota.providers && realQuota.providers.nous) || {};
    const usageWindow = realBilling.plan_usage &&
      realBilling.plan_usage.kind === 'usage_window';
    if (usageWindow) {
      // Live paid usage-window account (Plus/Super/Ultra): Billing's dollar
      // model is unavailable, so the card must show the account snapshot's
      // one Account balance and exact window names. There is no default rate
      // line to expect; a real claim may still merge when present.
      const expectedPlan = realBilling.plan_usage.plan || '';
      check('real-plus-plan-rendered', expectedPlan && real.includes(expectedPlan), real);
      for (const label of ['Daily', 'Weekly', 'Monthly', 'Large models (Monthly)']) {
        check(`real-plus-window-${label.toLowerCase().replace(/[^a-z]+/g, '-')}`,
          real.includes(label), real);
      }
      check('real-plus-raw-window-ids-hidden',
        !/\bday\b|\bweek\b|top_tier/.test(real), real);
      const total = (realProvider.account_balances || [])
        .find((balance) => balance && balance.label === 'Account balance');
      const billingBalance = typeof realBilling.balance_display === 'string'
        ? realBilling.balance_display
        : null;
      if (billingBalance) {
        check('real-plus-billing-wallet-visible',
          real.includes('Account balance') && real.includes(billingBalance), real);
      }
      if (process.env.QUOTA_EXPECT_NO_CACHED_MONEY === '1') {
        check('real-plus-stale-or-unmatched-cached-money-withheld',
          !real.includes('Cycle ends:') && !real.includes('Spend this period'), real);
      }
      if (total) {
        check('real-plus-account-balance-visible',
          real.includes('Account balance') && real.includes(`$${total.total_balance} USD`), real);
      }
      check('real-plus-no-component-lines',
        !/Top-up credits|Subscription credits/.test(real), real);
      check('real-plus-private-fields-hidden',
        !/SECRET-ORG|9999|payment_method/.test(real), real);
    } else {
      const expected = process.env.QUOTA_EXPECT_RATE || 'Rate limits: 50 RPM · 500k TPM · 2.1k RPH';
      check('real-billing-plan-rendered', real.includes('Free') || real.includes('Plus') || real.includes('Pro'), real);
      check('real-billing-live-rate-merged', real.includes(expected), real);
      check('real-billing-no-published-fallback', !real.includes('Rate ceiling'), real);
      check('real-billing-private-fields-hidden', !/SECRET-ORG|9999|payment_method/.test(real), real);
    }
    await shot(usageWindow ? 'nous-real-live-plus-window' : 'nous-real-billing-live-rates');
  }

  check('no-page-errors', errors.length === 0, errors);
  await browser.close();
  process.stdout.write(JSON.stringify({ out: OUT, checks }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
