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
| `grok` | browser cookies | **Opt-in**, disabled by default. The xAI API exposes no plan or account name, and the browser session cannot verify a subscription, so the card shows no plan and claims no billing mode (`unknown`). Two distinct meters are reported with explicit source/scope — see [Grok](#grok-opt-in) below. |

Each fetcher is **fail-open**: a broken provider shows `unavailable (<reason>)`
and never blocks the rest.

### Usage provenance

Every record carries a normalized `provenance` object alongside its windows,
serialized into `quota_cache.json` and rendered by `hermes quota` and the
Desktop pane:

```json
"provenance": {
  "source": "api.anthropic.com/api/oauth/usage",
  "scope": "Claude OAuth subscription (this login only)",
  "account": null,
  "plan_source": "provider-response",
  "billing_mode": "subscription",
  "meter": "subscription allowance",
  "note": null
}
```

- `plan_source` is `provider-response` (a vendor field), `local-credential`
  (e.g. Claude's `.credentials.json` `subscriptionType`), `user-label` (a
  configured display label — never a verified plan) or `unknown`. A plan is
  never inferred from an email address or a `.edu` domain. A configured extra
  Claude account takes its plan from its own credential file. The **primary**
  Claude login's plan is read from the local credential **only when its
  `accessToken` is byte-identical to the resolved primary credential**: the
  `.credentials.json` file is preferred, and on macOS the
  `Claude Code-credentials` Keychain item (read-only, bounded, via `security`)
  is consulted when the file yields no plan — that is where a macOS
  enterprise/EDU login normally lives. Any other source, or a mismatch, leaves
  the plan `unknown` rather than attaching one login's plan to another credential.
- `billing_mode` separates a `subscription` allowance from `pay-as-you-go`
  money; a record that provably shows both (Anthropic subscription windows plus
  enabled `extra_usage`; Nous subscription credits plus a **positive**
  `purchased_credits_remaining`) is `mixed`. A Codex `credits` balance does **not**
  count: its presence is not proof of purchased/pay-as-you-go money (it may be an
  included or promotional allowance), so it never turns a subscription record into
  `mixed` or `pay-as-you-go`. `unknown` is used when the source does not say —
  never guessed.
- `meter` names what the numbers measure (`subscription allowance`,
  `credits balance`, `chat queries`, `unknown`).
- `account` is a display label at most; it is never a verified identity, and no
  email, token or credential is written into provenance.

All values are normalized (control characters stripped, bounded length) and an
older cache without provenance renders exactly as before. Unknown fields are
omitted by both the CLI and the pane rather than shown as status.

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

Grok is the only provider read from your browser — there is no clean API path
right now — so it is **disabled by default**:

```bash
hermes config set plugins.entries.quota.settings.grokEnabled true
hermes quota refresh
```

When off, Grok reports `opt-in-disabled`: no cookies are read, no files written.
Cookie sources, in order:

1. `~/grok_session.json` if you created one yourself
2. Firefox `grok.com` cookies
3. Chromium / Google Chrome / Brave `grok.com` cookies (macOS and Linux)

Browser notes:

- Sign into <https://grok.com> in the browser at least once.
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
  prefix is stripped after AES-CBC decrypt.
- App-Bound `v20` cookies are not supported (`chrome-app-bound`).
- Safari is not supported.

**Two meters, each with its own source and scope.** The primary read is
`grok.com GetGrokCreditsConfig` — the **Grok Build / Weekly Limit** allowance
the grok.com panel shows — scoped to the signed-in grok.com session. Only when
that parse fails does the fetcher fall back to `grok.com /rest/rate-limits`, the
**per-model chat query** meter on a rolling 2h window; the record's `meter` is
`chat queries` and its note says it is the fallback, not the Build/weekly
allowance. Both records state that this is the grok.com browser session. What
either meter shares with the Hermes `xai-oauth` provider **cannot be established
from a browser session**, so the note says so (may share; unverified) instead of
asserting a relationship. Because the session cannot verify a subscription or a
plan, no billing mode or plan is claimed — a free or unknown Grok account is not
labelled `subscription`. Nothing about either meter is inferred from the
account's name or email.
