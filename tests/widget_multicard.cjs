// Stateful sibling of widget_renderer.cjs: renders the real desktop/plugin.js
// and lets a test click one card button, then re-renders, so the selection
// change is observable. The stubs mirror the shared harness; this file is
// separate so widget_renderer.cjs stays untouched for every existing test.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function nodeText(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (Array.isArray(node)) return node.map(nodeText).join('');
  if (typeof node !== 'object') return String(node);
  return nodeText(node.props ? node.props.children : null);
}

function* walk(node) {
  if (Array.isArray(node)) {
    for (const item of node) yield* walk(item);
    return;
  }
  if (!node || typeof node !== 'object') return;
  yield node;
  if (node.props && node.props.children != null) yield* walk(node.props.children);
}

// Card buttons are the ones carrying a status dot (the StatusDot stub).
function cardButtons(tree) {
  return [...walk(tree)].filter(
    (n) =>
      n.type === 'button' &&
      Array.isArray(n.props.children) &&
      n.props.children.some((c) => c && c.props && typeof c.props['data-tone'] === 'string'),
  );
}

function renderWidget(options = {}) {
  const jsx = (type, props) =>
    typeof type === 'function' ? type(props || {}) : { type, props: props || {} };
  const atom = (value) => ({
    get: () => value,
    set: (v) => {
      value = v;
    },
  });
  let translations = {};
  // One slot per useState call, in call order. A re-render reads the slots
  // instead of re-initialising them, so clicks persist like real React state.
  const states = [];
  let slot = 0;
  const useState = (initial) => {
    const index = slot++;
    if (index >= states.length) states.push(typeof initial === 'function' ? initial() : initial);
    return [
      states[index],
      (v) => {
        states[index] = typeof v === 'function' ? v(states[index]) : v;
      },
    ];
  };
  const context = {
    atom, jsx, jsxs: jsx, cn: (...args) => args.filter(Boolean).join(' '),
    useValue: (a) => a.get(), useEffect: () => {}, useMemo: (f) => f(),
    useRef: (v) => ({ current: v }), useState,
    usePluginI18n: () => (key, ...args) =>
      typeof translations[key] === 'function' ? translations[key](...args) : translations[key] || key,
    useQuery: () => ({
      data: options.data, isError: !!options.isError, isPlaceholderData: !!options.isPlaceholderData,
    }),
    useQueryClient: () => ({ invalidateQueries: () => {} }), useMutation: () => ({}),
    Tip: (props) => ({ type: 'Tip', props }),
    StatusDot: ({ tone }) => jsx('span', { 'data-tone': tone }),
    Input: 'input', Switch: 'input', SegmentedControl: 'span', icons: {},
    host: { state: { focusedSessionOwner: { get: () => null, set: () => {}, subscribe: () => () => {} } } },
    PANES_AREA: '', ROUTES_AREA: '', SIDEBAR_NAV_AREA: '', STATUSBAR_AREAS: { right: '' },
    fmtDayTime: new Intl.DateTimeFormat('en-US'), console, Date, Intl, setTimeout, clearTimeout,
  };
  const file = path.join(__dirname, '../desktop/plugin.js');
  const source = fs.readFileSync(file, 'utf8')
    .replace(/^import[\s\S]*?from ["'][^"']+["'];/gm, '')
    .replace('export default {', 'const plugin = {');
  vm.createContext(context);
  vm.runInContext(source + '\nplugin.register(TEST_CTX); paneDetailAtom.set(TEST_MODE);', Object.assign(context, {
    TEST_MODE: options.mode || 'dense',
    TEST_CTX: { storage: { get: () => undefined }, i18n: { register: (v) => { translations = v.en; } }, register: () => () => {} },
  }));
  vm.runInContext('statusbarModeAtom.set(TEST_STATUS_MODE); disabledProvidersAtom.set(TEST_DISABLED);', Object.assign(context, {
    TEST_STATUS_MODE: options.statusMode || 'all', TEST_DISABLED: options.disabled || [],
  }));
  const expression =
    options.component === 'row'
      ? 'ProviderRow({id:WIDGET_ID,provider:PROVIDER})'
      : options.component === 'status'
        ? 'StatusBar()'
        : 'QuotaPane()';
  context.WIDGET_ID = options.id || 'deepseek';
  context.PROVIDER = options.provider;
  const runPass = () => {
    slot = 0;
    return vm.runInContext(expression, context);
  };
  const before = runPass();
  const beforeButtons = cardButtons(before);
  const labels = beforeButtons.map(nodeText);
  const tones = beforeButtons.map(
    (b) => b.props.children.find((c) => c && c.props && c.props['data-tone']).props['data-tone'],
  );
  const pressed = beforeButtons.map((b) => b.props['aria-pressed'] === true);
  let after = null;
  let pressedAfter = null;
  if (options.click != null) {
    const targetIndex = labels.indexOf(options.click);
    if (targetIndex === -1) return { error: 'card-button-not-found', labels, tones };
    beforeButtons[targetIndex].props.onClick();
    after = runPass();
    pressedAfter = cardButtons(after).map((b) => b.props['aria-pressed'] === true);
  }
  return { labels, tones, pressed, pressedAfter, before, after };
}

module.exports = { renderWidget };
if (require.main === module) {
  const input = fs.readFileSync(0, 'utf8');
  process.stdout.write(JSON.stringify(renderWidget(JSON.parse(input))));
}
