// Real-React render of the agent-monitor views (optional: needs the dev deps
// from docs/development.md — `npm i --no-save react react-dom @babel/core
// @babel/preset-react @babel/preset-env`). Unlike widget_monitor_renderer.cjs
// this runs the actual hooks and the jsx runtime, so hook-order mistakes,
// invalid element types and render-time throws surface here.
//
// stdin: {data, monitor, history, storage, locale, views: [..], focus: {usage, sessionId, storedId, busy}}
// stdout: {html: {view: markup}, errors: [console.error lines]}
const babel = require('@babel/core');
const React = require('react');
const ReactDOMServer = require('react-dom/server');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

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

const options = JSON.parse(readStdin());
const file = options.source || path.join(__dirname, '..', 'desktop', 'plugin.js');
const code = babel.transformSync(fs.readFileSync(file, 'utf8'), {
  presets: [['@babel/preset-env', {targets: {node: 'current'}, modules: 'commonjs'}]],
  filename: 'plugin.js',
}).code;

const errors = [];
const origError = console.error;
console.error = (...args) => { errors.push(args.map(String).join(' ')); };

const storage = new Map(Object.entries(options.storage || {}));
let bundles = {};
const locale = options.locale || 'en';
const translate = (key, ...args) => {
  const table = bundles[locale] || {};
  const v = key in table ? table[key] : (bundles.en || {})[key];
  return typeof v === 'function' ? v(...args) : v != null ? v : key;
};
const el = tag => props => React.createElement(tag, {className: props && props.className}, props && props.children);
const atoms = [];
const focus = options.focus || {};
const ro = value => ({get: () => value, listen: () => () => {}, subscribe: () => () => {}});
const sdk = {
  atom: v => { const a = {get: () => v, set: n => { v = n; }, subscribe: () => () => {}}; atoms.push(a); return a; },
  cn: (...a) => a.filter(Boolean).join(' '),
  fmtDayTime: new Intl.DateTimeFormat('en-US'),
  host: {
    state: {
      focusedSessionOwner: {get: () => ({connectionId: 'local', profile: 'work'}), set: () => {}, subscribe: () => () => {}},
      focusedUsage: ro(focus.usage ?? null), focusedSessionId: ro(focus.sessionId ?? null),
      focusedStoredSessionId: ro(focus.storedId ?? null), busy: ro(Boolean(focus.busy)),
    },
    composer: {submit: () => true},
    navigate: () => {}, openExternal: () => {}, notify: () => {}, openSession: async () => {},
    profileRoutes: async () => [], requestProfile: async () => ({code: 0, output: '{}'}),
  },
  Input: props => React.createElement('input', {value: props.value ?? '', readOnly: true, 'data-budget-input': props['data-budget-input']}),
  icons: new Proxy({}, {get: () => props => React.createElement('span', {className: props && props.className})}),
  PANES_AREA: 'panes', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', STATUSBAR_AREAS: {right: 'statusbar.right', left: 'statusbar.left'},
  SegmentedControl: props => React.createElement('div', {role: 'tablist'},
    props.options.map(o => React.createElement('button', {key: o.id, 'aria-pressed': o.id === props.value}, o.label))),
  StatusDot: () => React.createElement('span', null),
  Switch: props => React.createElement('button', {'aria-checked': !!props.checked}),
  Tip: ({children}) => children,
  Popover: ({children}) => React.createElement(React.Fragment, null, children),
  PopoverTrigger: ({children}) => children,
  PopoverContent: props => React.createElement('div', {'data-chip-card': props['data-chip-card']}, props.children),
  // Layout matrix (Accounts): a closed dialog renders nothing, like the host's.
  ConfirmDialog: props => (props && props.open ? React.createElement('div', {role: 'dialog'}, props.title, props.description) : null),
  useMutation: () => ({mutate: () => {}, isPending: false}),
  usePluginI18n: () => translate,
  useQuery: opts => {
    const key = opts.queryKey || [];
    if (key[1] === 'monitor') {
      if (key[2] === 'snapshot') return {data: options.monitor, isError: false, isLoading: false};
      // Opt-in (layout matrix): a synthetic `quota monitor accounts` payload.
      if (key[2] === 'accounts' && options.accounts !== undefined) {
        return {data: opts.enabled === false ? undefined : options.accounts, isError: false, isLoading: false};
      }
      return {data: opts.enabled === false ? undefined : options.history, isError: false};
    }
    // Opt-in (layout matrix): a failed quota read, with or without a cache.
    if (options.quotaError) {
      return {data: options.data, isError: true, error: new Error(options.quotaError), isLoading: false, isPlaceholderData: false};
    }
    return {data: options.data, isError: false, isLoading: false, isPlaceholderData: false};
  },
  useQueryClient: () => ({invalidateQueries: () => {}}),
  useValue: a => React.useSyncExternalStore(a.subscribe || (() => () => {}), a.get, a.get),
};
const sandbox = {exports: {}};
const context = vm.createContext({
  module: sandbox, exports: sandbox.exports, console, Promise, Date, JSON, Number, String, Object, Array, Math,
  Set, Map, Intl, setTimeout, clearTimeout, setInterval, clearInterval, encodeURIComponent,
  require: m => m === 'react' ? React : m === 'react/jsx-runtime' ? require('react/jsx-runtime') : m === '@hermes/plugin-sdk' ? sdk : require(m),
});
vm.runInContext(code, context);
const mod = sandbox.exports.default || sandbox.exports;
const items = [];
mod.register({
  i18n: {register: v => { bundles = v; }, t: translate},
  storage: {get: k => storage.get(k), set: (k, v) => storage.set(k, v), remove: k => storage.delete(k)},
  os: {notify: () => {}, writeClipboard: async () => true},
  register: item => { items.push(item); return () => {}; },
});

const html = {};
const page = items.find(i => i.area === 'routes');
for (const view of options.views || ['quota', 'live', 'health', 'usage', 'kanban']) {
  storage.set('paneView', view);
  // Re-apply persisted settings (register() read them once at load).
  vm.runInContext('applyStoredMonitor && applyStoredMonitor()', context);
  try {
    html[view] = ReactDOMServer.renderToStaticMarkup(page.render());
  } catch (e) {
    errors.push('render ' + view + ': ' + (e && e.stack || e));
  }
}
for (const id of ['agents', 'cost', 'context']) {
  try {
    html[id] = ReactDOMServer.renderToStaticMarkup(items.find(i => i.id === id).render());
  } catch (e) {
    errors.push('render ' + id + ': ' + (e && e.stack || e));
  }
}
for (const stage of ['idle', 'confirm']) {
  try {
    const snap = options.monitor && options.monitor.monitor_version ? options.monitor : null;
    html['contextCard_' + stage] = ReactDOMServer.renderToStaticMarkup(React.createElement(vm.runInContext('ContextCard', context),
      {snapshot: snap, scope: {connectionId: 'local', profile: 'work'}, close() {}, stage}));
  } catch (e) {
    errors.push('render contextCard ' + stage + ': ' + (e && e.stack || e));
  }
}
try {
  html.settings = ReactDOMServer.renderToStaticMarkup(React.createElement(vm.runInContext('QuotaSettings', context)));
} catch (e) {
  errors.push('render settings: ' + (e && e.stack || e));
}
// Opt-in (layout matrix): the docked pane, the status-bar items and the four
// chip popover cards (ChipPopover only mounts its card once opened).
if (options.surfaces) {
  const snap = options.monitor && options.monitor.monitor_version ? options.monitor : null;
  const scope = {connectionId: 'local', profile: 'work'};
  const extra = {
    dock: () => React.createElement(vm.runInContext('QuotaPane', context), {page: false}),
    statusbar: () => React.createElement(React.Fragment, null,
      ...['context', 'agents', 'statusbar', 'cost'].map(id => React.createElement('span', {key: id, 'data-sb-item': id},
        items.find(i => i.id === id).render()))),
    popQuota: () => React.createElement(vm.runInContext('QuotaCard', context), {close() {}}),
    popAgents: () => React.createElement(vm.runInContext('AgentsCard', context), {snapshot: snap, scope, close() {}}),
    popContext: () => React.createElement(vm.runInContext('ContextCard', context), {snapshot: snap, scope, close() {}}),
    popCost: () => React.createElement(vm.runInContext('CostCard', context), {snapshot: snap, close() {}}),
  };
  storage.set('paneView', 'quota');
  vm.runInContext('applyStoredMonitor && applyStoredMonitor()', context);
  for (const [id, make] of Object.entries(extra)) {
    try {
      html[id] = ReactDOMServer.renderToStaticMarkup(make());
    } catch (e) {
      errors.push('render ' + id + ': ' + (e && e.stack || e));
    }
  }
  // The docked (narrow) pane for every monitor view. Settings is pane-local
  // state (useState), so the dock's settings body is QuotaSettings itself.
  for (const view of options.dockViews || []) {
    storage.set('paneView', view);
    vm.runInContext('applyStoredMonitor && applyStoredMonitor()', context);
    try {
      html['dock_' + view] = ReactDOMServer.renderToStaticMarkup(view === 'settings'
        ? React.createElement(vm.runInContext('QuotaSettings', context))
        : React.createElement(vm.runInContext('QuotaPane', context), {page: false}));
    } catch (e) {
      errors.push('render dock_' + view + ': ' + (e && e.stack || e));
    }
  }
}
console.error = origError;
process.stdout.write(JSON.stringify({html, errors}));
