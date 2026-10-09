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
| `minimax` | Subscription Key **or** OAuth (`minimax-oauth`) | Token Plan 5h + Weekly windows per model; pay-as-you-go keys show `no-subscription`. The `video` model bucket is **opt-in** (default off — low tiers don't include video, so the entry reports a meaningless 100%). Enable with `hermes config set plugins.entries.quota.settings.minimaxVideoEnabled true` or `HERMES_QUOTA_MINIMAX_VIDEO_ENABLED=1`. |
| `ollama` | `OLLAMA_API_KEY` (`sk-…`, from ollama.com/settings) | Ollama Cloud, from `GET /api/usage` (spend, per-model request counts) and `POST /api/me` (plan label, signup date). The **Monthly** bar is the server-reported usage fraction. The reset is the next monthly anniversary of `CreatedAt`, matching Ollama's documented "resets monthly on the same day of the month your plan started"; the day is derived because no endpoint returns a reset timestamp. **No balance value** — Ollama exposes none; see below. |
| `grok` | browser cookies | **Opt-in**, disabled by default. The xAI API exposes no plan or account name, so the card shows neither. |

Each fetcher is **fail-open**: a broken provider shows `unavailable (<reason>)`
and never blocks the rest.

### Ollama

Ollama Cloud quota is read from two endpoints: `GET /api/usage` for the spend
and per-model request counts, and `POST /api/me` for the plan label and the
signup date. Set `OLLAMA_API_KEY` to the `sk-…` key from
[ollama.com/settings](https://ollama.com/settings).

**There is no balance or credit value, because Ollama does not expose one.**
The settings page shows a dollar balance, but it is not reachable from the
API: of 36 candidate paths tried with both `GET` and `POST`, only `/api/usage`
and `/api/me` respond, and `/api/usage` ignores `?include=balance` and
`?include=credits`. This has been reported upstream and is not being worked
on yet:

- [ollama/ollama#12532 — Expose cloud usage stats via `/api/me`](https://github.com/ollama/ollama/issues/12532)
- [ollama/ollama#18653 — Add an API endpoint for credit balance](https://github.com/ollama/ollama/issues/18653)

Until one of those lands, the card shows spend and request counts rather than
a fabricated `$0`. The remaining path — scraping `ollama.com/settings` with a
`__Secure-session` browser cookie — is a sensitive-source reader that would
need to be opt-in and disabled by default, like `grok`. It is not implemented
here.

Until `/api/me` exists the plugin already handles the account, but it cannot
know the plan or derive the reset, so the card falls back to spend and request
counts alone. Until the balance endpoint exists there is nothing to add.

### Antigravity

Antigravity quota collection is disabled unless you opt in:

```sh
hermes config set plugins.entries.quota.settings.antigravityEnabled true
```

Alternatively, set `HERMES_QUOTA_ANTIGRAVITY_ENABLED=1`. If enabled, the plugin
reads Antigravity's token from Windows Credential Manager (`gemini:antigravity`),
macOS Keychain (service `gemini`, account `antigravity`), or Linux's
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
