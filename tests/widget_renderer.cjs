// Run the actual uncompiled widget with SDK/hook boundaries stubbed offline.
const fs = require('node:fs');
const vm = require('node:vm');
function renderWidget(options = {}) {
  const jsx = (type, props) => typeof type === 'function' ? type(props || {}) : {type, props: props || {}};
  const atom = value => ({get: () => value, set: v => { value = v; }});
  let translations = {};
  const context = {
    atom, jsx, jsxs: jsx, cn: (...args) => args.filter(Boolean).join(' '),
    useValue: a => a.get(), useEffect: () => {}, useMemo: f => f(),
    useRef: v => ({current: v}), useState: v => [typeof v === 'function' ? v() : v, () => {}],
    usePluginI18n: () => (key, ...args) => typeof translations[key] === 'function' ? translations[key](...args) : translations[key] || key,
    useQuery: () => ({data: options.data, isError: !!options.isError, isPlaceholderData: !!options.isPlaceholderData}),
    useQueryClient: () => ({invalidateQueries: () => {}}), useMutation: () => ({}),
    // Keep tooltip labels separate from trigger text/native title. This is a
    // structural test seam, not a simulation of hover (see hover test).
    Tip: props => ({type: 'Tip', props}),
    // Radix Popover.Root renders no element, and the card is only built while
    // open (useState(false) here): a closed chip stays Tip > trigger > button.
    Popover: props => { const kids = [].concat(props.children).filter(Boolean); return kids.length === 1 ? kids[0] : {type: 'Popover', props}; },
    PopoverTrigger: props => props.children,
    PopoverContent: props => ({type: 'PopoverContent', props}),
    StatusDot: ({tone}) => jsx('span', {'data-tone': tone}),
    Input: 'input', Switch: 'input', SegmentedControl: 'span', icons: {},
    // Quota commands route to the focused profile's owner (#26); the pane reads
    // `host.state.focusedSessionOwner` before it can render, so the offline
    // harness has to provide the same read-only atom shape as the SDK.
    host: {state: {focusedSessionOwner: {get: () => null, set: () => {}, subscribe: () => () => {}}}},
    PANES_AREA: '', ROUTES_AREA: '', SIDEBAR_NAV_AREA: '', STATUSBAR_AREAS: {right: ''},
    fmtDayTime: new Intl.DateTimeFormat('en-US'), console,
    Date, Intl, setTimeout, clearTimeout,
  };
  const openedExternal = [];
  const file = options.source || require('node:path').join(__dirname, '../desktop/plugin.js');
  const source = fs.readFileSync(file, 'utf8').replace(/^import[\s\S]*?from ["'][^"']+["'];/gm, '').replace('export default {', 'const plugin = {');
  vm.createContext(context);
  vm.runInContext(source + '\nplugin.register(TEST_CTX); paneDetailAtom.set(TEST_MODE);', Object.assign(context, {
    TEST_MODE: options.mode || 'dense',
    TEST_CTX: {storage: {get: () => undefined}, os: {openExternal: async url => { openedExternal.push(url); return true; }}, i18n: {register: v => {translations = v.en;}}, register: () => () => {}},
  }));
  vm.runInContext('statusbarModeAtom.set(TEST_STATUS_MODE); disabledProvidersAtom.set(TEST_DISABLED);', Object.assign(context, { TEST_STATUS_MODE: options.statusMode || 'all', TEST_DISABLED: options.disabled || [] }));
  // Pane style ('rings' default | 'bars'). Swallowed on purpose: the atom can be
  // absent while the toggle is still landing, and that must not fail every other
  // widget test. Renderer tests for the style assert on the tree, not this call.
  vm.runInContext('try { paneStyleAtom.set(TEST_PANE_STYLE); } catch (e) {}', Object.assign(context, { TEST_PANE_STYLE: options.paneStyle || 'rings' }));
  // Status-bar chip style ('both' default | 'rings' | 'values'). Swallowed for
  // the same reason: a missing atom must not fail every other widget test.
  vm.runInContext('try { statusbarStyleAtom.set(TEST_STATUSBAR_STYLE); } catch (e) {}', Object.assign(context, { TEST_STATUSBAR_STYLE: options.statusbarStyle || 'both' }));
  context.PROVIDER = options.provider;
  context.WIDGET_ID = options.id || 'deepseek';
  const expression = options.component === 'status' ? 'StatusBar()' : options.component === 'chip' ? 'ProviderChip({pid:WIDGET_ID,provider:PROVIDER})' : options.component === 'row' ? 'ProviderRow({id:WIDGET_ID,provider:PROVIDER})' : options.component === 'worst' ? 'QuotaChipWithBar()' : 'QuotaPane()';
  const tree = vm.runInContext(expression, context);
  if (options.clickLinks) {
    const visit = node => {
      if (Array.isArray(node)) return node.forEach(visit);
      if (!node || typeof node !== 'object') return;
      if (node.type === 'a' && typeof node.props.onClick === 'function') node.props.onClick({preventDefault() {}});
      visit(node.props?.children);
    };
    visit(tree);
  }
  tree.openedExternal = openedExternal;
  return tree;
}
module.exports = {renderWidget};
if (require.main === module) process.stdout.write(JSON.stringify(renderWidget(JSON.parse(fs.readFileSync(0, 'utf8')))));
