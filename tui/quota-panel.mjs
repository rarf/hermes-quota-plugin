// Standalone Hermes Ink widget. No credentials or Hermes core imports.
import { mkdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { execFile } from 'node:child_process';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const clean = value => String(value ?? '').replace(/[\x00-\x1f\x7f-\x9f]/g, '').slice(0, 120);
const list = value => Array.isArray(value) ? value : [];
const SELECTOR = /^[a-z0-9][a-z0-9:_#.-]{0,119}$/i;

// Keep these public display names aligned with Desktop PROVIDER_META.
export const PROVIDER_NAMES = {
  anthropic: 'Anthropic', 'openai-codex': 'OpenAI Codex', nous: 'Nous Portal',
  openrouter: 'OpenRouter', deepseek: 'DeepSeek', experientiallabs: 'Experiential Labs',
  gemini: 'Google Gemini', kimi: 'Kimi / Moonshot', grok: 'xAI Grok',
  'opencode-go': 'OpenCode Go', copilot: 'Copilot', zai: 'Z.ai',
  commandcode: 'CommandCode', cursor: 'Cursor', antigravity: 'Antigravity',
  minimax: 'MiniMax', ollama: 'Ollama',
};

function friendlyName(id, record, account) {
  const [base, ...suffix] = id.split(':');
  const name = Object.hasOwn(PROVIDER_NAMES, base) ? PROVIDER_NAMES[base] : clean(record.label || base);
  const label = account || record.account_label || suffix.join(':');
  return label ? `${name} · ${clean(label)}` : name;
}

function resetDate(value, now, zone = '') {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const fields = zoneParts(date, safeZone(zone));
  const nowFields = zoneParts(new Date(now), safeZone(zone));
  const year = fields.year !== nowFields.year ? `/${fields.year}` : '';
  return `${fields.day}/${fields.month}${year} ${fields.hour}:${fields.minute}`;
}

function resetLabel(value, now, zone = '') {
  const time = typeof value === 'string' ? Date.parse(value) : NaN;
  if (!Number.isFinite(time)) return '';
  const minutes = Math.ceil((time - now) / 60000);
  if (minutes <= 0) return 'reset due';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const relative = days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
  return `reset ${relative} (${resetDate(value, now, zone)})`;
}

export function panelModel(cache, selected = [], now = Date.now()) {
  const timestamp = typeof cache?.fetched_at === 'string' ? Date.parse(cache.fetched_at) : NaN;
  const age = Number.isFinite(timestamp) ? Math.max(0, (now - timestamp) / 1000) : null;
  const providers = object(cache?.providers) ? cache.providers : {};
  const rows = [];
  for (const [id, record] of Object.entries(providers)) {
    if (!object(record)) continue;
    const accounts = id === 'openai-codex' && list(record.accounts).length ? record.accounts : [record];
    accounts.forEach((account, index) => {
      if (!object(account)) return;
      const key = accounts === record.accounts ? `${id}:${account.id || index + 1}` : id;
      if (selected.length && !selected.some(s => s === key || s === id || key.startsWith(`${s}:`))) return;
      const label = friendlyName(id, record, accounts === record.accounts ? account.label || `Account ${index + 1}` : null);
      const windows = list(account.windows).filter(object);
      const meters = windows.map(window => {
        const used = window.used_percent;
        const percent = typeof used === 'number' && Number.isFinite(used) && used >= 0 && used <= 100
          ? `${Math.round(100 - used)}% left` : '? left';
        const reset = resetLabel(window.reset_at, now, cache?.timezone);
        return `${clean(window.label || 'Window')} ${percent}${reset ? ` (${reset})` : ''}`;
      });
      const balances = list(account.account_balances).filter(object).filter(b =>
        typeof b.total_balance === 'string' && /^-?\d+(\.\d+)?$/.test(b.total_balance)
      ).map(b => `${clean(b.currency)} ${clean(b.total_balance)}`);
      const unavailable = Boolean(account.unavailable_reason);
      rows.push({ id: key, label, windows, balances, reason: account.unavailable_reason,
        text: `${label}: ${unavailable ? 'unavailable' : [...meters, ...balances].join(' · ') || 'no data'}`, unavailable, hasData: !unavailable && Boolean(meters.length || balances.length) });
    });
  }
  for (const selector of selected) {
    if (!rows.some(row => row.id === selector || row.id.startsWith(`${selector}:`))) {
      rows.push({ id: selector, text: `${clean(selector)}: no data`, unavailable: true, hasData: false });
    }
  }
  return { rows, stale: age === null || age > 600, age };
}

/** Cached availability is evidence, not a new credential scan or login check. */
export function pickerOptions(cache) {
  return panelModel(cache).rows.filter(row => !/^(no-credentials|disabled|not-enabled|not-opted-in|opt-in-disabled)(:|$)/i.test(String(row.reason || '')));
}

/**
 * Where the picker must start: the remembered provider/account when it is one
 * of the cached rows, otherwise the first row. Without this the modal opened
 * on row 0 after every restart, so it looked like nothing was selected.
 */
export function rememberedIndex(options, remembered) {
  if (!Array.isArray(options) || !options.length || !remembered) return 0;
  const at = options.findIndex(row => row && (row.id === remembered || String(row.id).startsWith(`${remembered}:`)));
  return at < 0 ? 0 : at;
}

/**
 * A selection worth persisting: it still resolves to a cached provider/account.
 * Several TUI processes share one preference file, and an old process can
 * remount a stale panel — without this check it overwrites the last good
 * choice with a selector that matches nothing.
 */
export function rememberable(options, selected) {
  if (!Array.isArray(options) || !Array.isArray(selected) || selected.length !== 1) return false;
  return options.some(row => row && (row.id === selected[0] || String(row.id).startsWith(`${selected[0]}:`)));
}

export function meterModel(cache, selector, now = Date.now()) {
  const model = panelModel(cache, [selector], now);
  const row = model.rows.find(item => item.id === selector) || model.rows[0];
  const meters = row?.unavailable ? [] : list(row?.windows).map(window => {
    const used = window.used_percent;
    return { label: clean(window.label || 'Window'), reset: resetLabel(window.reset_at, now, cache?.timezone),
      resetDate: typeof window.reset_at === 'string' ? resetDate(window.reset_at, now, cache?.timezone) : '',
      remaining: typeof used === 'number' && Number.isFinite(used) && used >= 0 && used <= 100 ? Math.round(100 - used) : null };
  });
  return { ...model, label: row?.label || clean(selector || 'Choose a provider'),
    unavailable: Boolean(row?.unavailable), balances: row?.balances || [], meters };
}

const shorten = (text, limit) => [...text].length <= limit ? text : [...text].slice(0, Math.max(0, limit - 1)).join('') + '…';
const length = text => [...text].length;

// Third-party aggregator metadata. Only a validated announcement is visible;
// forecasts, observations and scheduled_for are deliberately ignored. The X
// source is the verified announcer account, never arbitrary cached text.
const SPECIAL_SOURCE = /^https:\/\/x\.com\/thsottiaux\/status\/\d+$/;

/** A usable IANA zone name, or '' to use the process zone. */
function safeZone(value) {
  const name = typeof value === 'string' ? value.trim() : '';
  if (!name) return '';
  try {
    new Intl.DateTimeFormat('en-GB', { timeZone: name }).format(new Date());
    return name;
  } catch {
    return '';
  }
}

/** Calendar fields for one instant in the chosen zone. */
function zoneParts(date, zone) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: zone || undefined,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date);
  const out = {};
  for (const part of parts) if (part.type !== 'literal') out[part.type] = part.value;
  return out;
}

/** Provider-level announcement from the cache root, or null when unusable. */
export function specialResetModel(cache, now = Date.now()) {
  const root = object(cache?.codex_special_reset) ? cache.codex_special_reset : null;
  const announcement = root && object(root.announcement) ? root.announcement : null;
  if (!announcement) return null;
  const status = announcement.status === 'scheduled' || announcement.status === 'recorded' ? announcement.status : '';
  const resetType = announcement.reset_type === 'regular' || announcement.reset_type === 'banked' ? announcement.reset_type : '';
  const announcedAt = typeof announcement.announced_at === 'string' ? announcement.announced_at : '';
  if (!status || !resetType || !announcement.id || typeof announcement.id !== 'string') return null;
  // Require an explicit timezone so a date-only value cannot be reinterpreted.
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(announcedAt)) return null;
  const time = Date.parse(announcedAt);
  if (!Number.isFinite(time)) return null;
  if (typeof announcement.source_url !== 'string' || !SPECIAL_SOURCE.test(announcement.source_url)) return null;
  const date = new Date(time);
  // Format in the user's configured zone (cache root) so a gateway or
  // renderer running under another TZ cannot shift the announcement hour.
  const zone = safeZone(cache?.timezone);
  // The publisher's scheduled estimate is the actionable time; the announced
  // timestamp only says when the post appeared.
  const scheduledText = typeof announcement.scheduled_for === 'string' ? announcement.scheduled_for.trim() : '';
  // Match the CLI parser: a legacy timestamp without an offset means UTC.
  const scheduledMs = scheduledText
    ? Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/i.test(scheduledText) ? scheduledText : `${scheduledText}Z`) : NaN;
  // Recorded events are history; an elapsed estimate is no longer actionable.
  if (status === 'recorded' || (Number.isFinite(scheduledMs) && scheduledMs <= now)) return null;
  const primary = Number.isFinite(scheduledMs) ? scheduledMs : time;
  const fields = zoneParts(new Date(primary), zone);
  const nowFields = zoneParts(new Date(now), zone);
  const fetched = typeof root.fetched_at === 'string' ? Date.parse(root.fetched_at) : NaN;
  const year = fields.year === nowFields.year ? '' : `/${fields.year}`;
  return {
    id: clean(announcement.id), status, resetType,
    announcedAt, sourceUrl: announcement.source_url,
    scheduled: Number.isFinite(scheduledMs),
    stale: root.stale !== false || !Number.isFinite(fetched),
    date: `${fields.day}/${fields.month}${year} ${fields.hour}:${fields.minute}`,
  };
}

const ANNOUNCEMENT_CREDIT = 'codex-resets.com';
function announcementVariants(special) {
  const mark = special.stale ? 'STALE · ' : '';
  const banked = special.resetType === 'banked';
  return [
    `${mark}✨ ${banked ? 'Banked reset' : 'Reset'} ${special.date} · ${special.status} · ${ANNOUNCEMENT_CREDIT}`,
    `${mark}✨ ${banked ? 'Banked reset' : 'Reset'} ${special.date} · ${ANNOUNCEMENT_CREDIT}`,
    `${mark}✨ ${special.date} · ${special.status} · ${ANNOUNCEMENT_CREDIT}`,
    `${mark}✨ ${special.date} · ${ANNOUNCEMENT_CREDIT}`,
    `${mark}ann. ${special.date} · ${ANNOUNCEMENT_CREDIT}`,
  ];
}

/** Compact provider-level notice used by the picker where the one-row chip
 *  cannot fit attribution (very narrow terminals). */
export function specialNoticeText(cache, now = Date.now(), cols = 120) {
  const special = specialResetModel(cache, now);
  if (!special) return '';
  if (cols < 60) return `${special.stale ? 'STALE · ' : ''}✨ ${special.date} · ${ANNOUNCEMENT_CREDIT}`;
  return `✨ ${special.resetType === 'banked' ? 'Banked' : 'Special'} reset ${special.date} · ${special.status} · ${special.stale ? 'stale · ' : ''}${ANNOUNCEMENT_CREDIT}`;
}

/** Exactly one line; tightest known window first, with explicit overflow. */
export function lineModel(cache, selector, cols, now = Date.now(), status = {}) {
  const model = meterModel(cache, selector, now);
  const meters = [...model.meters].sort((a, b) => (a.remaining ?? Infinity) - (b.remaining ?? Infinity));
  const prefix = `Quota · ${model.stale ? 'STALE · ' : ''}`;
  const error = status.error ? 'refresh error · ' : '';
  const width = Math.max(1, Math.floor(cols));
  const meter = meters[0];
  const remaining = meter?.remaining ?? null;
  const overflow = meters.length > 1 ? ` · +${meters.length - 1}` : '';
  let barWidth = 6, nameWidth = 24, windowWidth = 12;
  const segment = window => {
    const left = window.remaining === null ? '?' : String(window.remaining);
    const filled = window.remaining === null ? 0 : Math.round(window.remaining / 100 * barWidth);
    const bar = '█'.repeat(filled) + '░'.repeat(barWidth - filled);
    const reset = width < 60 ? '' : window.resetDate && window.reset !== 'reset due' ? `reset ${window.resetDate}` : window.reset;
    return `${width > 48 ? `${shorten(window.label, windowWidth)} ` : ''}${bar} ${left}%${width > 48 ? ' left' : ''}${reset ? ` · ${reset}` : ''}`;
  };
  const build = () => `${prefix}${error}${shorten(model.label, nameWidth)} · ${meter ? segment(meter) : model.unavailable ? 'unavailable' : model.balances.join(' · ') || '/quota-select'}${overflow}`;
  if (length(build()) > width) barWidth = 4;

  if (length(build()) > width) nameWidth = Math.max(8, Math.min(24, Math.floor(width / 4)));
  if (length(build()) > width) windowWidth = 6;
  let text = build();

  if (overflow && length(text) > width && width > length(overflow)) {
    text = shorten(text.slice(0, -overflow.length), width - length(overflow)) + overflow;
  }

  // Announcement is provider-level and only meaningful beside Codex quota.
  // Below 60 columns the one row cannot carry attribution without dropping
  // mandatory quota state, so the chip stays pure quota and the picker shows
  // the announcement instead.
  const special = String(selector).split(':')[0] === 'openai-codex' ? specialResetModel(cache, now) : null;
  if (special && width >= 60) {
    const left = remaining === null ? '?' : String(remaining);
    const quotaValue = model.unavailable ? 'unavailable' : `${left}%`;
    const resetDay = meter?.resetDate ? meter.resetDate.split(' ')[0] : '';
    const reset = resetDay ? ` · reset ${resetDay}` : '';
    const mark = `${model.stale ? 'STALE · ' : ''}${status.error ? 'refresh error · ' : ''}`;
    const shortLabel = model.label.replace(/^OpenAI /, '');
    const account = shortLabel.includes(' · ') ? shortLabel.split(' · ').slice(1).join(' · ') : '';
    const quotaVariants = [
      text,
      `${mark}Quota · ${model.label} · ${quotaValue}${reset}${overflow}`,
      `${mark}Quota · ${shortLabel} · ${quotaValue}${reset}${overflow}`,
      `${mark}Quota · ${shortLabel} · ${quotaValue}${overflow}`,
      `${mark}${shortLabel} ${quotaValue}${overflow}`,
      `${mark}${account || shortLabel} ${quotaValue}${overflow}`,
      `${mark}${quotaValue}${overflow}`,
    ];
    const announcements = announcementVariants(special);
    // Quota identity and state stay mandatory; only secondary detail degrades.
    const pairs = [
      [0, 0], [0, 1], [0, 2],
      [2, 1], [2, 2], [2, 3], [2, 4],
      [1, 2], [1, 3], [1, 4],
      [3, 3], [3, 4],
      [4, 4],
      [0, 5], [0, 6], [4, 5], [4, 6],
    ];
    const candidate = pairs
      .map(([a, q]) => `${announcements[a]} · ${quotaVariants[q]}`)
      .find(value => length(value) <= width);
    text = candidate || text;
  }

  return { text: shorten(text, width), stale: model.stale, remaining, unavailable: model.unavailable, error: Boolean(status.error), special: Boolean(special) };
}

/** Role-colored chip, with no status-bar-like background or full-width rule. */
export function renderLine(sdk, model, t, available) {
  const { h, Box, Text } = sdk;
  const color = model.error || model.unavailable ? t.color.error : model.stale || model.remaining === null ? t.color.muted
    : model.remaining <= 10 ? t.color.error : model.remaining <= 30 ? t.color.warn : t.color.ok;
  const chunks = model.text.split(/(Quota|[█░]+|(?:\d+|\?)%(?: left)?|STALE|refresh error)/g).filter(Boolean);
  return h(Box, { width: available, height: 1, justifyContent: 'flex-end' },
    h(Text, { wrap: 'truncate-end' }, ...chunks.map((chunk, index) => h(Text, {
      key: index, color: chunk === 'Quota' ? t.color.primary : chunk === 'refresh error' ? t.color.error
        : /[█░%]/.test(chunk) ? color : t.color.muted,
      bold: chunk === 'Quota',
    }, chunk))));
}

/** Poll only while mounted. Cache reads never wait for the network refresh. */
export function createPoller({ read, refresh, onUpdate, now = Date.now }) {
  let stopped = false, reading = false, refreshing = false, lastAttempt = -Infinity;
  let cache = {}, error = '', refreshingError = '';
  const controller = new AbortController();
  const publish = () => {
    if (!stopped) onUpdate({ cache, error: error || refreshingError, refreshing });
  };
  async function load() {
    try { cache = await read(); error = ''; }
    catch { cache = {}; error = 'Cache unavailable'; }
  }
  return {
    async tick() {
      if (stopped || reading) return;
      reading = true;
      try { await load(); } finally { reading = false; }
      if (stopped) return;
      publish();
      if (panelModel(cache, [], now()).stale && !refreshing && now() - lastAttempt >= 60000) {
        refreshing = true;
        lastAttempt = now();
        refreshingError = '';
        publish();
        void (async () => {
          try {
            await refresh(controller.signal);
            if (!stopped) await load();
          } catch { refreshingError = 'Refresh failed; retry in 60s'; }
          finally { refreshing = false; publish(); }
        })();
      }
    },
    stop() { stopped = true; controller.abort(); },
  };
}

const hermesHome = () => process.env.HERMES_HOME?.trim() || join(homedir(), '.hermes');

/**
 * Display preference only: account selector + whether the dock auto-opens.
 * Deliberately NEXT TO the cache, never inside `tui-widgets/`: the TUI reloads
 * every widget when that directory changes, so writing the preference there
 * would remount the dock and write again — an endless reload/write loop.
 */
export const PREFS_VERSION = 1;
const defaultPrefs = () => ({ selected: null, autoOpen: true });

export const prefsPath = (home = hermesHome()) => join(home, 'quota-panel.state.json');

/** Anything unrecognised falls back to a closed default; never throws. */
export function normalizePrefs(value) {
  const prefs = defaultPrefs();
  if (!object(value)) return prefs;
  if (typeof value.selected === 'string' && SELECTOR.test(value.selected.trim())) prefs.selected = value.selected.trim();
  if (typeof value.autoOpen === 'boolean') prefs.autoOpen = value.autoOpen;
  return prefs;
}

export async function readPrefs(home = hermesHome()) {
  const path = prefsPath(home);
  try {
    const info = await stat(path);
    if (!info.isFile() || info.size > 8192) return defaultPrefs();
    return normalizePrefs(JSON.parse(await readFile(path, 'utf8')));
  } catch { return defaultPrefs(); }
}

/** Atomic, owner-only, best effort: a read-only HOME must not break the dock.
 *  Rewriting an unchanged preference is skipped, so a remount cannot cause churn. */
export async function writePrefs(prefs, home = hermesHome()) {
  const next = normalizePrefs(prefs);
  const current = await readPrefs(home);
  if (current.selected === next.selected && current.autoOpen === next.autoOpen) return next;
  const path = prefsPath(home);
  const tmp = `${path}.${process.pid}.tmp`;
  try {
    await mkdir(dirname(path), { recursive: true });
    await writeFile(tmp, `${JSON.stringify({ version: PREFS_VERSION, ...next }, null, 2)}\n`, { mode: 0o600 });
    await rename(tmp, path);
    return next;
  } catch {
    try { await rm(tmp, { force: true }); } catch { /* ignore */ }
    return null;
  }
}

export async function readCache() {
  const path = join(hermesHome(), 'quota_cache.json');
  if ((await stat(path)).size > 1024 * 1024) throw new Error('Cache too large');
  const cache = JSON.parse(await readFile(path, 'utf8'));
  if (!object(cache) || !object(cache.providers)) throw new Error('Invalid cache');
  return cache;
}

function refreshCache(signal) {
  return new Promise((resolve, reject) => {
    // Fixed argv, no shell or selectors in the command. Existing plugin owns all I/O.
    execFile('hermes', ['quota', 'refresh'], {
      signal, timeout: 45000, killSignal: 'SIGKILL', maxBuffer: 65536, windowsHide: true,
    }, error => error ? reject(error) : resolve());
  });
}

/** Serialized so a fast open/close/open sequence cannot write an older state last. */
export function createPrefsStore(home = hermesHome()) {
  let queue = Promise.resolve();
  let current = defaultPrefs();
  const store = {
    get: () => current,
    async load() { current = await readPrefs(home); return current; },
    save(patch) {
      const next = { ...current, ...patch };
      current = normalizePrefs(next);
      queue = queue.then(async () => {
        const written = await writePrefs({ ...(await readPrefs(home)), ...patch }, home);
        return written;
      }).catch(() => null);
      return queue;
    },
    idle: () => queue,
  };
  return store;
}

/**
 * Auto-open the saved dock once per TUI process. The `restored` flag lives on
 * globalThis because a /widgets-reload re-imports this module with a fresh
 * cache-busting query — a module-local flag would reopen a dock the user closed.
 */
export async function restoreSavedPanel(sdk, panel, { home, force = false } = {}) {
  const prefs = await readPrefs(home);
  const shared = globalThis.__hermesQuotaPanel ??= { restored: false };
  if (!prefs.autoOpen || !prefs.selected) return null;
  if (shared.restored && !force) return null;
  if (typeof sdk?.openWidget !== 'function') return null;
  shared.restored = true;
  sdk.openWidget(panel, { selected: [prefs.selected] });
  return prefs.selected;
}

/** Mounting the dock IS the preference: it is what survives a restart. */
export function markDockVisible(prefs, selected) {
  return prefs.save(selected.length ? { selected: selected[0], autoOpen: true } : { autoOpen: true });
}

export default function register(sdk) {
  const { h, Box, Text, React, defineWidgetApp, Overlay, Dialog } = sdk;
  const prefs = createPrefsStore();
  let chosen = null;
  let panel, picker;
  function Panel({ selected, t, cols }) {
    const [snapshot, setSnapshot] = React.useState({ cache: {}, error: '', refreshing: false });
    const [now, setNow] = React.useState(Date.now);
    React.useEffect(() => {
      if (!selected.length) {
        sdk.openWidget(picker, picker.init(''));
        return;
      }
      // Showing the dock IS the preference: this is what makes it survive a
      // restart. Only persist a selection the cache still resolves, so an old
      // TUI remounting a stale panel cannot clobber the last good choice.
      void (async () => {
        try {
          if (rememberable(pickerOptions(await readCache()), selected)) await markDockVisible(prefs, selected);
        } catch { /* an unreadable cache keeps the previous preference */ }
      })();
      const poller = createPoller({ read: readCache, refresh: refreshCache, onUpdate: setSnapshot });
      void poller.tick();
      const timer = setInterval(() => { setNow(Date.now()); void poller.tick(); }, 15000);
      return () => { clearInterval(timer); poller.stop(); };
    }, []);
    // Host dock reserves two trailing columns, plus outer chrome padding.
    const available = Math.max(1, cols - 4);
    const model = lineModel(snapshot.cache, selected[0], available, now, snapshot);
    return renderLine(sdk, model, t, available);
  }

  function Picker({ state, t, cols, rows }) {
    const [noticeCache, setNoticeCache] = React.useState(null);
    const [now, setNow] = React.useState(Date.now());
    React.useEffect(() => {
      let mounted = true;
      // Start on the remembered provider so a restart does not silently move
      // the cursor back to the first cached row.
      Promise.all([readCache(), prefs.load()]).then(([cache, saved]) => {
        if (!mounted) return;
        const options = pickerOptions(cache);
        const remembered = chosen || saved.selected || null;
        setNoticeCache(cache);
        sdk.updateWidget(picker, current => ({ ...current, phase: 'ready', options, current: remembered,
          index: rememberedIndex(options, remembered), stale: panelModel(cache).stale }));
      }, () => {
        if (mounted) sdk.updateWidget(picker, current => ({ ...current, phase: 'error' }));
      });
      const timer = setInterval(() => setNow(Date.now()), 15000);
      return () => { mounted = false; clearInterval(timer); };
    }, []);
    const special = specialNoticeText(noticeCache, now, cols);
    const count = Math.max(1, Math.min(7, rows - 14));
    const start = Math.max(0, Math.min(state.index - Math.floor(count / 2), state.options.length - count));
    const visible = state.options.slice(start, start + count);
    return h(Overlay, { zone: 'center', backdrop: true },
      h(Dialog, { width: Math.max(12, Math.min(76, cols)), title: 'Quota · choose provider / account', hint: '↑↓ choose · Enter confirm · Esc cancel' },
        h(Box, { flexDirection: 'column' },
          h(Text, { color: t.color.muted, wrap: 'truncate-end' }, 'From quota cache · no key scan · display only'),
          special ? h(Text, { key: 'special', color: t.color.muted, wrap: 'truncate-end' }, special) : null,
          ...(state.phase === 'loading' ? [h(Text, { key: 'loading' }, 'Reading cache…')]
            : state.phase === 'error' ? [h(Text, { key: 'error', color: t.color.error }, 'Cache unavailable. Run /quota refresh first.')]
              : !visible.length ? [h(Text, { key: 'empty' }, 'No configured providers in cache. Run /quota refresh.')]
                : visible.map((option, index) => h(Text, { key: option.id, wrap: 'truncate-end',
                  color: start + index === state.index ? t.color.primary : t.color.text,
                  bold: start + index === state.index }, `${start + index === state.index ? '›' : ' '} ${option.label} · ${option.unavailable ? 'unavailable' : 'cached'}${option.id === state.current ? ' · current' : ''}`))),
          h(Text, { color: t.color.muted }, state.phase === 'ready' ? `${state.options.length ? state.index + 1 : 0}/${state.options.length}${state.stale ? ' · STALE snapshot' : ''}` : ' '),
        )),
    );
  }
  panel = defineWidgetApp({
    id: 'quota-panel', help: 'Toggle one-account quota bar above the composer',
    mode: 'ambient', zone: 'dock-top', width: 100,
    usage: '/quota-panel [provider:account|on|off] · /quota-select to choose interactively',
    init(arg) {
      const value = arg.trim();
      if (/^off$/i.test(value)) { void prefs.save({ autoOpen: false }); return null; }
      const selected = value && !/^on$/i.test(value) ? [...new Set(value.split(/[\s,]+/).filter(Boolean))] : [];
      if (selected.length > 1 || selected.some(id => !SELECTOR.test(id))) return null;
      const remembered = chosen || prefs.get().selected;
      if (!selected.length && remembered) selected.push(remembered);
      return { selected };
    },
    reduce: state => state,
    render: ({ state, t, cols }) => h(Panel, { key: state.selected.join(','), selected: state.selected, t, cols }),
  });
  picker = defineWidgetApp({
    id: 'quota-select', help: 'Choose a cached configured provider/account for the quota bar', mode: 'modal',
    init: () => ({ phase: 'loading', options: [], index: 0, special: '' }),
    reduce(state, { ch, key }) {
      if (key.escape || (key.ctrl && ch === 'c') || ch === 'q') return null;
      if (key.return && state.options[state.index]) {
        chosen = state.options[state.index].id;
        void prefs.save({ selected: chosen, autoOpen: true });
        sdk.openWidget(panel, { selected: [chosen] });
        return null;
      }
      if (key.downArrow && state.options.length) return { ...state, index: (state.index + 1) % state.options.length };
      if (key.upArrow && state.options.length) return { ...state, index: (state.index + state.options.length - 1) % state.options.length };
      return state;
    },
    render: ({ state, t, cols, rows }) => h(Picker, { state, t, cols, rows }),
  });
  // Auto-open the remembered dock (once per process, never on /widgets-reload).
  void (async () => {
    await restoreSavedPanel(sdk, panel);
    const saved = await prefs.load();
    if (!chosen && saved.selected) chosen = saved.selected;
  })();
}
