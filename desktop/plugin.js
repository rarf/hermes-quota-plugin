/**
 * Hermes desktop widget for the `quota` plugin.
 *
 * Statusbar (style inspired by CodexBar):
 *   * a single chip: worst remaining % across providers + tonal bar.
 *   * hover gives a button-like affordance; click opens the /quota route
 *     (full provider list with per-window % + reset breakdown).
 *   * respects the "Show status bar" setting; docked pane off by default.
 *
 * Reset label format (persisted in ctx.storage under `resetFormat`):
 *   - relative (default): short countdown, e.g. "resets in 3h 12m".
 *   - absolute: date + time, e.g. "Aug 11, 2:30 PM".
 *
 * Also: route /quota (full pane), side pane (right), sidebar nav row.
 *
 * Data path: ctx.rest('/quota') → gateway → GET /api/plugins/quota/quota.
 *
 * SINGLE SOURCE FILE: keep only at <hermes home>/desktop-plugins/quota/plugin.js.
 * Do NOT also place a copy under profiles/<name>/desktop-plugins/quota — the
 * desktop loads both and a duplicate top-level binding collides in the shared
 * module scope and breaks the plugin load (SyntaxError).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls. Only these imports
 * resolve: @hermes/plugin-sdk, react, react/jsx-runtime. `ctx` comes from
 * register(ctx), not from an import.
 */

import {
  atom,
  cn,
  fmtDayTime,
  host,
  icons,
  PANES_AREA,
  Popover,
  PopoverContent,
  PopoverTrigger,
  ROUTES_AREA,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  StatusDot,
  Switch,
  Input,
  Tip,
  useMutation,
  usePluginI18n,
  useQuery,
  useQueryClient,
  useValue,
  STATUSBAR_AREAS,
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'quota'

// Module-level ctx handle (set in register). The data hook below needs it.
let CTX = null

// ---- persisted settings (ctx.storage + useValue atoms) -------------------

const RESET_FORMAT_KEY = 'resetFormat' // 'relative' | 'absolute'
const RESET_FORMAT_DEFAULT = 'relative'

const resetFormatAtom = atom(RESET_FORMAT_DEFAULT)

function readStored(key, fallback) {
  try {
    const v = CTX.storage.get(key)
    return v == null ? fallback : v
  } catch {
    return fallback
  }
}

function applyStored(key, atomRef, allowed, fallback) {
  try {
    const v = CTX.storage.get(key)
    atomRef.set(allowed.includes(v) ? v : fallback)
  } catch {
    /* noop */
  }
}

function setStored(key, value, atomRef) {
  try {
    CTX.storage.set(key, value)
    atomRef.set(value)
  } catch {
    /* noop */
  }
}

function applyStoredResetFormat() {
  applyStored(RESET_FORMAT_KEY, resetFormatAtom, ['relative', 'absolute'], RESET_FORMAT_DEFAULT)
}

function setResetFormat(fmt) {
  setStored(RESET_FORMAT_KEY, fmt, resetFormatAtom)
}

// showUnconfigured: show providers whose status is 'unavailable' in the pane.
const SHOW_UNCONFIGURED_KEY = 'showUnconfigured'
const SHOW_UNCONFIGURED_DEFAULT = false

const showUnconfiguredAtom = atom(SHOW_UNCONFIGURED_DEFAULT)

function applyStoredShowUnconfigured() {
  try {
    // Coerce to a boolean so a persisted `true` survives a reload; any other
    // stored value (missing / false) resolves to the `false` default.
    showUnconfiguredAtom.set(readStored(SHOW_UNCONFIGURED_KEY, SHOW_UNCONFIGURED_DEFAULT) === true)
  } catch {
    /* noop */
  }
}

function setShowUnconfigured(v) {
  setStored(SHOW_UNCONFIGURED_KEY, !!v, showUnconfiguredAtom)
}

// Surface toggles: status bar on by default; docked pane off by default.
const SHOW_STATUSBAR_KEY = 'showStatusBar'
const SHOW_STATUSBAR_DEFAULT = true
const SHOW_DOCKED_PANE_KEY = 'showDockedPane'
const SHOW_DOCKED_PANE_DEFAULT = false

const showStatusBarAtom = atom(SHOW_STATUSBAR_DEFAULT)
const showDockedPaneAtom = atom(SHOW_DOCKED_PANE_DEFAULT)

function applyStoredBoolean(key, atomRef, fallback) {
  try {
    atomRef.set(readStored(key, fallback) === true)
  } catch {
    /* noop */
  }
}

function applyStoredSurfaceVisibility() {
  applyStoredBoolean(SHOW_STATUSBAR_KEY, showStatusBarAtom, SHOW_STATUSBAR_DEFAULT)
  applyStoredBoolean(SHOW_DOCKED_PANE_KEY, showDockedPaneAtom, SHOW_DOCKED_PANE_DEFAULT)
}

function setShowStatusBar(v) {
  setStored(SHOW_STATUSBAR_KEY, !!v, showStatusBarAtom)
}

function setShowDockedPane(v) {
  setStored(SHOW_DOCKED_PANE_KEY, !!v, showDockedPaneAtom)
  if (showDockedPaneAtom.get()) {
    registerDockedPane()
  } else {
    clearDockedPane()
  }
}

// statusbarMode: 'all' renders every configured provider side by side;
// 'worst' renders the single worst-provider chip (previous behaviour).
const STATUSBAR_MODE_KEY = 'statusbarMode'
const STATUSBAR_MODE_DEFAULT = 'all'

const statusbarModeAtom = atom(STATUSBAR_MODE_DEFAULT)

function applyStoredStatusbarMode() {
  applyStored(STATUSBAR_MODE_KEY, statusbarModeAtom, ['all', 'worst'], STATUSBAR_MODE_DEFAULT)
}

function setStatusbarMode(mode) {
  setStored(STATUSBAR_MODE_KEY, mode, statusbarModeAtom)
}

// refreshInterval: statusbar/pane poll cadence in seconds (persisted as number).
const REFRESH_INTERVAL_KEY = 'refreshInterval'
const REFRESH_INTERVAL_DEFAULT = 60
const REFRESH_INTERVAL_MIN = 15
const REFRESH_INTERVAL_MAX = 600

const refreshIntervalAtom = atom(REFRESH_INTERVAL_DEFAULT)

function applyStoredRefreshInterval() {
  try {
    const v = CTX.storage.get(REFRESH_INTERVAL_KEY)
    const n = Number(v)
    const clamped = isNaN(n)
      ? REFRESH_INTERVAL_DEFAULT
      : Math.max(REFRESH_INTERVAL_MIN, Math.min(REFRESH_INTERVAL_MAX, Math.round(n)))
    refreshIntervalAtom.set(clamped)
  } catch {
    /* noop */
  }
}

function setRefreshInterval(seconds) {
  const n = Number(seconds)
  const clamped = isNaN(n)
    ? REFRESH_INTERVAL_DEFAULT
    : Math.max(REFRESH_INTERVAL_MIN, Math.min(REFRESH_INTERVAL_MAX, Math.round(n)))
  setStored(REFRESH_INTERVAL_KEY, clamped, refreshIntervalAtom)
}

// Apply every persisted setting from storage on plugin load.
function applyStoredAll() {
  applyStoredResetFormat()
  applyStoredShowUnconfigured()
  applyStoredSurfaceVisibility()
  applyStoredStatusbarMode()
  applyStoredRefreshInterval()
}

// ---- severity helpers -----------------------------------------------------

function toneForRemaining(pct) {
  if (pct == null) return 'muted'
  if (pct <= 15) return 'bad'
  if (pct <= 40) return 'warn'
  return 'good'
}

function worstWindow(provider) {
  let worst = null
  for (const w of provider.windows || []) {
    if (w.remaining_pct == null) continue
    if (worst == null || w.remaining_pct < worst) worst = w.remaining_pct
  }
  return worst
}

// Short absolute reset: today/tomorrow/time, else "Aug 11, 2:30 PM".
function absoluteReset(resetIso) {
  if (!resetIso) return ''
  try {
    const dt = new Date(resetIso)
    if (isNaN(dt.getTime())) return ''
    const now = new Date()
    const sameDay = dt.toDateString() === now.toDateString()
    const tomorrow = new Date(now)
    tomorrow.setDate(now.getDate() + 1)
    const isTomorrow = dt.toDateString() === tomorrow.toDateString()
    const time = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (sameDay) return `today ${time}`
    if (isTomorrow) return `tomorrow ${time}`
    return fmtDayTime.format(dt)
  } catch {
    return ''
  }
}

// Short relative countdown, e.g. "resets in 3h 12m". `nowMs` lets the UI tick.
function relativeCountdown(resetIso, nowMs = Date.now()) {
  if (!resetIso) return ''
  try {
    const dt = new Date(resetIso)
    const target = dt.getTime()
    if (isNaN(target)) return ''
    const diff = target - nowMs
    if (diff <= 0) return 'resetting…'
    const totalMin = Math.floor(diff / 60_000)
    const days = Math.floor(totalMin / 1440)
    const hours = Math.floor((totalMin % 1440) / 60)
    const mins = totalMin % 60
    if (days > 0) return `${days}d ${hours}h`
    if (hours > 0) return `${hours}h ${mins}m`
    if (mins > 0) return `${mins}m`
    return '<1m'
  } catch {
    return ''
  }
}

// Format a reset timestamp per the persisted format setting.
function formatReset(resetIso, format) {
  if (!resetIso) return ''
  if (format === 'absolute') return absoluteReset(resetIso)
  const relative = relativeCountdown(resetIso)
  const absolute = absoluteReset(resetIso)
  return absolute ? `${relative} (${absolute})` : relative
}

function isConfigured(provider) {
  return provider && !provider.unavailable_reason
}

// Re-render on an interval so relative countdowns tick live.
function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

// ---- data hook ------------------------------------------------------------

function useQuota() {
  const intervalMs = useValue(refreshIntervalAtom) * 1000
  return useQuery({
    queryKey: ['quota', 'widget'],
    queryFn: () => CTX.rest('/quota'),
    refetchInterval: intervalMs,
    staleTime: Math.max(10_000, Math.floor(intervalMs / 2)),
    retry: 1,
  })
}

// ---- single chip ----------------------------------------------------------

function QuotaChip() {
  const t = usePluginI18n(ID)
  const { data, isError } = useQuota()
  const resetFormat = useValue(resetFormatAtom)
  const now = useNow()
  if (isError || !data || !data.providers) {
    return jsx(StatusDot, { tone: 'muted' })
  }
  const providers = Object.values(data.providers)
  if (providers.length === 0) return jsx(StatusDot, { tone: 'muted' })
  let worst = null
  let worstLabel = ''
  let worstReset = null
  for (const p of providers) {
    const r = worstWindow(p)
    if (r == null) continue
    if (worst == null || r < worst) {
      worst = r
      worstLabel = p.label || ''
      const resets = (p.windows || []).map((w) => w.reset_at).filter(Boolean)
      worstReset = resets.length ? resets[0] : null
    }
  }
  if (worst == null) return jsx(StatusDot, { tone: 'muted' })
  const tone = toneForRemaining(worst)
  const countdown = formatReset(worstReset, resetFormat)
  return jsx(
    Tip,
    {
      label: t('chipTip', worst, worstLabel, countdown),
    },
    jsx(
      'button',
      {
        type: 'button',
        className: cn(
          'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]',
          'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors'
        ),
        onClick: () => host.navigate('/quota'),
        children: jsxs('span', {
          className: 'inline-flex items-center gap-1',
          children: [jsx(StatusDot, { tone }), jsx('span', { children: `${worst}%` })],
        }),
      }
    )
  )
}

// ---- per-provider popover (icons / auto>=2 mode) --------------------------

function ProviderPopoverContent({ provider, resetFormat }) {
  const t = usePluginI18n(ID)
  const windows = provider.windows || []
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', { className: 'text-xs font-medium text-(--ui-text-primary)', children: provider.label || '' }),
          provider.plan ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: provider.plan }) : null,
        ],
      }),
      windows.length === 0
        ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: t('noData') })
        : jsx(
            'div',
            {
              className: 'flex flex-col divide-y divide-(--ui-stroke-secondary)',
              children: windows.map((w, i) => {
                const tone = toneForRemaining(w.remaining_pct)
                const reset = formatReset(w.reset_at, resetFormat)
                return jsxs(
                  'div',
                  {
                    className: 'flex flex-col gap-0.5 py-1.5',
                    children: [
                      jsxs('div', {
                        className: 'flex items-center justify-between gap-2 text-[0.6875rem]',
                        children: [
                          jsx('span', { className: 'inline-flex items-center gap-1 text-(--ui-text-secondary)', children: [jsx(StatusDot, { tone }), jsx('span', { children: w.label || 'window' })] }),
                          jsx('span', { className: 'tabular-nums text-(--ui-text-tertiary)', children: w.remaining_pct == null ? '—' : `${w.remaining_pct}%` }),
                        ],
                      }),
                      reset ? jsx('div', { className: 'pl-3 text-[0.625rem] text-(--ui-text-quaternary)', children: t('reset', reset) }) : null,
                    ],
                  },
                  `w-${i}`
                )
              }),
            }
          ),
    ],
  })
}

function ProviderItem({ pid, provider }) {
  const t = usePluginI18n(ID)
  const resetFormat = useValue(resetFormatAtom)
  const now = useNow()
  const r = worstWindow(provider)
  const tone = toneForRemaining(r)
  const reset = (provider.windows || []).map((w) => w.reset_at).filter(Boolean)[0]
  const countdown = formatReset(reset, resetFormat)
  return jsx(
    Popover,
    {},
    jsx(
      Tip,
      {
        label: t('providerTip', provider.label || pid, r == null ? '—' : `${r}%`, countdown),
      },
      jsx(
        PopoverTrigger,
        { asChild: true },
        jsx(
          'button',
          {
            type: 'button',
            className: cn(
              'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]',
              'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors'
            ),
            children: jsxs('span', {
              className: 'inline-flex items-center gap-1',
              children: [jsx(StatusDot, { tone }), jsx('span', { children: provider.label || pid })],
            }),
          }
        )
      )
    ),
    jsx(
      PopoverContent,
      { align: 'end', side: 'bottom', className: 'w-60' },
      jsx(ProviderPopoverContent, { provider, resetFormat })
    )
  )
}

// ---- statusbar ------------------------------------------------------------
// One static statusbar item (registered once in register(ctx)). The StatusBar
// component re-renders itself via hooks; it never re-registers.

let _paneDisposer = null

function clearDockedPane() {
  if (!_paneDisposer) return
  try {
    _paneDisposer()
  } catch {
    /* noop */
  }
  _paneDisposer = null
}

function registerDockedPane() {
  clearDockedPane()
  if (!CTX || !showDockedPaneAtom.get()) return
  try {
    _paneDisposer = CTX.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'quota',
      data: {
        placement: 'right',
        dock: { pane: 'workspace', pos: 'right' },
        width: '300px',
      },
      render: () => jsx(QuotaPane, {}),
    })
  } catch {
    _paneDisposer = null
  }
}

// ---- statusbar (single static item) ---------------------------------------
// Registered exactly ONCE (see register(ctx) below). The render component uses
// hooks (useQuota / useValue) and re-renders itself when data or settings
// change. It never calls CTX.register, so there is no registration feedback
// loop — that loop (a controller that re-registered items on every poll) is
// what hung the renderer before.
//
// STEP 1 (baby steps): one stable statusbar item that internally renders
// either the per-provider icons (icons / auto>=2) or the single QuotaChip
// (single / auto<2). No dynamic (re-)registration.

function QuotaChipWithBar() {
  // Final: chip shows worst provider + bar; click opens the /quota route
  // (full provider list). Hover gives a button-like affordance.
  const [hover, setHover] = useState(false)
  const { data } = useQuota()
  const providers = data && data.providers ? Object.values(data.providers) : []
  let worst = null
  let worstLabel = ''
  for (const p of providers) {
    const r = worstWindow(p)
    if (r == null) continue
    if (worst == null || r < worst) {
      worst = r
      worstLabel = p.label || ''
    }
  }
  if (worst == null) return jsx('span', { children: 'Q:none' })
  const tone = toneForRemaining(worst)
  const fill = tone === 'bad' ? 'var(--ui-danger)' : tone === 'warn' ? 'var(--ui-warning, #d9a23a)' : 'var(--ui-accent)'
  const onClick = () => {
    if (typeof host.navigate === 'function') host.navigate('/quota')
  }
  return jsxs('span', {
    className: 'inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded cursor-pointer',
    style: hover ? { background: 'var(--ui-stroke-secondary)' } : undefined,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    onClick,
    children: [
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-secondary)', children: worstLabel + ' ' + worst + '%' }),
      jsx('span', {
        className: 'inline-block h-1.5 w-10 overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        children: jsx('span', { className: 'block h-full rounded-full', style: { width: worst + '%', background: fill } }),
      }),
    ],
  })
}

// ---- per-provider statusbar chip (plain chip, no popover) ------------------

function ProviderChip({ pid, provider }) {
  const r = worstWindow(provider)
  const tone = toneForRemaining(r)
  const dot =
    tone === 'bad'
      ? 'var(--ui-danger)'
      : tone === 'warn'
        ? 'var(--ui-warning, #d9a23a)'
        : tone === 'good'
          ? 'var(--ui-accent)'
          : 'var(--ui-stroke-secondary)'
  const label = provider.label || pid
  return jsxs(
    'button',
    {
      type: 'button',
      title: `${label} · ${r == null ? '—' : `${r}%`}`,
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors'
      ),
      onClick: () => {
        if (typeof host.navigate === 'function') host.navigate('/quota')
      },
      children: [
        jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full', style: { background: dot } }),
        jsx('span', { children: label }),
        jsx('span', { className: 'tabular-nums', children: r == null ? '—' : `${r}%` }),
      ],
    }
  )
}

function StatusBar() {
  // 'worst' → previous single worst chip + bar. 'all' → one plain chip per
  // configured provider, side by side. Both respect "Show status bar".
  const showStatusBar = useValue(showStatusBarAtom)
  const mode = useValue(statusbarModeAtom)
  const showUnconfigured = useValue(showUnconfiguredAtom)
  const { data, isError } = useQuota()
  if (!showStatusBar) return null
  if (mode === 'worst') return jsx(QuotaChipWithBar, {})
  if (isError || !data || !data.providers) return jsx(StatusDot, { tone: 'muted' })
  const entries = Object.entries(data.providers).filter(([, p]) => showUnconfigured || isConfigured(p))
  if (entries.length === 0) return jsx(StatusDot, { tone: 'muted' })
  return jsx(
    'span',
    { className: 'inline-flex h-full items-center', children: entries.map(([pid, p]) => jsx(ProviderChip, { pid, provider: p, key: pid })) }
  )
}

// ---- pane -----------------------------------------------------------------

function QuotaBar({ value, tone }) {
  const pct = Math.max(0, Math.min(100, value == null ? 0 : value))
  const fill =
    tone === 'bad' ? 'var(--ui-danger)' : tone === 'warn' ? 'var(--ui-warning, #d9a23a)' : 'var(--ui-accent)'
  return jsx('div', {
    className: 'h-1.5 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
    children: jsx('div', { className: 'h-full rounded-full transition-all', style: { width: `${pct}%`, background: fill } }),
  })
}

function ProviderRow({ id, provider }) {
  const t = usePluginI18n(ID)
  const resetFormat = useValue(resetFormatAtom)
  const reason = provider.unavailable_reason
  if (reason) {
    return jsxs('div', {
      className: 'flex flex-col gap-0.5 py-1.5',
      children: [
        jsxs('div', {
          className: 'flex items-center gap-1.5 text-sm',
          children: [jsx(StatusDot, { tone: 'muted' }), jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: provider.label || id })],
        }),
        jsx('div', { className: 'pl-3.5 text-xs text-(--ui-text-tertiary)', children: t('unavailable', reason) }),
      ],
    })
  }
  const windows = provider.windows || []
  if (windows.length === 0) {
    return jsxs('div', {
      className: 'flex items-center gap-1.5 py-1.5 text-sm',
      children: [
        jsx(StatusDot, { tone: 'muted' }),
        jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: provider.label || id }),
        jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: t('noData') }),
      ],
    })
  }
  return jsxs('div', {
    className: 'flex flex-col gap-1.5 py-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary)', children: provider.label || id }),
          provider.plan ? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: provider.plan }) : null,
        ],
      }),
      ...windows.map((w, i) => {
        const tone = toneForRemaining(w.remaining_pct)
        return jsxs(
          'div',
          {
            className: 'flex flex-col gap-1',
            children: [
              jsxs('div', {
                className: 'flex items-center justify-between gap-2 text-xs',
                children: [
                  jsx('span', { className: 'text-(--ui-text-secondary)', children: w.label || 'window' }),
                  jsx('span', { className: 'tabular-nums text-(--ui-text-tertiary)', children: w.remaining_pct == null ? '—' : `${w.remaining_pct}%` }),
                ],
              }),
              jsx(QuotaBar, { value: w.remaining_pct, tone }),
              w.reset_at ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('reset', formatReset(w.reset_at, resetFormat)) }) : null,
            ],
          },
          `${id}-w-${i}`
        )
      }),
    ],
  })
}

function ResetFormatControl() {
  const t = usePluginI18n(ID)
  const resetFormat = useValue(resetFormatAtom)
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2',
    children: [
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: t('resetFormatLabel') }),
      jsx(SegmentedControl, {
        value: resetFormat,
        onChange: (v) => setResetFormat(v),
        options: [
          { id: 'relative', label: t('relative') },
          { id: 'absolute', label: t('absolute') },
        ],
      }),
    ],
  })
}

// Safe render of an SDK lucide icon: missing exports resolve to `undefined`
// (ES module namespace) rather than throwing, so fall back to a glyph.
function SdkIcon({ name, className, fallback = '•' }) {
  const C = icons[name]
  return C ? jsx(C, { className }) : jsx('span', { className, children: fallback })
}

function ShowUnconfiguredControl() {
  const t = usePluginI18n(ID)
  const show = useValue(showUnconfiguredAtom)
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2',
    children: [
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: t('showUnconfiguredLabel') }),
      jsx(Switch, { checked: show, onCheckedChange: (v) => setShowUnconfigured(v) }),
    ],
  })
}

function ShowStatusBarControl() {
  const t = usePluginI18n(ID)
  const show = useValue(showStatusBarAtom)
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: t('showStatusBarLabel') }),
          jsx(Switch, { checked: show, onCheckedChange: (v) => setShowStatusBar(v) }),
        ],
      }),
      jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('showStatusBarHint') }),
    ],
  })
}

function ShowDockedPaneControl() {
  const t = usePluginI18n(ID)
  const show = useValue(showDockedPaneAtom)
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2',
    children: [
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: t('showDockedPaneLabel') }),
      jsx(Switch, { checked: show, onCheckedChange: (v) => setShowDockedPane(v) }),
    ],
  })
}

function RefreshIntervalControl() {
  const t = usePluginI18n(ID)
  const seconds = useValue(refreshIntervalAtom)
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: t('refreshIntervalLabel') }),
          jsxs('div', {
            className: 'flex items-center gap-1',
            children: [
              jsx(Input, {
                type: 'number',
                min: REFRESH_INTERVAL_MIN,
                max: REFRESH_INTERVAL_MAX,
                step: 5,
                value: seconds,
                onChange: (e) => setRefreshInterval(e && e.target ? e.target.value : e),
                className: 'h-6 w-16 text-right text-xs',
              }),
              jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('seconds') }),
            ],
          }),
        ],
      }),
      jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('refreshIntervalHint', REFRESH_INTERVAL_MIN, REFRESH_INTERVAL_MAX) }),
    ],
  })
}

function StatusbarModeControl() {
  const t = usePluginI18n(ID)
  const mode = useValue(statusbarModeAtom)
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2',
    children: [
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: t('statusbarModeLabel') }),
      jsx(SegmentedControl, {
        value: mode,
        onChange: (v) => setStatusbarMode(v),
        options: [
          { id: 'all', label: t('statusbarModeAll') },
          { id: 'worst', label: t('statusbarModeWorst') },
        ],
      }),
    ],
  })
}

function QuotaSettings() {
  const t = usePluginI18n(ID)
  return jsxs('div', {
    className: 'flex flex-col gap-3 overflow-y-auto py-1',
    children: [
      jsx(ShowStatusBarControl, {}),
      jsx(StatusbarModeControl, {}),
      jsx(ShowDockedPaneControl, {}),
      jsx(ResetFormatControl, {}),
      jsx(ShowUnconfiguredControl, {}),
      jsx(RefreshIntervalControl, {}),
    ],
  })
}

function QuotaPane() {
  const t = usePluginI18n(ID)
  const qc = useQueryClient()
  const [view, setView] = useState('list') // 'list' | 'settings'
  const showUnconfigured = useValue(showUnconfiguredAtom)
  const { data, isError, isLoading, refetch } = useQuota()
  const refresh = useMutation({
    mutationFn: () => CTX.rest('/refresh', { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['quota', 'widget'] }),
  })

  const headerTitle = view === 'settings' ? t('settingsTitle') : t('paneTitle')

  let body
  if (view === 'settings') {
    body = jsx(QuotaSettings, {})
  } else if (isLoading) {
    body = jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t('loading') })
  } else if (isError) {
    body = jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t('error') })
  } else if (!data || !data.providers || Object.keys(data.providers).length === 0) {
    body = jsxs('div', { className: 'flex flex-col gap-1 text-xs text-(--ui-text-tertiary)', children: [jsx('div', { children: t('empty') }), jsx('div', { children: t('emptyHint') })] })
  } else {
    const entries = showUnconfigured
      ? Object.entries(data.providers)
      : Object.entries(data.providers).filter(([, p]) => isConfigured(p))
    body = jsxs('div', {
      className: 'flex flex-col divide-y divide-(--ui-stroke-secondary)',
      children: entries.map(([id, p]) => jsx(ProviderRow, { id, provider: p, key: id })),
    })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('div', { className: 'text-sm font-medium', children: headerTitle }),
          jsxs('div', {
            className: 'flex items-center gap-1',
            children: [
              view === 'settings'
                ? jsx(
                    'button',
                    {
                      type: 'button',
                      className: cn('inline-flex h-6 w-6 items-center justify-center rounded', 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors'),
                      title: t('backTip'),
                      onClick: () => setView('list'),
                      children: jsx(SdkIcon, { name: 'ArrowLeft', className: 'h-3.5 w-3.5', fallback: '‹' }),
                    }
                  )
                : jsxs('div', {
                    className: 'flex items-center',
                    children: [
                      jsx(
                        'button',
                        {
                          type: 'button',
                          className: cn('inline-flex h-6 w-6 items-center justify-center rounded', 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors'),
                          title: t('refreshTip'),
                          disabled: refresh.isPending,
                          onClick: () => refresh.mutate(),
                          children: jsx(icons.RefreshCw, { className: cn('h-3.5 w-3.5', refresh.isPending && 'animate-spin') }),
                        }
                      ),
                      jsx(
                        'button',
                        {
                          type: 'button',
                          className: cn('inline-flex h-6 items-center justify-center gap-1 rounded px-1.5', 'text-xs text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground transition-colors'),
                          title: t('settingsTip'),
                          onClick: () => setView('settings'),
                          children: jsxs('span', {
                            className: 'inline-flex items-center gap-1',
                            children: [
                              jsx(SdkIcon, { name: 'Settings', className: 'h-3.5 w-3.5', fallback: '⚙' }),
                              jsx('span', { children: t('settingsButton') }),
                            ],
                          }),
                        }
                        ),
                    ],
                  }),
            ],
          }),
        ],
      }),
      jsx('div', { className: 'min-h-0 flex-1', children: body }),
      data && data.fetched_at
        ? jsx('div', { className: 'pt-2 text-[0.6875rem] text-(--ui-text-quaternary)', children: t('fetched', absoluteReset(data.fetched_at) || data.fetched_at) })
        : null,
    ],
  })
}

// ---- registration ---------------------------------------------------------

export default {
  id: ID,
  name: 'Quota Widget',
  defaultEnabled: true,
  register(ctx) {
    CTX = ctx
    applyStoredAll()
    registerDockedPane()
    ctx.i18n.register({
      en: {
        paneTitle: 'Quota',
        settingsTitle: 'Quota · Settings',
        chipTip: (pct, label, reset) => `Quota · lowest ${pct}% (${label})${reset ? ` · resets in ${reset}` : ''}`,
        providerTip: (label, pct, reset) => `${label} · ${pct}%${reset ? ` · resets in ${reset}` : ''}`,
        popoverReset: (reset) => `resets in ${reset}`,
        refreshTip: 'Refresh quota',
        settingsTip: 'Quota settings',
        backTip: 'Back to quota',
        loading: 'Loading…',
        error: 'Quota backend unavailable',
        empty: 'No quota data yet.',
        emptyHint: 'Quota data is being initialized automatically…',
        unavailable: (reason) => `unavailable (${reason})`,
        noData: 'no window data',
        reset: (when) => `reset ${when}`,
        fetched: (when) => `fetched ${when}`,
        resetFormatLabel: 'Reset format',
        relative: 'Relative',
        absolute: 'Absolute',
        showUnconfiguredLabel: 'Show unconfigured',
        statusbarModeLabel: 'Status bar mode',
        statusbarModeAll: 'All providers',
        statusbarModeWorst: 'Worst only',
        showStatusBarLabel: 'Show status bar indicator',
        showStatusBarHint: 'The Hermes status bar must also be visible (⌘K → Toggle status bar).',
        showDockedPaneLabel: 'Show docked quota pane',
        settingsButton: 'Settings',
        refreshIntervalLabel: 'Refresh interval',
        refreshIntervalHint: (min, max) => `Polls every ${min}–${max}s. Bar and pane update live.`,
        seconds: 's',
      },
    })

    // Single, static statusbar item. The StatusBar component re-renders itself
    // via hooks (useQuota / useValue) — it never re-registers, so no feedback
    // loop. Order 125 keeps it left of the default right-side items.
    ctx.register({
      id: 'statusbar',
      area: STATUSBAR_AREAS.right,
      order: 125,
      render: () => jsx(StatusBar, {}),
    })

    // Route page (/quota).
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/quota' },
      render: () => jsx(QuotaPane, {}),
    })

    // Sidebar nav row.
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 80,
      data: { codicon: 'pulse', label: 'Quota', path: '/quota' },
    })
  },
}
