// Headless render harness for the Quota desktop widget.
//
// Renders the REAL desktop/plugin.js through a mocked @hermes/plugin-sdk, so a
// UI change can be checked (and screenshotted) without launching Electron.
// See the hermes-plugin-verify skill for the technique and its traps.
//
//   npm i --no-save react react-dom @babel/core @babel/preset-react @babel/preset-env
//   hermes quota status --json --cached > .widget-fixture.json
//   node scripts/render-widget.cjs                    # pane (routes)
//   AREA=statusbar.right node scripts/render-widget.cjs   # status bar chip
//
// Env: PLUGIN_FILE, FIXTURE, OUT, AREA
const babel = require('@babel/core')
const React = require('react')
const ReactDOMServer = require('react-dom/server')
const vm = require('vm')
const fs = require('fs')
const os = require('os')
const path = require('path')

const repo = path.resolve(__dirname, '..')
const FILE = process.env.PLUGIN_FILE || path.join(repo, 'desktop', 'plugin.js')
const FIXTURE = process.env.FIXTURE || path.join(repo, '.widget-fixture.json')
const OUT = process.env.OUT || path.join(os.tmpdir(), 'quota-widget-render.html')

const src = fs.readFileSync(FILE, 'utf8')
const out = babel.transformSync(src, {
  presets: [
    ['@babel/preset-react', { runtime: 'classic' }],
    ['@babel/preset-env', { targets: { node: 'current' }, modules: 'commonjs' }]
  ],
  filename: 'plugin.js'
}).code

const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'))

const mk = tag => props =>
  React.createElement(tag, { ...(props || {}), key: undefined }, (props || {}).children)
const icons = new Proxy(
  {},
  { get: () => props => React.createElement('span', null, (props || {}).children) }
)

const strings = {}
const items = []
const sdk = {
  atom: v => ({ get: () => v, set: () => {}, subscribe: () => () => {} }),
  cn: (...a) => a.filter(Boolean).join(' '),
  fmtDayTime: iso => iso,
  host: {
    request: async (method) => {
      if (method === 'cli.exec') return { code: 0, output: JSON.stringify(fixture) }
      return {}
    },
    navigate: () => {},
    openExternal: () => {},
    // Quota commands are routed to the focused profile's owner (#26). Without
    // this state the pane throws on `focusedSessionOwner.get()` before it can
    // render, so the harness must provide it.
    state: {
      focusedSessionOwner: { get: () => null, set: () => {}, subscribe: () => () => {} }
    }
  },
  Input: props =>
    React.createElement('input', { value: (props || {}).value ?? '', readOnly: true }),
  icons,
  PANES_AREA: 'panes',
  ROUTES_AREA: 'routes',
  SIDEBAR_NAV_AREA: 'sidebar',
  STATUSBAR_AREAS: { left: 'statusbar.left', right: 'statusbar.right' },
  SegmentedControl: mk('div'),
  StatusDot: mk('span'),
  Switch: mk('button'),
  // Static markup has no hover state. Do not fake a tooltip with title;
  // tests/widget_hover.cjs exercises the actual SDK Tip in Chromium.
  Tip: ({ children }) => children,
  useMutation: () => ({ mutate: () => {}, isPending: false }),
  usePluginI18n: id => (key, ...args) => {
    const v = (strings[id] || {})[key]
    if (typeof v === 'function') return v(...args)
    return v != null ? v : key
  },
  useQuery: () => ({
    data: fixture,
    isLoading: false,
    isError: false,
    error: null,
    isPlaceholderData: false,
    refetch: () => {}
  }),
  useQueryClient: () => ({ invalidateQueries: () => {} }),
  useValue: a => (a && typeof a.get === 'function' ? a.get() : 60)
}

const sandbox = { exports: {} }
const context = vm.createContext({
  module: sandbox,
  exports: sandbox.exports,
  console,
  React,
  Promise,
  Date,
  JSON,
  Number,
  String,
  Object,
  Array,
  Math,
  Set,
  Map,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  require: m =>
    m === 'react'
      ? React
      : m === 'react/jsx-runtime'
        ? require('react/jsx-runtime')
        : m === '@hermes/plugin-sdk'
          ? sdk
          : require(m)
})

vm.runInContext(out, context)

const mod = sandbox.exports.default || sandbox.exports
mod.register({
  i18n: {
    register: table => {
      const locale = (table && table.en) || table || {}
      strings[mod.id] = locale
      Object.assign(strings, locale)
    }
  },
  storage: { get: () => null, set: () => {} },
  register: item => items.push(item),
  register_hook: () => {}
})

const area = process.env.AREA || 'routes'
const item = items.find(i => i.area === area)
if (!item) {
  console.error('not registered; got:', items.map(i => `${i.id}:${i.area}`))
  process.exit(2)
}

const html = ReactDOMServer.renderToStaticMarkup(item.render())
fs.writeFileSync(OUT, html)

console.log('plugin file :', FILE)
console.log('rendered    :', area, item.id, html.length, 'chars ->', OUT)
console.log('providers   :', Object.keys(fixture.providers || {}).length)
console.log('--- assertions ---')
const checks =
  area === 'routes'
    ? {
        'name "OpenCode Go"': /OpenCode Go/.test(html),
        'no raw provider id': !/>\s*opencode-go\s*</.test(html),
        'brand mark path present': /M4 2h16v20H4zM8 6v12h8V6z/.test(html),
        'footer age + cadence': /old · poll \d+s/.test(html),
        // The pane labels the timestamp "Checked <date, time, zone>" (#23); it
        // used to read "fetched <relative day>".
        'footer checked line': /Checked /.test(html)
      }
    : {
        'name "OpenCode Go"': /OpenCode Go/.test(html),
        'no raw provider id': !/>\s*opencode-go\s*</.test(html)
      }
let ok = true
for (const [k, v] of Object.entries(checks)) {
  if (!v) ok = false
  console.log(`${v ? 'PASS' : 'FAIL'}  ${k}`)
}
process.exit(ok ? 0 : 1)
