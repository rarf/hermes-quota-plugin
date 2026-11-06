# Providers

| Provider | Source | Notes |
| --- | --- | --- |
| `anthropic` | Anthropic OAuth usage API | One bounded read maps session/weekly windows, model-scoped weekly limits such as **Fable week**, and extra usage. Additional Claude logins can be shown side by side — see [Claude accounts](claude-accounts.md) |
| `openai-codex` | local OAuth | Also parses `additional_rate_limits` to surface **per-model Spark limits** (`5.3 Codex Spark · 5h`, `· Weekly`) that stay hidden elsewhere |
| `copilot` | local OAuth | Plan badge + windows |
| `nous` | Nous Portal | Works on free accounts |
| `gemini` | local CLI / OAuth | Detects the tier via `loadCodeAssist` |
| `antigravity` | Reads `gemini:antigravity` from Windows Credential Manager; the `gemini` / `antigravity` item from macOS Keychain; or `~/.gemini/antigravity-cli/antigravity-oauth-token` on Linux | **Opt-in, disabled by default.** Google AI Pro pools — Gemini and Claude/GPT each with a 5-hour and a weekly window; plan from `loadCodeAssist` (`paidTier` wins over `currentTier`). When enabled, refreshes with Antigravity's installed-app OAuth client identity and impersonates its `User-Agent: antigravity/2.8.0 windows/amd64`. Credential stores are read-only. |
| `kimi` | Hermes `kimi-coding` auth (dotenv/pool) or `~/kimi_session.json` | Session (5h), Monthly, and rate windows from `api.kimi.com/coding/v1/usages` |
| `openrouter` | Hermes native API key + saved credential pool | Per-key caps/usage and one explicitly scoped account wallet; see below |
| `deepseek` | Native DeepSeek API key | Account balances in USD/CNY and API-call availability, no fabricated percentage |
| `opencode-go` | API key | See the OpenCode note below |
| `zai` | Z.ai API key | GLM Coding Plan Session, Weekly and web-tools windows |
| `commandcode` | Command Code CLI `~/.commandcode/auth.json`, then Hermes `commandcode` API-key auth | 5h, Weekly, and a known-plan Cycle window |
| `cursor` | `cursor-agent` login (macOS keychain or `auth.json`) | Read-only credential use; Included and API billing-cycle percents; personal on-demand cap as a window, personal/team pools as details. If the session expires, run `cursor-agent login`; the plugin never exchanges refresh tokens or writes to Cursor's credential store. |
| `minimax` | Subscription Key **or** OAuth (`minimax-oauth`) | Token Plan 5h + Weekly windows per model; pay-as-you-go keys show `no-subscription`. Subscription Keys from both MiniMax platforms work: the CN hosts (`minimaxi.com`) are tried after the international ones when a host rejects the key as "invalid api key". The `video` model bucket is **opt-in** (default off — low tiers don't include video, so the entry reports a meaningless 100%). Enable with `hermes config set plugins.entries.quota.settings.minimaxVideoEnabled true` or `HERMES_QUOTA_MINIMAX_VIDEO_ENABLED=1`. |
| `ollama` | `OLLAMA_API_KEY` (`sk-…`, from ollama.com/settings) | Ollama Cloud, from `GET /api/balance` (included allowance + balance, the reset period, purchased credit) and `GET /api/usage?range=30d` (spend, requests, tokens); `POST /api/me` supplies the plan label. The **Included credits** bar is the share drawn from the plan allowance, and the reset is the provider's own `period.until`. **Account balance** is the purchased wallet, which is separate money. See below. |
| `grok` | Hermes `xai-oauth` bearer, then browser cookies | **Opt-in**, disabled by default. Reuses the Hermes xAI OAuth access token for the billing endpoint when present, then falls back to `grok_session.json`, Firefox, and Chromium-family cookies. Windows DPAPI/AES-GCM cookie decryption is supported; the xAI API exposes no plan or account name, so the card shows neither. |

Each fetcher is **fail-open**: a broken provider shows `unavailable (<reason>)`
and never blocks the rest.

### Nous Portal

The CLI and TUI read Nous Portal through the cached account fetcher (the same
Hermes-managed login `hermes portal status` shows). That fetcher imports the
undocumented core module `hermes_cli.nous_account` — a catalog rule 9
private-core warning, disclosed rather than claimed compliant, because Hermes
has no public Nous account-data seam. It keeps two
independent reads: the old local-JWT method (RPM/TPM/RPH and period spend) and
the fresh account snapshot (subscription/credits/entitlement), so a successful
fresh read no longer discards the rate-limit claims. Cached JWT facts are
merged only when both reads prove the same Portal, organisation and user
identity and the same plan (name and/or numeric tier), or a cached Free plan
matched to a fresh Free reading; a missing identity, a free/paid mismatch or a
paid-tier change contributes no cached claims, so one account's or plan's
rate/spend facts can never decorate another's card. A JWT minted for a Free plan
with purchased credits carries `paid_access: true` but no plan name, account
tier or purchased-credit fields; when its numeric tier matches the fresh Free
plan, that unproven flag is not a mismatch and its rate-limit claims still
merge. A paid plan name, paid account tier or paid-subscription evidence still
fails closed. On
Desktop, the Nous card reads billing/usage through Hermes's read-only
`billing.state` gateway method (called with the Desktop SDK's `host.request`) at
the configured interval; that method may contact Nous Portal, and a failure keeps
the cached row. `billing.state` is an app gateway method, not a documented
plugin API or compatibility contract. Billing is authoritative for plan,
status, usage windows and
every financial figure on a credit-grant plan. The cached account row is never
a source of financial data there: `billing.state` exposes no signed-in user id,
so another login's spend, rollover or **Account balance** cannot be proven
to describe this Billing account.

A usage-window plan (Plus/Super/Ultra) is the exception: core reports its
dollar-credit model as unavailable, so Billing carries the plan, access state
and gauges but no computed usage total. Billing's own `balance_display` is
still the account wallet and supplies the one **Account balance** even when
`usage.available` is false; it never depends on the cached account row. A fresh
same-profile account snapshot may also add neutral `Cycle ends` and `Spend this
period`, and its reported total-usable figure outranks Billing's wallet when its
exact plan label and access state match Billing's and its `account_org_key`
matches the local digest of Billing's `org_slug`; without a fresh matching
snapshot, Billing's wallet remains the balance. That cached total is display
data, not verified money: it is a point-in-time Portal figure, and while a plan
is paused it still counts the included credits the pause withholds. A paused
plan therefore never takes the cached total and keeps Billing's own wallet (or,
in the CLI row, the purchased wallet alone); the organisation digest is a
same-organisation hint that cannot distinguish another signed-in user, so the
cached total is not account proof. `Cycle ends` is used because
Billing exposes no cancellation state: a cancelled-at-period-end plan must not
be labeled as renewing.
Subscription and top-up component amounts are folded into that one balance and
never rendered as separate lines. If the total field is absent, the purchased
wallet may be used only when the snapshot explicitly reports zero included
subscription credits; an unknown component is never summed into a fabricated
total. The card also never claims that wallet automatically pays included
subscription usage (that needs `keep_going_on_credits` / `credits_pay`).
Billing's plan, access state, usage windows and wallet balance stay
authoritative; a stale, missing or mismatched cached snapshot contributes no
cycle-end, spend, rate or component fact. That match is only a
same-organisation hint, not account proof: Billing exposes no signed-in user
id, so another user in the same organisation can match. A cached cycle-end or
spend fact therefore carries the same residual limitation as the rate line;
Billing's own plan, wallet and windows do not depend on that match.

Two non-financial account facts may accompany a Billing row:

- **Rate limits.** A cached `Rate limits: …` line is shown only when the cache
  is fresh, the cached row and Billing carry the same exact plan label and the
  same access state (no free/paid or active/unknown crossing), and both name
  the same Portal organisation. Billing's `org_slug` is reduced locally to a
  pseudonymous digest (unkeyed FNV-1a, not a secret or cryptographic
  identifier) and compared with the cache's `account_org_key`; the raw slug is
  not rendered or stored in the query cache. A numeric tier or generic "paid"
  flag is never normalized into a plan. Same organisation + same plan + same
  state is still only a same-organisation hint, not account proof:
  `billing.state` exposes no signed-in user id, so another user in the same
  organisation can match. The line is therefore disclosed as cached account
  data with that residual limitation; any missing or disagreeing fact fails
  closed.
- **Free Tool pool.** It is shown only when Billing and a fresh same-profile
  cached snapshot both describe a Free plan, the account fetcher's live
  `tool_access.enabled` switch is true (the fetch emits the fact only in that
  case), and the cached `account_org_key` matches the local digest of Billing's
  `org_slug`. A paid Billing row, a disabled pool, an unavailable/unknown
  account read, a missing or mismatched organisation, and a cache older than
  the tool-pool freshness bound show no pool line. The categories are
  entitlement labels, not account or payment data.

When no live account rate claim is mergeable, an unambiguous Billing plan gets
a documented **published** fallback from the official
[Nous API docs](https://portal.nousresearch.com/api-docs) (retrieved
2026-11-06):

| Plan | RPM | TPM |
| --- | ---: | ---: |
| Free | 50 | 500,000 |
| Basic | 400 | 2,000,000 |
| Plus | 400 | 4,000,000 |
| Super | 800 | 8,000,000 |
| Scale | 600 | 6,000,000 |
| Ultra | 1,600 | 16,000,000 |
| Default paid users | 180 | 720,000 |

The fallback is rendered as `Rate ceiling: … (published)` and only for an exact
documented plan label. The account API's product-prefixed labels map to the
same bare tier (`Hermes Plus` → Plus; 400 RPM / 4M TPM), while an unknown
suffix or numeric tier still gets no fallback. A Free reading never inherits a
paid plan's ceiling, and a paid reading never inherits Free's. The docs publish
RPM and TPM only, so a fallback never invents an RPH figure; a live account
claim always wins and keeps its RPH value. Unknown labels and numeric tiers
show no fallback. A low-balance paid
plan keeps its exact label while its access state becomes `unknown`, so its
published ceiling is still shown. The same table is
used by the CLI/TUI account card.

Free accounts with no purchased credit show `Free` plus free-tier facts only:
component zero credit lines and a free plan's cycle-end date are not rendered.
When the account snapshot supplies its computed `total_usable_credits`, that
exact amount is shown as one prominent **Account balance** (zero is a known
amount; an absent field stays unknown and shows no balance). If that total is
absent while the snapshot explicitly reports zero included subscription
credits, the purchased wallet is the whole usable balance and may be shown as
the Account balance; an unknown subscription component is never summed.
Purchased credits on a Free plan can make `paid_service_access` true
while the subscription itself has no charge or included credits; that is still
a Free plan, not a paid subscription, so the plan stays `Free`, the wallet is
available for calls, and no cycle-end date is rendered. When that purchased
wallet is positive, the card says `Free tier - paid models via purchased
credits` and shows it as the one **Account balance**, so the status
line no longer denies a funded wallet; separate component lines are hidden.
A missing top-up amount stays unknown, and the plain free-only copy remains
for a Free account with no purchased credit. A funded total whose component
the snapshot does not name is described neutrally as `Free tier - credits
available`, neither denied as free-only nor attributed to a purchase. The free
**Tool pool** line is shown only when the account snapshot's live
`tool_access.enabled` switch is true; coverage entries alone do not advertise
pool access. When the fresh account-API snapshot omits `tool_access` entirely,
a cached JWT snapshot that proves the same Portal account and the same
free/paid reading may supply its enabled switch. An explicit fresh
`enabled: false` always wins, and a cached pool is never used across accounts
or from a paid snapshot. A real paid subscription (a positive charge, included
credits, or an active paid subscription) outranks a stale free tier claim and
keeps its supplied cycle-end date.

An `unavailable` account (missing account or lapsed subscription) shows only
the status line: stale subscription windows, credit balances, rollover,
cycle-end dates, spend and rate limits are suppressed rather than presented as
current facts.

A paused plan (`plan_usage.paused_reason`) is a warning, not a red unusable
verdict: the included allowance is paused, while Billing's own `credits_pay`
answer covers the separate purchased-credit pool. `true` says calls continue on
purchased credits and renders as a warning, `false` renders as unavailable, and
an absent answer stays unknown rather than asserting a stop. A `true` answer is
void when Billing reports the account wallet as explicitly `$0.00`: with no
purchased credit to spend, calls are unavailable and the card does not claim
purchased credits still apply. An absent wallet amount leaves the Billing
answer standing. Paused plan windows are never shown or ranked as remaining
quota.

The CLI/TUI account snapshot exposes the same pause as
`usage_window.paused_reason`; it classifies the row `paused` and suppresses its
windows. Its own `usage_window.keep_going_on_credits` answer governs call
availability: `true` reports available only when a positive purchased balance
is actually proven, `false` reports unavailable, and an absent answer stays
unknown. The same rule applies to an active usage-window plan whose all-scope
gauge is exhausted: with `keep_going_on_credits=false` calls are unavailable,
with `true` availability follows the proven purchased balance, and otherwise it
stays unknown. Only an account confirmed depleted reports calls unavailable
without that evidence; an unknown snapshot never claims yes.

### Ollama

Ollama Cloud quota is read from three endpoints, all documented since
[ollama/ollama#18829](https://github.com/ollama/ollama/pull/18829) merged:

- `GET /api/balance` — the plan allowance (`included.allowance_usd`), what is
  left of it (`included.balance_usd`), the reset (`included.period.until`) and
  purchased credit (`purchased.balance_usd`).
- `GET /api/usage?range=30d` — spend, request count and token counts across a
  `24h` / `7d` / `30d` window (`totals` plus daily or hourly `buckets`).
- `POST /api/me` — the plan label. `GET` is a 405, so this stays a POST.

Set `OLLAMA_API_KEY` to the `sk-…` key from
[ollama.com/settings](https://ollama.com/settings).

**Two pools, and they are separate money.** `ollama.com/settings` shows
"Free usage credits" as a percentage of a small monthly allowance, and "Usage
credits / Current balance" for purchased credit underneath it. `/api/balance`
reports both, so the card does too:

- **Included credits** — the window, the percentage
  (`(allowance − balance) / allowance`) and the reset, all from one response.
  Both sides of the division are real dollars, so the percent is arithmetic
  rather than inference.
- **Account balance** — the purchased wallet, which is the figure the dashboard
  calls "Current balance". It does not refill at the reset.

**Total spend is neither of those.** `totals.usage_usd` covers both pools, so
it is not the percentage's base — on the account this was captured from it read
`$0.06348` against `$0.06107` drawn from the allowance, the difference being the
two paid-model requests billed to purchased credit. The card labels the line
"Total spend" for that reason; the two figures are expected to differ, and
**Total spend is not the percentage's denominator.**

**Money is rounded to cents for display.** The endpoint's own precision
(`balance_usd: 2.43893`) is per-token rounding, not resolution; a nonzero
sub-cent balance keeps extra digits so it cannot read as `$0.00`.

**Legacy pre-credits plans** return `included.session` / `included.weekly` with
Ollama's own `remaining_percent` and `resets_at` instead of dollar fields. Valid
0–100 remaining percentages are converted to used percentages. Invalid
percentages and resets are omitted independently; a window with neither is
not data. A missing profile plan remains unknown, never inferred from the pool.

Both endpoints allow 10 requests per minute per user, shared across API keys and
devices, and answer a breach with `429` and a `Retry-After` header — reported as
`http-429`. The card makes three calls per refresh, well inside that.

Related upstream discussions:

- [ollama/ollama#12532 — Cloud usage stats](https://github.com/ollama/ollama/issues/12532)
- [ollama/ollama#18653 — Cloud API: expose credit balance & true spend](https://github.com/ollama/ollama/issues/18653)



### Antigravity

Antigravity quota collection is disabled unless you opt in:

```sh
hermes config set plugins.entries.quota.settings.antigravityEnabled true
```

Alternatively, set `HERMES_QUOTA_ANTIGRAVITY_ENABLED=1`. If enabled, the plugin
reads Antigravity's token from Windows Credential Manager (`gemini:antigravity`),
macOS Keychain (service `gemini`, account `antigravity`; `agy`'s `go-keyring-base64:`
wrapper is unwrapped before parse), or Linux's
`~/.gemini/antigravity-cli/antigravity-oauth-token` file. It refreshes the token
with Antigravity's installed-app OAuth client identity, then calls Google's
quota service using the spoofed `User-Agent: antigravity/2.8.0 windows/amd64`.
Credential stores are read-only; refreshed tokens stay in memory.

### DeepSeek

Reads the documented [`GET /user/balance`](https://api-docs.deepseek.com/api/get-user-balance)
endpoint at `https://api.deepseek.com`. Credentials come from Hermes' native
`resolve_api_key_provider_credentials("deepseek")` helper, including its dotenv
and `key_env` precedence. Configure `DEEPSEEK_API_KEY` through Hermes as usual.
The plugin does not enumerate, rotate, or write credential pools.

This is the direct DeepSeek account balance, not usage of DeepSeek models through
OpenRouter. Each returned USD/CNY balance stays separate, with total, granted and
topped-up amounts preserved as decimal strings in the cache. No account balances
are added together, no per-key spending limit is inferred, and no plan or quota
percentage is invented. Zero balances and `is_available: false` are valid data.

The pane shows a balance headline and API-call availability in both clean and
dense modes. Dense mode and `/quota deepseek` also show the detailed breakdown.
The status-bar chip displays money instead of an empty percentage; worst-only
mode uses balance chips only when no percentage windows are available.
Missing credentials and failed requests remain explicit unavailable states.
Requests have a timeout, bounded response size, and no redirects; error details
and credentials are never written into the display cache.
### OpenRouter

Reads `GET https://openrouter.ai/api/v1/key` separately for each distinct locally
configured credential, using Hermes' native OpenRouter resolution plus its saved
credential pool. Duplicate secrets are queried once even if their saved labels
differ. Native resolution is pinned to the canonical OpenRouter endpoint to
avoid selecting, rotating or seeding the runtime pool. Environment-backed saved
rows are resolved through Hermes' profile-aware credential lookup. This is not
an inventory of all keys on an OpenRouter account: the management `/keys`
endpoint is never queried, and no management key is required.

Each key has a generated label such as `Key 1 (native)` or `Key 2 (saved)`.
Saved/vendor labels, key suffixes, hashes, account IDs and emails are never
included in display data. The ordinal follows discovery order, not a stable
account identity or the runtime pool's current selection.

- Key-specific spending cap, remaining allowance and daily/weekly/monthly usage
  are shown when supplied by the API. A percentage requires a positive cap and
  remaining allowance within that cap. An uncapped key does not mean unlimited
  account credit; an exhausted key cap does not mean an empty account wallet.
- `/credits` is queried **once**, through the native credential (or the first
  resolved saved credential if native resolution is unavailable). The displayed
  wallet belongs to that credential's account. Other keys' account membership is
  **unverified**: wallets are never summed, duplicated or inferred from equal
  balances. The wallet is not a total across accounts.
- Key and wallet requests run independently under one 10-second deadline. One
  failed or timed-out key does not hide successful keys or the wallet. If every
  request fails, the provider is unavailable rather than reporting false data.
- The Desktop pane retains these details in **both clean and dense modes**, so
  wallet scope, uncapped keys and per-key failures remain visible. They are also
  present in `hermes quota status --json`.

### CommandCode

CommandCode prefers the API key from `~/.commandcode/auth.json`. When that
file has no usable key, it uses Hermes' `commandcode` API-key resolver (including
Hermes dotenv and credential-pool sources). It follows the billing calls used
by the official Command Code CLI 1.53.0. It first requests
`/alpha/whoami?limits=1`, then scopes billing and usage calls with the returned
organization ID; `currentPeriodStart` is passed as the usage-summary `since`
value when the subscription response provides it. These alpha routes are not a
public API, so the fetcher fails open on unknown response shapes and shows only
known plan labels/denominators. It accepts both observed locations for
`windowLimits` (`credits.windowLimits` and the older top-level form), shows
rolling **5h** and **Weekly** windows, and shows a **Cycle** percentage only for
a known plan. Unknown plans retain a balance-only detail instead of an invented
label or percentage. The provider's request group has its own 10-second
wall-clock deadline, below the cache sweep budget.

### OpenAI Codex saved accounts

The cache keeps saved accounts nested under `providers["openai-codex"].accounts`; the pane and CLI render each separately, and the status bar selects one representative account rather than summing percentages. This is display-only and does not change inference routing. Polling is read-only: it does not refresh tokens, rotate credentials, modify auth files or redeem resets. Expired tokens require normal Hermes sign-in. Unknown or stale account data is not treated as exhaustion. See the Codex sections in the upstream history for provider-specific handling; labels are local display text, so choose screenshot-safe names.

Account names: a saved alias is shown as-is. A generic login-method label such as `device_code` is replaced by the account email stored in the token's OpenAI profile claim. That email is decoded locally from the token already in your `auth.json`; it is not sent anywhere else, but it will appear on screen, so use an alias if you capture screenshots. A row with no label at all shows as `Account N`.

Plan names follow OpenAI's public tiers: the API value `prolite` is shown as **Pro 100** and `pro` as **Pro 200**. A **Pro 500** tier is not mapped yet because its raw API value has not been confirmed; unknown values are shown title-cased.

### OpenCode (Go)

Reads `GET https://opencode.ai/zen/go/v1/usage`. The key is resolved from
`OPENCODE_API_KEY`, then `OPENCODE_GO_API_KEY` (the name Hermes' own `opencode-go`
chat provider uses in `~/.hermes/.env`), then OpenCode's CLI auth file
`~/.local/share/opencode/auth.json`. That endpoint intermittently answers
`503 Go usage is unavailable` for a large share of calls regardless of credential
or User-Agent, so the fetcher retries before reporting the provider unavailable.

With one key, the windows are labelled plainly (`5-hour`, `Weekly`, `Monthly`). With several keys, each window carries its slot as `key N` so the rows stay distinguishable; environment variables are not treated as account identities.

### Grok (opt-in)

Grok is **disabled by default**. When enabled, the provider first reuses the
Hermes `xai-oauth` access token as a Bearer token against the Grok billing
endpoint. The token is read-only and is never written or refreshed by this
plugin. If Hermes xAI OAuth is unavailable, or the billing request rejects the
Bearer credential with an authentication error, the provider keeps the
existing cookie fallback chain:

```bash
hermes config set plugins.entries.quota.settings.grokEnabled true
hermes quota refresh
```

Credential sources, in order:

1. Hermes `xai-oauth` Bearer token
2. `~/grok_session.json` if you created one yourself
3. Firefox `grok.com` cookies
4. Chromium / Google Chrome / Brave `grok.com` cookies

Browser notes:

- Sign into <https://grok.com> in the browser at least once when using the
  cookie fallback.
- Windows reads Chromium's DPAPI-protected Local State AES key and decrypts
  `v10`/`v11` cookies with AES-256-GCM. App-Bound `v20` cookies are reported as
  unsupported (`chrome-app-bound`), never bypassed.
- macOS reads the `Chrome Safe Storage` Keychain item (prompted on first
  refresh) and derives the AES key with 1003 PBKDF2 rounds.
- Linux reads the login keyring with
  `secret-tool lookup application chromium` (or `chrome` / `brave`, matching the
  profile that owns the cookie DB) and derives with a single PBKDF2 round —
  profiles created with `--password-store=basic` fall back to the well-known
  `peanuts` password.
- If refresh returns `chrome-tcc-denied`, grant Full Disk Access to
  **Hermes.app** (Desktop) and/or the Terminal you use for `hermes quota refresh`,
  then retry.
- Chromium 127+ cookies prefix a SHA256(`host_key`) digest before the value; that
  prefix is stripped after decryption.
- Safari is not supported.
