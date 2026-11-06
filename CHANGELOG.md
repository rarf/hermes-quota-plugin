# Changelog

Newest first. Each entry matches an annotated `vX.Y.Z` tag; the tag message is
the release note.

## 3.1.0 — 2026-11-06

- Desktop Quota: every saved account and every quota window is visible at
  once, as compact rows with bars, in the pane and in the status-bar popover.
  The one-account-at-a-time account tabs are gone. Unknown windows show "—",
  never 0%. Failed saved accounts keep their slot with the reason.
- Status bar: grouped chips name the limiting account, its window and the
  remaining value, plus an explicit `+N` for the other accounts; `all` and
  `worst` modes are kept and Codex stale values stay struck through.
- Accounts: each saved account lists all of its windows as bars.
- Live, Health, Usage, Kanban and Settings: flat KPI strips, quiet section
  titles, status words instead of pills, and secondary detail (charts,
  per-day values, settings groups) behind native disclosures. Every
  preference and notification kind is unchanged. Eight new strings are
  translated in all ten locales.
- Tests: layout regressions for the quota and monitor surfaces and a
  real-module render matrix (widths, themes and states).
- Credentials: unchanged. Provider readers stay opt-in and disclosed as in
  3.0.x.

## 3.0.1 — 2026-11-03

- Desktop: keep the last successfully fetched quota data visible when polling
  fails (#87, thanks @iauramchyk).
- Desktop: show each quota ring's reset time in clean pane mode (#88).
- CLI, Desktop and TUI: hide expired scheduled Codex special-reset notices at
  render time without deleting history or changing quota/credit values. Notices
  without a scheduled time remain visible (#97).
- Kimi: send an explicit plugin User-Agent for quota requests; offline regression
  coverage includes the default, .ai and legacy POST endpoints (#92, #98).
- CI: include special-reset JavaScript regressions and retain Python 3.9 coverage
  on Ubuntu 24.04, where the required runtime is available.
- Catalog limitation: existing provider credential readers and OAuth handling do
  not meet current Hermes catalog rule 11. This patch does not migrate them or
  establish eligibility for a new catalog pin. Antigravity PR #95 is not included.

## 3.0.0 — 2026-10-23

- Agent Monitor inside Desktop: live Hermes sessions, attention and context
  indicators, model health, recorded usage estimates, quota history and
  burn-rate forecasts, without a separate dashboard server (#83).
- Localized monitor UI, status-bar chips, context guard and configurable
  notifications. Existing quota functionality remains available without
  enrolling in monitor collection.
- Collection is opt-in per active profile. Preserve the hardened trial scope:
  no sibling-profile/external CLI/shared Kanban collection, new credential-pool
  metadata reads, account/budget mutations or monitor agent tools.
- Usage is labeled as estimated API-price equivalents, not billed charges;
  month-end spend projection is unavailable. Private monitor database and
  symlink/sidecar protections retained.
- Preserve native Hermes plugin updates and the per-card Codex reset notice.
- README and monitor documentation now describe the shipped scope and limits.
- A huge thank-you to @Asimatasert for the Agent Monitor contribution,
  localization, tests and follow-up fixes that made this milestone possible.

## 2.12.0 — 2026-10-23

- Desktop: one card per saved credential for multi-account providers, grouped
  by provider in the status bar; the pane keeps a card per account and the
  Codex announcement once above the group (#71).
- Skill: bundle `quota-check` (`quota:quota-check`) so agents check quota
  before long work and ask before splitting tasks or switching models (#69).

## 2.11.5 — 2026-10-23

- Codex: a pooled account's saved upstream base URL is preserved for usage
  requests, so a local inference proxy no longer turns a valid account into
  `http-404` (#81).
- Grok: reuse the Hermes `xai-oauth` bearer before the browser-cookie chain;
  Windows Chrome v20 App-Bound cookie decryption is supported (#82).

## 2.11.4 — 2026-10-23

- Desktop: a legacy single connection reports an owner without a registry
  `connectionId`; the widget now routes through the live gateway instead of
  failing with "focused quota profile unavailable". A `null` owner still keeps
  the query disabled, so an ambiguous multi-connection never guesses a profile
  (#79).

## 2.11.3 — 2026-10-23

- Cache: a carried record (incomplete sweep) now drops its windows and balances,
  so the CLI cannot render stale percentages under a fresh cache timestamp; the
  note is deduplicated instead of growing on every sweep.
- TUI: `rememberable` tolerates a non-array selection, and a test pins the
  persisted preference as the single string the picker resolves.

## 2.11.2 — 2026-10-23

- Cache: a sweep whose provider registry is incomplete no longer deletes the
  providers it never tried — they are carried forward and marked
  `not-refreshed`.
- Tests: the shared-cache refresh test patches its own cache path, so a test
  run can no longer rewrite the live `quota_cache.json` with a synthetic
  single-provider cache.
- Desktop: `WIDGET_VERSION` synced with the manifest (2.11.1 shipped a skew).

## 2.11.1 — 2026-10-22

- TUI: `/quota-select` opens on the provider/account saved by the previous run
  and marks that row `· current`; Enter keeps it instead of silently switching
  to the first cached row.
- TUI: a stale panel can no longer overwrite the saved preference with a
  selector the cache does not resolve.
- Tests: the picker test uses a temporary `HERMES_HOME`, so a test run never
  rewrites the live `quota-panel.state.json`.

## 2.11.0 — 2026-10-22

- `/quota` dashboard with friendly names, remaining-quota bars and worst-first
  ordering.
- Codex special-reset announcement on CLI, TUI and Desktop, with its public
  source and credit.
- Optional TUI quota chip and a saved dock preference that survives a restart.
