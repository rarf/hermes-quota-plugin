// Offline renderer for the agent-monitor views. Same technique as
// widget_renderer.cjs (the real uncompiled desktop/plugin.js in a vm, SDK and
// hook boundaries stubbed), plus what the monitor needs: queries dispatched by
// key (quota status vs `quota monitor snapshot` vs history), a focused profile
// route, ctx.storage backed by a map, captured OS notifications / clipboard /
// openSession calls, a locale switch (en | tr), and the focused chat
// (`focus: {usage, sessionId, storedId, busy}` -> host.state atoms, plus a
// recorded host.composer.submit answering `submitResult`, default true),
// billing fixtures (`billingData`, `billingByProfile`, `billingIsError`), and
// captured console output.
//
// stdin: JSON options; stdout: JSON {tree?, value?, notified, clipboard,
// opened, toasts, cli, billing, liveRequests, logs, storage, queries}. `now`
// (ms) freezes Date / Date.now.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Large stdin payloads can hit EAGAIN on a non-blocking pipe with readFileSync(0).
function readStdin() {
  const chunks = [];
  const buf = Buffer.alloc(1 << 16);
  for (;;) {
    let n;
    try {
      n = fs.readSync(0, buf, 0, buf.length, null);
    } catch (e) {
      if (e.code === 'EAGAIN') { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5); continue; }
      if (e.code === 'EOF') break;
      throw e;
    }
    if (!n) break;
    chunks.push(Buffer.from(buf.subarray(0, n)));
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function run(options = {}) {
  const jsx = (type, props, key) => typeof type === 'function' ? type(props || {}) : {type, props: props || {}, key: key ?? null};
  const atom = value => ({get: () => value, set: v => { value = v; }});
  let bundles = {};
  const locale = options.locale || 'en';
  const translate = (key, ...args) => {
    const table = bundles[locale] || {};
    const v = key in table ? table[key] : (bundles.en || {})[key];
    return typeof v === 'function' ? v(...args) : v != null ? v : key;
  };
  const storage = new Map(Object.entries(options.storage || {}));
  const record = {notified: [], clipboard: [], opened: [], toasts: [], cli: [], billing: [], liveRequests: [], logs: [], queries: [], submitted: [], navigated: [], invalidated: [], mutated: [], mutations: []};
  const focus = options.focus || {};
  const ro = value => ({get: () => value, listen: () => () => {}, subscribe: () => () => {}});
  // The focused owner is mutable so a test can switch profiles mid-render, and
  // `dataByProfile` lets the quota query answer with that profile's payload.
  let owner = options.noScope ? null : (options.owner || {connectionId: 'local', profile: 'work'});
  const fixtureFor = () => {
    const table = options.dataByProfile;
    if (table && owner && owner.profile in table) return table[owner.profile];
    return options.data;
  };
  // Billing fixtures are addressed like the quota ones: one payload per
  // focused profile, or a single payload for every scope.
  const billingFor = () => {
    const table = options.billingByProfile;
    if (table && owner && owner.profile in table) return table[owner.profile];
    return options.billingData;
  };
  const routes = options.routes || [{connectionId: 'local', profile: 'work', targetProfile: 'work', mode: 'local'},
                  {connectionId: 'local', profile: 'home', targetProfile: 'home', mode: 'local'}];
  const queryResult = (opts) => {
    const key = opts.queryKey || [];
    record.queries.push({key, enabled: opts.enabled !== false});
    if (key[1] === 'billing') {
      return {
        data: opts.enabled === false ? undefined : billingFor(),
        dataUpdatedAt: options.billingUpdatedAt,
        isError: !!options.billingIsError,
        error: options.billingIsError ? new Error('billing unavailable') : null,
        isLoading: false,
        isPlaceholderData: false,
      };
    }
    if (key[1] === 'monitor') {
      if (key[2] === 'accounts') {
        return {data: opts.enabled === false ? undefined : options.accounts, isError: false, isLoading: false, isFetching: false};
      }
      if (key[2] === 'snapshot') {
        return {data: options.monitor, isError: !!options.monitorIsError, error: options.monitorIsError ? new Error('boom') : null, isLoading: false};
      }
      return {data: opts.enabled === false ? undefined : options.history, isError: false, isLoading: false};
    }
    return {data: fixtureFor(), isError: !!options.isError, isPlaceholderData: false, isLoading: !!options.quotaIsLoading};
  };
  // One response shape for both the routed and legacy billing doors.
  const billingResponse = () => {
    if (options.billingError) throw new Error(options.billingError);
    const queue = options.billingResults;
    if (Array.isArray(queue) && queue.length) {
      const next = queue.shift();
      if (next && next.error) throw new Error(next.error);
      return next && Object.prototype.hasOwnProperty.call(next, 'result') ? next.result : next;
    }
    return billingFor();
  };
  const host = {
    state: {
      focusedSessionOwner: {get: () => owner, set: (next) => { owner = next; }, subscribe: () => () => {}},
      focusedUsage: ro(focus.usage ?? null),
      focusedSessionId: ro(focus.sessionId ?? null),
      focusedStoredSessionId: ro(focus.storedId ?? null),
      busy: ro(Boolean(focus.busy)),
    },
    composer: {
      submit: (sessionId, text) => { record.submitted.push({sessionId, text}); return options.submitResult ?? true; },
    },
    profileRoutes: async () => routes,
    request: async (method, params, timeoutMs) => {
      record.liveRequests.push({method, params, timeoutMs});
      if (method === 'billing.state') return billingResponse();
      return {code: 0, output: '{}'};
    },
    requestProfile: async (route, method, params, timeoutMs, requestOptions) => {
      if (method === 'billing.state') {
        record.billing.push({route, method, params, timeoutMs, options: requestOptions});
        return billingResponse();
      }
      record.cli.push({route: route.profile, method, argv: params.argv});
      const queue = options.cliResults;
      if (Array.isArray(queue) && queue.length) return queue.shift();
      return options.cliResult || {code: 0, output: '{}'};
    },
    openSession: async (id, opts) => { record.opened.push({id, opts: opts || null}); },
    notify: (toast) => { record.toasts.push(toast); },
    navigate: (path) => { record.navigated.push(path); },
  };
  // `now` (ms) freezes the clock: payloads are built relative to the test's
  // NOW, so a long suite must not drift countdowns past their expectations.
  let DateImpl = Date;
  if (options.now != null && Number.isFinite(Number(options.now))) {
    const fixed = Number(options.now);
    DateImpl = class extends Date {
      constructor(...args) { if (args.length) super(...args); else super(fixed); }
      static now() { return fixed; }
    };
  }
  const logs = [];
  const capture = level => (...args) => { logs.push({level, text: args.map(v => String(v)).join(' ')}); };
  const testConsole = {log: capture('log'), warn: capture('warn'), error: capture('error'), debug: capture('debug'), info: capture('info')};
  const context = {
    atom, jsx, jsxs: jsx, cn: (...args) => args.filter(Boolean).join(' '),
    useValue: a => a.get(), useEffect: (fn) => { if (options.runEffects) fn(); }, useMemo: f => f(),
    useRef: v => ({current: v}), useState: v => [typeof v === 'function' ? v() : v, () => {}],
    usePluginI18n: () => translate,
    useQuery: queryResult,
    useQueryClient: () => ({
      invalidateQueries: (arg) => { record.invalidated.push(arg && arg.queryKey); },
      setQueryData: () => {},
    }),
    // Capture each mutation's options so a test can trigger the real handler a
    // button would: mutate() runs the component's own mutationFn and onSuccess.
    useMutation: (opts = {}) => {
      const mutation = {
        isPending: false,
        mutate: (...args) => {
          record.mutated.push(true);
          const result = opts.mutationFn ? opts.mutationFn(...args) : undefined;
          return Promise.resolve(result).then((value) => {
            if (opts.onSuccess) return opts.onSuccess(value);
            return undefined;
          });
        },
      };
      record.mutations.push(mutation);
      return mutation;
    },
    Tip: props => ({type: 'Tip', props}),
    Popover: props => ({type: 'Popover', props}), PopoverTrigger: props => props.children,
    PopoverContent: props => ({type: 'PopoverContent', props}),
    StatusDot: ({tone}) => jsx('span', {'data-tone': tone}),
    Input: 'input', Switch: 'input', SegmentedControl: 'segmented', icons: {},
    ConfirmDialog: props => ({type: 'ConfirmDialog', props}),
    host,
    PANES_AREA: 'panes', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', STATUSBAR_AREAS: {right: 'statusbar.right'},
    fmtDayTime: new Intl.DateTimeFormat('en-US'), console: testConsole,
    Date: DateImpl, Intl, setTimeout, clearTimeout, setInterval, clearInterval,
  };
  const file = options.source || path.join(__dirname, '../desktop/plugin.js');
  const source = fs.readFileSync(file, 'utf8')
    .replace(/^import[\s\S]*?from ["'][^"']+["'];/gm, '')
    .replace('export default {', 'const plugin = {');
  const registered = [];
  const ctx = {
    storage: {get: k => storage.get(k), set: (k, v) => storage.set(k, v), remove: k => storage.delete(k)},
    i18n: {register: v => { bundles = v; }, t: translate},
    os: {
      notify: n => { record.notified.push({title: n.title, body: n.body, activatable: typeof n.onActivate === 'function'}); },
      writeClipboard: async text => { record.clipboard.push(text); return true; },
    },
    register: item => { registered.push(item.id + ':' + item.area); return () => {}; },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nplugin.register(TEST_CTX);', Object.assign(context, {TEST_CTX: ctx}));
  // Apply the per-test hide list after registration, exactly like
  // widget_renderer.cjs. Leave storage-applied lists alone when the option is
  // absent; setting [] here would wipe a fixture stored in `storage`.
  if (options.disabled !== undefined) {
    vm.runInContext('disabledProvidersAtom.set(TEST_DISABLED);', Object.assign(context, {
      TEST_DISABLED: options.disabled || [],
    }));
  }
  const expressions = {
    pane: 'QuotaPane()',
    settings: 'QuotaSettings()',
    agents: 'MonitorStatus()',
    cost: 'CostChip()',
    status: 'StatusBar()',
    live: 'LiveView({mon: useMonitor()})',
    context: 'ContextChip()',
    contextCard: `ContextCard({snapshot: useMonitor().snapshot, scope: useMonitor().scope, close() {}, stage: ${JSON.stringify(options.stage || 'idle')}, topic: ${JSON.stringify(options.topic || '')}})`,
  };
  const out = {registered};
  if (options.expr) {
    out.value = await vm.runInContext('(async () => (' + options.expr + '))()', context);
  } else {
    out.tree = vm.runInContext(expressions[options.component || 'pane'], context);
  }
  // Click the real control with this title (e.g. the Quota pane's refresh
  // button), so a test exercises the component's own handler end to end.
  const findByTitle = (node, title) => {
    if (!node || typeof node !== 'object') return null;
    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findByTitle(child, title);
        if (found) return found;
      }
      return null;
    }
    if (node.props && node.props.title === title) return node;
    return findByTitle(node.props && node.props.children, title);
  };
  if (options.clickTitle) {
    const title = String(options.clickTitle);
    const button = findByTitle(out.tree, title);
    if (!button || typeof button.props.onClick !== 'function') {
      throw new Error('no clickable element titled: ' + title);
    }
    await button.props.onClick();
  }
  // Let fire-and-forget handlers (clipboard, openSession) settle.
  await new Promise(resolve => setTimeout(resolve, 10));
  return Object.assign(out, record, {storage: Object.fromEntries(storage)});
}

module.exports = {run};
if (require.main === module) {
  run(JSON.parse(readStdin())).then(
    out => process.stdout.write(JSON.stringify(out)),
    error => { process.stderr.write(String(error && error.stack || error)); process.exit(1); },
  );
}
