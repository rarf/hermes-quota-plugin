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
    useQuery: () => ({data: options.data, isError: !!options.isError}),
    useQueryClient: () => ({invalidateQueries: () => {}}), useMutation: () => ({}),
    StatusDot: ({tone}) => jsx('span', {'data-tone': tone}),
    Input: 'input', Switch: 'input', SegmentedControl: 'span', icons: {},
    host: {}, PANES_AREA: '', ROUTES_AREA: '', SIDEBAR_NAV_AREA: '', STATUSBAR_AREAS: {right: ''},
    fmtDayTime: new Intl.DateTimeFormat('en-US'), console,
    Date, Intl, setTimeout, clearTimeout,
  };
  const file = options.source || require('node:path').join(__dirname, '../desktop/plugin.js');
  const source = fs.readFileSync(file, 'utf8').replace(/^import[\s\S]*?from ["'][^"']+["'];/gm, '').replace('export default {', 'const plugin = {');
  vm.createContext(context);
  vm.runInContext(source + '\nplugin.register(TEST_CTX); paneDetailAtom.set(TEST_MODE);', Object.assign(context, {
    TEST_MODE: options.mode || 'dense',
    TEST_CTX: {storage: {get: () => undefined}, i18n: {register: v => {translations = v.en;}}, register: () => () => {}},
  }));
  context.PROVIDER = options.provider;
  const expression = options.component === 'chip' ? 'ProviderChip({pid:"deepseek",provider:PROVIDER})' : options.component === 'row' ? 'ProviderRow({id:"deepseek",provider:PROVIDER})' : options.component === 'worst' ? 'QuotaChipWithBar()' : 'QuotaPane()';
  return vm.runInContext(expression, context);
}
module.exports = {renderWidget};
if (require.main === module) process.stdout.write(JSON.stringify(renderWidget(JSON.parse(fs.readFileSync(0, 'utf8')))));
