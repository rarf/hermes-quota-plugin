# Usage

## Terminal UI

The optional Ink TUI widget provides a one-line, right-aligned quota chip above
the composer. `/quota-select` opens a provider/account picker; `/quota-panel`
toggles the display, and the chosen account is remembered so the chip is already
there on the next TUI start (`/quota-panel off` stops that). For an OpenAI Codex
account the chip also carries a validated special-reset announcement when the
width allows — see [Codex special-reset announcements](codex-special-reset.md).
See [TUI setup and commands](tui-panel.md).

## CLI dashboard

`/quota` and `hermes quota` print one block per provider **with a saved account**:

```
📊 Quota · updated 2m ago
✨ Codex special reset · scheduled Oct 22 18:00 · announced 07:55 · codex-resets.com

Anthropic · Conta 2 (Team)
  Current session    ░░░░░░░░░░    0% left · resets in 1h 6m

xAI Grok
  Weekly Limit       ██░░░░░░░░   16% left · resets in 20h 12m

Antigravity (Google AI Pro)
  Gemini · 5h        ██████████   97% left · resets in 2h 12m
  Claude/GPT · week  ███░░░░░░░   31% left · resets in 5d 20h
```

- Friendly provider names and account labels, mirroring the Desktop/TUI tables.
- A one-line **Codex special-reset announcement** when one is published — the
  publisher's scheduled estimate plus the post time. It is announcement
  metadata, never an account allowance, so it never changes a provider's bar.
  See [Codex special-reset announcements](codex-special-reset.md).
- One bar per real meter, filled by what is **left**; the reset is relative while
  it is under a week away.
- Blocks are sorted by the lowest remaining quota, so the provider that needs
  attention comes first.
- Providers with no credential are hidden, with a one-line count at the bottom.
  `/quota <provider>` (e.g. `/quota cursor`) still explains why one is absent.
  Any other unavailable reason — `config-invalid`, `rate-limited`, `no-data` —
  stays visible with its actionable detail.
- `/quota claude`, `/quota codex`, `/quota google` and similar words resolve to
  the provider they mean; a space-containing filter such as `/quota conta 2`
  works too.

## Hermes Desktop

### Status-bar chip

- Compact chip with your **lowest remaining quota**, or every provider side by
  side (`Status bar mode`).
- Hover for the full breakdown: **every provider and every window** (Session,
  Spark 5h, Spark Weekly…) with `% left` and time-to-reset.

### Context guard

The core status bar already shows the focused chat's context, so this chip only
appears when something needs action: the focused chat at or over the warning
threshold (`◔ 84%`, `+2` when other sessions are over too), or other open
Hermes sessions over it (`2 sessions ≥ 80%`). Yellow from the threshold
(70 / 80 / 90 %, default 80), red from 90 %.

- Click: the focused chat's ring (used / max tokens, compressions so far, `≈`
  when estimated), **Compress…** with a confirm step and an optional focus topic
  — it sends `/compress [topic]` through the chat's composer, exactly like
  typing it, and is disabled while the chat is working — plus the other
  sessions over the threshold (click opens one) and **Open Live**.
- Live view: a small ring and percent on each Hermes session with a known
  window; rows over the threshold get a tone stripe and sort first within their
  state.
- Notifications (`Context window filling up`): once per session per level
  (threshold, 90 %); a session that drops well below re-arms; clicking opens it.
- Sidebar: `ctx 92%` joins the Quota entry while the focused chat is at 90 %+.
- Settings ▸ Status bar & sidebar: the chip switch and the threshold.

### Quota pane

Reachable three ways: the **docked pane** (`Placement: right`, 300px, toggled in
Settings), the **`/quota` route** and the **sidebar nav row** (`Quota`, pulse
icon).

![Quota pane](images/quota-pane.png)

- One card per provider with the official brand icon, a tonal progress bar, the
  plan badge and per-window rows.
- Detail lines where the API offers them: credits, banked resets, extra limits.
- Providers without data collapse into a quiet **"No data (n)"** section — the
  default view shows only providers with live numbers.
- **Pane detail**: `clean` (percentage windows and balance headlines) or `dense`
  (every window, reset time and detail line). Dense is the default.
- Footer: `Checked Jan 15, 2025, 12:34:56 PM UTC · 46s old · poll 60s`,
  localized with the full date, seconds and timezone. Provider cards scroll
  separately so long balance details cannot overlap the footer. With the
  Agent monitor on and a pane under 300px tall, the footer and the monitor
  summary row (the agent/quota/spend pills) are hidden so the card bands keep
  their space; both return when the pane is taller.
- **Card layout**: the pencil icon at the top right of the Quota pane opens
  edit mode. With a mouse or pen, every card can be dragged by its body — the
  dotted handle is only a visual cue — while the pin toggle, up/down arrows and
  account buttons stay ordinary click targets. Touch drags scroll the pane;
  on a touch screen, use the pin toggle and the arrows instead. Drag a card
  onto another to insert it there. Hold a dragged card near the top or bottom
  of its band (or use the mouse wheel) to scroll that band, so cards beyond
  the fold stay reachable; the pinned shelf and the unpinned list scroll
  independently. Pinned cards
  move to an opaque sticky band above the scrolling list — marked by a small
  pin icon in each card's header, next to its plan label, in the normal view —
  and stay visible while you scroll. Each band has its own native scrollbar,
  confined to that band: the pinned bar sits inside the shelf (clear of the
  resize edge), and the unpinned bar starts below the shelf. Both reserve the
  same gutter, so pinned and unpinned cards keep identical widths and edges
  even when only one band overflows. The band paints the pane's own surface,
  so pinned cards keep the list's width and edges instead of looking like a
  separate section.
  Pinned and unpinned cards reorder independently. A pin/unpin cycle leaves
  the unpinned band as it was, whatever order the cards are pinned and
  unpinned in, and a hand move made while cards are pinned gives the same
  final band whichever pin is released first. The arrows remain the keyboard
  path and keep focus at a band edge; a grouped account keeps its selected
  login across edit mode and pin moves. A-Z / Z-A sorts only the unpinned
  band, and the chosen direction is remembered: unpinning a card later places
  it in that sort even if a provider appeared in the meantime. Reordering the
  band by hand returns it to a custom order.
  The pinned band's height is resizable: drag its bottom edge, or focus the
  edge and use ↑/↓ (Shift for larger steps, Home/End for the limits). The edge
  accepts touch, pen and mouse; a 16px hit strip with a short visible grip
  appears on hover or focus without adding a frame, border or gap, so the band
  keeps the list's width and edges.
  Until you choose a height, the band grows with the pins: it follows through
  the bottom of the last pinned card plus the resize strip below it, and stops
  at half the plugin window, whichever comes first — pinning a card never
  requires a manual resize. A deliberate smaller height is remembered (and
  re-clamped) as your preference across pin-set and pane changes, while
  dragging back to the ceiling restores the automatic fit; a click that does
  not actually move the edge leaves the preference untouched.
  Double-click the edge to snap the band to a pinned card's bottom border:
  the midpoint of the card under the edge is the tipping point, so the card's
  upper half snaps to the previous card's bottom and its lower half snaps to
  its own bottom. The snap uses the card positions in the scrolled shelf, and
  the first-card floor and half-window ceiling still clamp it; when the
  ceiling cuts through the last card, the cap wins.
  The lower limit keeps the first pinned card fully visible above the resize
  strip (the strip's overlapping 8px is included), and the upper limit is the
  smaller of the last pinned card's bottom plus that strip and half the plugin
  window (the band's padding is included, so its outer box never exceeds half
  the window). The handle therefore sits below the card border at either limit
  instead of painting over it. It is also never taller than the visible card
  viewport. The chosen height is remembered and re-clamped when the pane,
  window or pin set changes; it does not disappear when a narrower pane
  temporarily forces a smaller value. A mixed pane prefers a 72px unpinned-list
  slice; in a very short pane it keeps a usable pinned slice (about 40px, enough
  for the first card's header) and shrinks the list to its own minimum, splitting
  the remaining height when even those two minima cannot both fit. Neither band
  is allowed to collapse to zero while the other takes a fixed budget. These
  minima apply to the card region itself. With the Agent monitor on, a pane under
  300px tall switches to a compact layout: the summary row and footer are
  hidden and the monitor tabs stay on one horizontally scrollable row, so a
  180px edit pane still leaves both bands a readable, scrollable slice. If even
  that compact chrome leaves less than the two minima, both bands split what
  remains proportionally instead of one taking a fixed budget.
  If one pinned card is taller than half the window (or than a very short pane
  can show together with the list and the strip), that geometry is impossible:
  the cap wins, the resize handle reports itself unavailable, and the card
  scrolls inside the shelf instead of the band taking over the pane. With only
  one pinned card the floor and ceiling coincide, so there is no range and the
  handle reports that the cards already fit. Several pinned cards that all fit
  under the cap still leave a range: at the automatic height the band opens at
  the full fit and can be dragged up to the first card's bottom. "Already fits"
  therefore means a collapsed one-card range, not merely that the pins fit.
  With no pins there is no shelf or handle; with every card pinned the same
  half-window cap applies.
  A wheel gesture anywhere in the pinned band belongs to that band: it
  scrolls the pinned cards while they have room and is consumed at the top
  and bottom instead of chaining into the unpinned list. Move the pointer
  below the band to scroll the list; while the shelf itself scrolls, a small
  chevron above or below the cards points to the ones outside the fold.
- **Sorting and reset**: while editing, a toolbar under the header sorts the
  unpinned cards A–Z or Z–A. Either sort asks first, warning that the custom
  unpinned order will be replaced; cancelling changes nothing. Pinned cards,
  their membership and their order are never touched by a sort. The reset
  button also asks first, then clears every pin and the custom card order, so
  both bands return to the default layout. Both bands remember their order
  across restarts, and unpinning returns a card to its recorded place: between
  the same neighbours when both are still visible in that order, even when the
  card already holds a stored rank that a later move has made stale; at its
  stored rank when they no longer bracket it; at its alphabetical rank when
  the list is currently sorted; and to the older numeric slot for layouts
  saved by a previous build. Cards pinned around a card that then moves keep
  their pin-time order whichever pin is released first. Pinning remembers only
  that place, not a copy of the list order: with no custom order,
  providers discovered while a card is pinned keep their payload place, and a
  pin followed by its unpin leaves Reset inert. This is
  desktop-client-local (stored with the pane's other preferences, not in the
  gateway quota cache), and it does not change the status bar or CLI order.
- A **version-skew** notice when the backend reports a different plugin version.

### Settings

Everything lives in the pane's **Quota Settings** view and persists locally:

| Setting | Values | Default |
| --- | --- | --- |
| Status bar mode | `all providers` · `worst only` | `all providers` |
| Show status bar indicator | on · off (the Hermes status bar must also be visible: ⌘K → Toggle status bar) | on |
| Pane detail | `clean` — rings with their reset times · `dense` — adds forecasts, history and detail lines | `dense` |
| Reset format | `relative` (`2h 15m (14:30)`) · `absolute` (`14:30`) | `relative` |
| Refresh interval | 15–600 s | `600` |
| Enabled providers | toggle any provider on/off (cherry-pick) | all on |
| Show docked quota pane | on · off | on |
| Agent monitor | on · off (widget-local views and polling; backend enrollment is separate) | off |

The **Agent monitor** switch only controls this widget's monitor views and CLI
polling. Turning it on does **not** enroll backend collection; enable the
monitor separately on the gateway for the intended profile:

```bash
hermes config set plugins.entries.quota.settings.monitorEnabled true
# Named profile:
hermes -p <profile> config set plugins.entries.quota.settings.monitorEnabled true
```

Turning the widget switch off stops its polling but leaves the backend setting
unchanged. Set `monitorEnabled` to `false` on the backend profile to stop
collection there.

### Commands

| Command | What it does |
| --- | --- |
| `/quota` | Show the dashboard for every provider with a saved account (refreshes first if stale) |
| `hermes quota` | Same as `hermes quota status` |
| `hermes quota status` | Dashboard of every provider with a saved account |
| `hermes quota status --json` | Raw payload for the desktop widget |
| `hermes quota status --json --cached` | The cache as-is — never refreshes (widget poll path) |
| `hermes quota status --max-age N` | Refresh first when the cache is older than N seconds |
| `hermes quota status --version-json` | Installed build stamp (update self-check) |
| `hermes quota refresh` | Force a re-fetch of all providers |
| `hermes quota provider <name>` | One provider, e.g. `hermes quota provider anthropic` |
| `hermes quota <provider-id>` | Shortcut, e.g. `hermes quota grok` |

### Bundled skill

The plugin registers a read-only `quota:quota-check` skill (via
`register_skill`) when the Hermes runtime supports plugin skills; on older
runtimes the skill is skipped and every other surface is unaffected. Agents
running with the plugin enabled can load it with
`skill_view(name='quota:quota-check')`. It documents cache-first quota
checks and an ask-first protocol: never degrade work to save quota — propose
splitting the task or switching models, and wait for the requester's call.

## How it works

Providers with a CLI or OAuth login (`anthropic`, `openai-codex`, `gemini`, `kimi`)
are read locally. A refresh runs the fetchers and writes
`$HERMES_HOME/quota_cache.json`; the UI only ever reads that file.

The desktop widget polls in two phases, so the configured interval is the real
cadence: every `refreshInterval` seconds it reads the cache with
`hermes quota status --json --cached` (no network, no waiting) and, when that
payload is older than the interval, fires `hermes quota refresh` out-of-band and
redraws when it lands. The sweep itself runs every provider **concurrently**
under a wall-clock budget (`REFRESH_BUDGET_S`, 20s), so one slow or hung endpoint
cannot stretch a poll past its interval — it is recorded as `timeout` and drops
its previous value until a refresh succeeds. The last payload is cached in the widget's storage too, so a
plugin reload or a gateway switch paints immediately instead of waiting on a
backend spawn.

> Quota windows show remaining **%** and **time-to-reset** when the API exposes
> a percentage or denominator. Account balances, such as DeepSeek, are separate
> monetary facts, never converted into a quota percentage.
