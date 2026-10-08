# Adding a new provider

This guide is for anyone adding a provider (e.g. "minimax", "zai", "deepseek")
to the quota plugin. Read it fully before writing code — it saves one review
round-trip.

## Architecture in one paragraph

Every provider lives behind a **fetcher**: a zero-argument function registered
in `quota_providers/registry.py` that returns a `QuotaResult` and **never
raises**. `quota_cache.py` iterates the registry, calls every fetcher, and
writes `$HERMES_HOME/quota_cache.json`. Both consumers (the `/quota` command
and the Desktop widget) read **only** that JSON file — no provider code ever
runs inside a chat turn or the widget process. Adding a provider therefore
touches exactly three places: one fetcher, one registration, one widget meta
entry.

## Step by step

### 1. Find a data source *before* writing code

Check, in this order:

1. **Hermes core already ships a snapshot** — `agent/account_usage.py`
   dispatches some providers through `fetch_account_usage("<id>")`. If yours is
   there, you only need the 3-line generic adapter in `builtin.py`.
2. **An authenticated CLI config / session file** — e.g. `~/.gemini/oauth_creds.json`,
   `~/kimi_session.json`, Hermes' own provider auth store
   (`hermes_cli.auth.get_provider_auth_state`). Prefer this over browser
   cookies; mark cookie readers opt-in like `grok.py` does.
3. **A documented REST endpoint** reachable with those credentials. Probe it
   live with a throwaway script first and record the exact response shape in
   the fetcher docstring.

If none exists, stop: report it upstream instead of scraping HTML pages.
Scrapers break silently and leak sessions.

### 2. Write the fetcher

Create `quota_providers/<provider>.py`:

```python
"""<Provider> (<plan name>) quota fetcher — plugin standalone copy."""

from __future__ import annotations

from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable


def fetch_<provider>_quota() -> QuotaResult:
    creds = _load_creds()
    if not creds:
        return build_unavailable("<provider>", "no-credentials")
    ...
    return QuotaResult(
        label="<provider>",
        windows=[QuotaWindow(label="Monthly", used_percent=used, reset_at=iso)],
        plan="Plus",            # or None when unknown — NEVER invent one
        details=["Credits balance: $12.50"],
    )


from .registry import register as _register  # noqa: E402

_register("<provider>")(fetch_<provider>_quota)
```

Contract rules (all enforced in review):

- **Fail-open, never raise.** Wrap the whole body; every failure path returns
  `build_unavailable("<provider>", "<machine-readable-reason>")`. Reasons use
  kebab-case: `no-credentials`, `auth-failed`, `http-429`, `parse-pending`.
- **`unavailable_reason` must be truthful.** `no-data` (asked, got nothing),
  `no-credentials` (nothing to auth with), `opt-in-disabled` (user turned it
  off) mean different things to users staring at the muted card.
- **Percentages need denominators.** Emit `used_percent` only from
  used/limit, remaining/limit, or a server-reported percent. Never fabricate a
  percent from a bare balance. No denominator: use `account_balances` for
  structured monetary facts and `details` for the CLI breakdown ("$X left").
  `AccountBalance` holds `currency`, `total_balance`, and optional
  `granted_balance` / `topped_up_balance` as decimal strings. A provider may set
  `api_calls_available` to a reported boolean; `None` means unknown. These fields
  survive cache serialization and render without percentage bars.
- **Free tiers get honest cards.** If the API exposes nothing numeric for the
  tier, return a card with `plan="Free"` and `details` describing what IS true
  (published limits, tool pool). See `_fetch_nous_portal()` in `builtin.py`
  for the reference implementation.
- **No secrets in output or logs.** Fetchers receive credentials, the cache
  stores display data only. Never log headers/cookies/tokens; never put
  account IDs or emails into `details`.
- **Use the no-redirect seam for authenticated requests.** Import
  `urlopen_no_redirect` from `.base` and use it for any request carrying a
  bearer token, cookie, or other credential. Bare `urllib.request.urlopen`
  follows redirects by default and can replay sensitive headers to the target.
- **Set a timeout on every request** (`timeout=15` max) and share a monotonic
  budget across retries or serial calls. `urllib`'s timeout limits socket
  inactivity, not total response wall time: a server that keeps trickling bytes
  can keep a read active beyond the budget. The quota-cache sweep records an
  unfinished provider as `timeout` after its wait budget, but it does not cancel
  the daemon worker or its blocked request. Do not describe this as a hard
  wall-clock request deadline unless the transport actually enforces one.
  Prefer `urllib.request` (stdlib) unless the repo already depends on an HTTP
  client.

Sensitive-source rule: anything that reads **browser cookies** or other
user-session material must be **opt-in** (config flag checked at fetch time,
like `grok.py`'s `grokEnabled`) and default to `opt-in-disabled`.

### Showing several accounts for one provider

A provider with more than one login (as `anthropic` now does for Claude
subscriptions) attaches `QuotaAccount` entries to its `QuotaResult` instead of
inventing new provider ids:

```python
from .base import QuotaAccount, QuotaResult

return QuotaResult(
    label="anthropic",
    windows=[...],
    accounts=[
        QuotaAccount(id="work", label="Work", windows=[...]),
        QuotaAccount(id="dead", label="Dead", unavailable_reason="no-credentials"),
    ],
)
```

The cache flattens each account into its own `<provider>:<account_id>` row, so
the footer, `/quota` and the widget render accounts with no account-specific
code: the widget resolves the base provider for the icon and shows
`<Provider> · <label>`. Keep it honest: an account `id` is stable and unique; an
account is never merged into another by organization or by an equal quota; a
token or a filesystem path must never reach the record; only a byte-identical
credential — including the primary's — may collapse to one row, and identity is
**never** inferred from a directory; a generated row (one with no usable id)
must never hide a configured one — give it the reserved
`GENERATED_ACCOUNT_ID_PREFIX` namespace and let the cache disambiguate any
collision deterministically; and an unreadable account is a truthful
`unavailable (<reason>)` row, never a hidden one. Bound the work: cap how many
entries you parse and read, emit a single overflow diagnostic past the cap, read
local credential files through a stat-verified regular-file descriptor with a
byte cap, and bound HTTP response reads.

### 3. Register + surface it

1. Import your module for its side effect in `quota_providers/__init__.py`.
2. Add display metadata in `desktop/plugin.js`:

   ```js
   const PROVIDER_META = {
       // ...
       minimax: { name: "MiniMax", mono: "M" },
   };
   ```

3. Bump `plugin.yaml` `version` (semver: new provider = minor).

### 4. Test it

Add cases to `tests/test_fetchers.py` (stdlib `unittest`, no network):

- missing credentials → `no-credentials`;
- happy path with a canned payload → exact windows/percents asserted;
- free-tier payload → honest card;
- garbage response → `bad-json`/`parse-pending`, never a crash.

Use `unittest.mock.patch` on the module's `urlopen_no_redirect`/loader functions —
tests must pass without an external provider. Loopback servers are suitable for
redirect and timeout behavior. Then verify live once:

```bash
python -c "from quota_providers.<provider> import fetch_<provider>_quota; \
           r = fetch_<provider>_quota(); \
           print(r.plan, [(w.label, w.used_percent) for w in r.windows], r.unavailable_reason)"
hermes quota refresh && cat "$LOCALAPPDATA/hermes/quota_cache.json"
```

The provider should appear in `/quota` and in the Desktop pane after
**Reload desktop plugins** (⌘K). Remember: widget-only changes reload; any new
Python backend file needs a full Desktop restart.

## Run the plugin scanner

Hermes scans every plugin it installs, with
[`tools/plugin_guard.py`](https://github.com/NousResearch/hermes-agent) from
hermes-agent. **A critical finding there does not fail an ordinary unit test —
it stops people installing or updating this plugin**, and the `plugin-scanner`
CI job checks for it on every PR. Running it locally is the same check without
the round trip:

```bash
python3 scripts/scan_plugin.py
```

The scanner is fetched from a **pinned Hermes release** (`SCANNER_REF` in
`scripts/scan_plugin.py`), not from `main`. Pinning is the difference between a
gate and a decoration: on 2026-10-04 `main` demoted the Google installed-app
client literals to `high` (`is_google_installed_app_secret`) while every stable
build still called them `critical`, so a `main`-tracking job went green on a tree
that stable Hermes refuses to install — which is exactly how the two literals
shipped through several releases unnoticed (issue #51). Bump `SCANNER_REF` in its
own commit when a release is targeted.

The gate fails on a `critical` finding in any category, or on a `high` finding in
`credential_exposure`, because a scanner-side demotion only moves the severity —
the class still blocks community installs. `--self-test` plants a secret-shaped
literal in a temp tree and requires the scanner to report it as blocking; CI runs
it before every scan, so a scanner bump that stops seeing the class fails loudly
instead of passing quietly.

Matched values are redacted in the output. A finding's file and line are enough
to locate it; copying credential-shaped strings into logs is worth avoiding.

If a finding is legitimate — a public OAuth client id, a fixture holding a
hostile string — do not add a suppression. The scanner already steps findings
down inside test trees, and a blanket ignore teaches the next author the same
thing. Restructure the code, or argue it upstream in
[hermes-agent](https://github.com/NousResearch/hermes-agent).

Because the scanner is pinned, a PR cannot go red because upstream changed its
mind; it goes red because this tree changed, or because a deliberate
`SCANNER_REF` bump brought a stricter scanner.

## Checklist

- [ ] Fetcher never raises; every failure returns `build_unavailable(...)`
- [ ] Percentages derived from a real denominator only
- [ ] Plan omitted when unknown; free tier shows an honest card
- [ ] Cookie/session readers are opt-in and default-off
- [ ] No secrets/IDs/emails in results, logs, or debug artifacts
- [ ] Registered in `__init__.py`; `PROVIDER_META` entry added
- [ ] Unit tests added and passing offline
- [ ] `plugin.yaml` version bumped; live refresh verified
- [ ] **Plugin scanner clean** — no `critical`, no `high` in `credential_exposure`; see [Run the plugin scanner](#run-the-plugin-scanner)
