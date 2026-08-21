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
  percent from a bare balance. No denominator → put the number in `details`
  instead ("$X left").
- **Free tiers get honest cards.** If the API exposes nothing numeric for the
  tier, return a card with `plan="Free"` and `details` describing what IS true
  (published limits, tool pool). See `_fetch_nous_portal()` in `builtin.py`
  for the reference implementation.
- **No secrets in output or logs.** Fetchers receive credentials, the cache
  stores display data only. Never log headers/cookies/tokens; never put
  account IDs or emails into `details`.
- **Timeout every request** (`timeout=15` max) and prefer `urllib.request`
  (stdlib) unless the repo already depends on an HTTP client.

Sensitive-source rule: anything that reads **browser cookies** or other
user-session material must be **opt-in** (config flag checked at fetch time,
like `grok.py`'s `grokEnabled`) and default to `opt-in-disabled`.

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

Use `unittest.mock.patch` on the module's `urlopen`/loader functions — tests
must pass offline. Then verify live once:

```bash
python -c "from quota_providers.<provider> import fetch_<provider>_quota; \
           r = fetch_<provider>_quota(); \
           print(r.plan, [(w.label, w.used_percent) for w in r.windows], r.unavailable_reason)"
hermes quota refresh && cat "$LOCALAPPDATA/hermes/quota_cache.json"
```

The provider should appear in `/quota` and in the Desktop pane after
**Reload desktop plugins** (⌘K). Remember: widget-only changes reload; any new
Python backend file needs a full Desktop restart.

## Checklist

- [ ] Fetcher never raises; every failure returns `build_unavailable(...)`
- [ ] Percentages derived from a real denominator only
- [ ] Plan omitted when unknown; free tier shows an honest card
- [ ] Cookie/session readers are opt-in and default-off
- [ ] No secrets/IDs/emails in results, logs, or debug artifacts
- [ ] Registered in `__init__.py`; `PROVIDER_META` entry added
- [ ] Unit tests added and passing offline
- [ ] `plugin.yaml` version bumped; live refresh verified
