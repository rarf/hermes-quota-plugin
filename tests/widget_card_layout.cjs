// Stateful renderer for the persisted card-layout feature. Renders the real
// desktop/plugin.js in a vm with SDK/hook boundaries stubbed, then applies a
// list of clicks (pin/unpin/move/edit/sort/reset/confirm/cancel) and
// re-renders after each one, so the atom-backed order and the stateful account
// selection behave like the app.
// A `{control:'data', data}` action swaps the payload between renders, like a
// later poll discovering or losing a provider while the card layout persists.
// {control:'moveSlot', storage, visible, from, to, before} calls the pure
// insertion helper behind a pointer drop; the real gesture lives in
// tests/widget_card_browser.cjs.
//
// stdin: JSON options; stdout: JSON {snapshots, storage, helperMoves, error?, tree?}.
// options.storage seeds ctx.storage, options.data is the quota payload,
// options.editing opens the pane in edit mode, options.storageThrows makes
// every get/set throw (the failure path), options.locale picks a bundle,
// options.page renders the full route page, and options.tree returns the last
// render tree for browser/layout probes.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Parsing and compiling the real plugin dominates a run, and the matrix tests
// boot hundreds of independent panes in one process. Prepare the script once;
// every pane still gets a fresh context, so module atoms stay isolated.
const PLUGIN_SOURCE = fs.readFileSync(path.join(__dirname, '../desktop/plugin.js'), 'utf8')
  .replace(/^import[\s\S]*?from ["'][^"']+["'];/gm, '')
  .replace('export default {', 'const plugin = {');
const PLUGIN_SCRIPT = new vm.Script(`${PLUGIN_SOURCE}\nplugin.register(TEST_CTX);`);

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

function renderWidget(options = {}) {
  const jsx = (type, props, key) =>
    typeof type === 'function' ? type(props || {}) : { type, props: props || {}, key: key ?? null };
  const atom = (value) => ({ get: () => value, set: (v) => { value = v; } });
  let bundles = {};
  const locale = options.locale || 'en';
  const translate = (key, ...args) => {
    const table = bundles[locale] || {};
    const value = key in table ? table[key] : (bundles.en || {})[key];
    return typeof value === 'function' ? value(...args) : value != null ? value : key;
  };
  const storage = new Map(Object.entries(options.storage || {}));
  const storageThrows = Boolean(options.storageThrows);
  const safeStorage = {
    get: (key) => {
      if (storageThrows) throw new Error('storage-get-failed');
      return storage.get(key);
    },
    set: (key, value) => {
      if (storageThrows) throw new Error('storage-set-failed');
      storage.set(key, value);
    },
    remove: (key) => {
      if (storageThrows) throw new Error('storage-remove-failed');
      storage.delete(key);
    },
  };
  // One slot per useState call, in call order, so account selection and the
  // settings/list view survive the re-render after a click.
  const states = [];
  let slot = 0;
  const useState = (initial) => {
    const index = slot++;
    if (index >= states.length) states.push(typeof initial === 'function' ? initial() : initial);
    return [
      states[index],
      (value) => {
        states[index] = typeof value === 'function' ? value(states[index]) : value;
      },
    ];
  };
  // Refs persist across renders like React's, so a drag source survives the
  // render that paints the drop indicator.
  const refs = [];
  let refSlot = 0;
  const useRef = (initial) => {
    const index = refSlot++;
    if (index >= refs.length) refs.push({ current: initial });
    return refs[index];
  };
  const ro = (value) => ({ get: () => value, set: () => {}, subscribe: () => () => {} });
  const context = {
    atom, jsx, jsxs: jsx, cn: (...args) => args.filter(Boolean).join(' '),
    useValue: (a) => a.get(), useEffect: () => {}, useMemo: (f) => f(),
    useRef, useState,
    usePluginI18n: () => translate,
    useQuery: () => ({
      data: options.data, isError: !!options.isError, isPlaceholderData: false, isLoading: false,
    }),
    useQueryClient: () => ({ invalidateQueries: () => {}, setQueryData: () => {} }),
    useMutation: () => ({}),
    Tip: (props) => ({ type: 'Tip', props }),
    Popover: (props) => { const kids = [].concat(props.children).filter(Boolean); return kids.length === 1 ? kids[0] : { type: 'Popover', props }; },
    PopoverTrigger: (props) => props.children,
    PopoverContent: (props) => ({ type: 'PopoverContent', props }),
    ConfirmDialog: (props) => ({ type: 'ConfirmDialog', props }),
    StatusDot: ({ tone }) => jsx('span', { 'data-tone': tone }),
    Input: 'input', Switch: 'input', SegmentedControl: 'span', icons: {},
    host: {
      state: { focusedSessionOwner: ro(null) },
      profileRoutes: async () => [],
      requestProfile: async () => ({ code: 0, output: '{}' }),
      notify: () => {},
    },
    PANES_AREA: '', ROUTES_AREA: '', SIDEBAR_NAV_AREA: '', STATUSBAR_AREAS: { right: '' },
    fmtDayTime: new Intl.DateTimeFormat('en-US'), console,
    Date, Intl, setTimeout, clearTimeout, setInterval, clearInterval,
  };
  const ctx = {
    storage: safeStorage,
    i18n: { register: (value) => { bundles = value; }, t: translate },
    os: { notify: () => {}, writeClipboard: async () => true },
    register: () => () => {},
  };
  vm.createContext(context);
  PLUGIN_SCRIPT.runInContext(Object.assign(context, { TEST_CTX: ctx }));
  vm.runInContext('disabledProvidersAtom.set(TEST_DISABLED);', Object.assign(context, {
    TEST_DISABLED: options.disabled || [],
  }));

  const snapshot = (tree) => {
    const all = [...walk(tree)];
    const byProp = (name) => all.find((n) => n.props && n.props[name]);
    const shelf = byProp('data-pinned-shelf');
    const scroll = byProp('data-quota-scroll');
    const shells = all.filter((n) => n.props && typeof n.props['data-card-shell'] === 'string');
    const inShell = (container) => container
      ? [...walk(container)]
          .filter((n) => n.props && typeof n.props['data-card-shell'] === 'string')
          .map((n) => n.props['data-card-shell'])
      : [];
    // Cards exist with or without edit mode; the shell wrapper does not. Read
    // the band membership from the card roots themselves so the quiet view is
    // testable too, and reduce grouped accounts to their base identity.
    const bandIds = (container) => container
      ? [...walk(container)]
          .filter((n) => n.props && typeof n.props['data-provider'] === 'string')
          .map((n) => String(n.props['data-provider']).split(':')[0])
      : [];
    const pinnedIds = new Set(bandIds(shelf));
    const providers = {};
    for (const node of all) {
      const pid = node.props && node.props['data-provider'];
      if (typeof pid === 'string') providers[pid.split(':')[0]] = pid;
    }
    const controls = {};
    const pinControlCounts = {};
    const dragHandles = [];
    const dragHandleProps = {};
    for (const node of all) {
      const control = node.props && node.props['data-card-control'];
      const id = node.props && node.props['data-card-id'];
      if (!control || !id) continue;
      if (control === 'pin') pinControlCounts[id] = (pinControlCounts[id] || 0) + 1;
      controls[id] = controls[id] || {};
      controls[id][control] = {
        label: node.props['aria-label'],
        disabled: Boolean(node.props.disabled),
        unavailable: node.props['aria-disabled'] === true,
        pressed: node.props['aria-pressed'],
      };
    }
    for (const node of all) {
      const handle = node.props && node.props['data-card-drag-handle'];
      if (handle) {
        dragHandles.push(handle);
        dragHandleProps[handle] = {
          draggable: Boolean(node.props.draggable),
          cursor: node.props.style ? node.props.style.cursor : null,
        };
      }
    }
    const edit = byProp('data-card-edit');
    const labels = [...new Set(all.flatMap((n) => {
      const props = n.props || {};
      return ['title', 'aria-label'].map((key) => props[key]).filter((v) => typeof v === 'string');
    }))];
    const pinnedBadges = {};
    for (const node of all) {
      const id = node.props && node.props['data-card-pinned'];
      if (typeof id === 'string') pinnedBadges[id] = node.props['aria-label'];
    }
    const headerRight = {};
    for (const node of all) {
      const id = node.props && node.props['data-card-header-right'];
      if (!id) continue;
      const kind = (child) => {
        const props = (child && child.props) || {};
        if (props['data-card-plan']) return 'plan';
        if (props['data-card-pinned']) return 'pin';
        if (props['data-card-controls']) return 'controls';
        return null;
      };
      headerRight[id] = [].concat(node.props.children).map(kind).filter(Boolean);
    }
    const shellProps = {};
    for (const node of shells) {
      shellProps[node.props['data-card-shell']] = {
        draggable: Boolean(node.props.draggable),
        drop: node.props['data-card-drop'] || null,
        style: node.props.style,
      };
    }
    const shelfScroll = byProp('data-pinned-shelf-scroll');
    const shelfFrame = byProp('data-pinned-shelf-frame');
    const shelfResize = byProp('data-pinned-shelf-resize');
    const scrollContent = byProp('data-card-scroll-content');
    const body = byProp('data-quota-body');
    const sortAz = all.find((n) => n.props && n.props['data-card-sort'] === 'asc');
    const sortZa = all.find((n) => n.props && n.props['data-card-sort'] === 'desc');
    const resetLayout = byProp('data-card-reset');
    const tool = (node) => node ? {
      label: node.props['aria-label'],
      title: node.props.title,
      hint: node.props['aria-description'] || null,
      text: nodeText(node),
      disabled: Boolean(node.props.disabled),
    } : null;
    const dialog = all.find((n) => n.type === 'ConfirmDialog');
    const full = {
      pointerDrag: Boolean(tree && tree.props && typeof tree.props.onPointerDown === 'function'),
      pinned: bandIds(shelf),
      unpinned: bandIds(scroll).filter((id) => !pinnedIds.has(id)),
      shells: shells.map((n) => n.props['data-card-shell']),
      providers,
      controls,
      pinControlCounts,
      dragHandles: [...new Set(dragHandles)],
      dragHandleProps,
      edit: edit ? { label: edit.props['aria-label'], pressed: edit.props['aria-pressed'] } : null,
      shelf: shelf ? {
        style: shelf.props.style,
        label: shelf.props['aria-label'],
        hasTextLabel: Boolean(byProp('data-pinned-label')),
        scrollStyle: shelfScroll ? shelfScroll.props.style : null,
        frameStyle: shelfFrame ? shelfFrame.props.style : null,
        resize: shelfResize ? {
          role: shelfResize.props.role,
          orientation: shelfResize.props['aria-orientation'],
          label: shelfResize.props['aria-label'],
          hint: shelfResize.props['aria-description'],
          title: shelfResize.props.title,
          disabled: shelfResize.props['aria-disabled'] === true,
          tabIndex: shelfResize.props.tabIndex,
          min: shelfResize.props['aria-valuemin'],
          max: shelfResize.props['aria-valuemax'],
          now: shelfResize.props['aria-valuenow'],
          style: shelfResize.props.style,
        } : null,
      } : null,
      shelfInScroll: shelf && scroll ? [...walk(scroll)].includes(shelf) : false,
      shelfInBody: shelf && body ? [...walk(body)].includes(shelf) : false,
      scrollInBody: scroll && body ? [...walk(body)].includes(scroll) : false,
      body: body ? {
        style: body.props.style,
        containerType: body.props.style.containerType,
      } : null,
      scroll: scroll ? {
        style: scroll.props.style,
        contentStyle: scrollContent ? scrollContent.props.style : null,
      } : null,
      pinnedBadges,
      headerRight,
      shellProps,
      hint: byProp('data-card-edit-hint') ? nodeText(byProp('data-card-edit-hint').props.children) : null,
      layoutTools: sortAz || sortZa || resetLayout ? {
        sortAz: tool(sortAz),
        sortZa: tool(sortZa),
        reset: tool(resetLayout),
      } : null,
      dialog: dialog ? {
        open: Boolean(dialog.props.open),
        title: dialog.props.title,
        description: dialog.props.description,
        confirmLabel: dialog.props.confirmLabel,
        cancelLabel: dialog.props.cancelLabel,
        destructive: Boolean(dialog.props.destructive),
      } : null,
      labels,
      text: nodeText(tree),
    };
    // Matrix probes only need the two bands; skip the bulky per-snapshot
    // detail so a batch of hundreds of panes stays cheap to serialise.
    return options.lean ? { pinned: full.pinned, unpinned: full.unpinned } : full;
  };

  const render = () => {
    slot = 0;
    refSlot = 0;
    return vm.runInContext(options.page ? 'QuotaPane({page: true})' : 'QuotaPane()', context);
  };
  const findControl = (tree, action) => {
    const all = [...walk(tree)];
    if (action.control === 'account') {
      return all.find((n) => n.type === 'button'
        && !n.props['data-card-control']
        && nodeText(n).includes(action.text));
    }
    if (action.control === 'edit') return all.find((n) => n.props && n.props['data-card-edit']);
    if (action.control === 'sort') return all.find((n) => n.props && n.props['data-card-sort'] === action.direction);
    if (action.control === 'reset') return all.find((n) => n.props && n.props['data-card-reset']);
    return all.find((n) => n.props && n.props['data-card-control'] === action.control
      && n.props['data-card-id'] === action.id);
  };

  const snapshots = [];
  let tree = render();
  // Edit mode is pane state now, not a module atom: open it through the real
  // toggle, exactly as a user does, and let the first snapshot show that state.
  if (options.editing) {
    const toggle = [...walk(tree)].find((n) => n.props && n.props['data-card-edit']);
    if (!toggle) return { snapshots, storage: Object.fromEntries(storage), error: 'edit-toggle-not-found' };
    toggle.props.onClick();
    tree = render();
  }
  snapshots.push(snapshot(tree));
  const helperMoves = [];
  for (const action of options.actions || []) {
    if (action.control === 'data') {
      // Swap the quota payload mid-session, like a poll that discovers a new
      // provider (or loses one), then render the same mounted pane.
      options.data = action.data;
      tree = render();
      snapshots.push(snapshot(tree));
      continue;
    }
    if (action.control === 'moveSlot') {
      // Pure insertion helper: the pointer-drag UI is covered by the real
      // Chromium harness, while the slot math stays unit-testable here.
      context.__moveArgs = action;
      helperMoves.push(vm.runInContext(
        'moveCardToSlot(__moveArgs.storage, __moveArgs.visible, __moveArgs.from, __moveArgs.to, __moveArgs.before)',
        context,
      ));
      continue;
    }
    if (action.control === 'confirmSort' || action.control === 'cancelSort'
      || action.control === 'confirmReset' || action.control === 'cancelReset') {
      const dialog = [...walk(tree)].find((n) => n.type === 'ConfirmDialog' && n.props && n.props.open);
      if (!dialog) return { snapshots, storage: Object.fromEntries(storage), error: `dialog-not-open:${JSON.stringify(action)}` };
      if (action.control === 'confirmSort' || action.control === 'confirmReset') dialog.props.onConfirm();
      else dialog.props.onClose();
      tree = render();
      snapshots.push(snapshot(tree));
      continue;
    }
    if (action.control === 'reorderAccounts') {
      const providers = options.data.providers;
      const next = {};
      for (const key of Object.keys(providers).reverse()) next[key] = providers[key];
      options.data.providers = next;
      tree = render();
      snapshots.push(snapshot(tree));
      continue;
    }
    if (action.control === 'remount') {
      // Model the component remount that entering edit mode or pinning causes:
      // refs are recreated while module atoms and ctx.storage persist. The
      // harness used to keep every ref across renders, which hid the bug where
      // a card's useRef default re-read entries[0] after a backend reorder.
      refs.length = 0;
      tree = render();
      snapshots.push(snapshot(tree));
      continue;
    }
    const target = findControl(tree, action);
    if (!target) return { snapshots, storage: Object.fromEntries(storage), error: `control-not-found:${JSON.stringify(action)}` };
    if (target.props.disabled) return { snapshots, storage: Object.fromEntries(storage), error: `control-disabled:${JSON.stringify(action)}` };
    target.props.onClick();
    tree = render();
    snapshots.push(snapshot(tree));
  }
  const out = { snapshots, storage: Object.fromEntries(storage), helperMoves };
  if (options.tree) out.tree = tree;
  return out;
}

module.exports = { renderWidget };
if (require.main === module) {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  // Batch mode: one process, many independent panes. Matrix/property tests
  // would otherwise pay the plugin parse cost per sequence.
  if (Array.isArray(input.batch)) {
    const { batch, ...base } = input;
    const clone = (value) => JSON.parse(JSON.stringify(value));
    const results = batch.map((item) => renderWidget({ ...clone(base), ...clone(item) }));
    process.stdout.write(JSON.stringify({ results }));
  } else {
    process.stdout.write(JSON.stringify(renderWidget(input)));
  }
}
