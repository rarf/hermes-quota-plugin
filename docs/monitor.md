# Monitor views (`hermes quota monitor`)

The plugin carries the Agent Monitor / Agent Bar views — live sessions, agent
attention, model health, fallback chains, usage, kanban, quota history and
burn rate — as JSON subcommands of `hermes quota`. The desktop widget calls
them through the gateway's `cli.exec`, exactly like `hermes quota status
--json --cached`; there is no server and no network I/O in any of them.

```bash
hermes quota monitor snapshot --json            # one call per widget poll
hermes quota monitor history --json --days 7    # quota history + burn (1-31 days, default 1)
hermes quota monitor recommend --json [--prefer-provider P] [--exclude P,Q] [--min-left 10] [--top 3]
hermes quota monitor budget --json              # spend today / month vs budget
hermes quota monitor set-budget --daily 20 --monthly 400 --json   # 0 clears a value
```

Every command prints exactly one JSON object on stdout. Every payload carries
`"monitor_version": 1` and `"generated_at"` (unix seconds). A command that
cannot run prints `{"monitor_version": 1, "error": "..."}` and exits 1.

## Data sources (all read-only)

| What | Where |
| --- | --- |
| Sessions, messages, model usage, turn leases | `<root>/state.db` and `<root>/profiles/*/state.db` (sqlite `mode=ro`) |
| Open chat windows | `<home>/runtime/active_sessions.json` (lease registry, live pid only) |
| Kanban | `<root>/kanban.db`, `<root>/kanban/boards/*/kanban.db` |
| Credential pool status | `auth.json` `credential_pool` (labels / status only; tokens are never read out) |
| Primary + fallback models | each profile's `config.yaml` |
| API failures | each profile's `logs/errors.log` |
| Quota windows | the plugin's own `quota_cache.json` (written by `hermes quota refresh`) |
| Claude Code / Copilot CLI | `~/.claude/projects`, `~/.claude/sessions`, `~/.copilot/session-store.db` when present |

`<home>` is `get_hermes_home()` (the active profile); `<root>` is the Hermes
root that holds `profiles/`. Every profile on the machine is included.

The plugin writes only its own files under `<home>`:

- `quota_monitor.db` — `quota_history` (one row per provider window per
  refresh; identical consecutive readings are skipped unless the last one is
  older than 30 min; rows older than 35 days are pruned), `rate_samples` (one
  token/cost sample per `snapshot`, kept 1 h), `kv` (cached model speed, 5 min).
- `quota_monitor.json` — `{"budget": {"daily": x, "monthly": y}}`.

## Quota history

Every quota refresh (`hermes quota refresh`, `/quota refresh`, `status` when it
refreshes, the widget's refresh) appends the new readings. `snapshot`,
`history` and `recommend` also catch up from the current cache, keyed by its
`fetched_at`, so a cache written elsewhere is still recorded once. Provider
ids are the cache keys (`anthropic`, `anthropic:<account id>`, `openai-codex`);
Codex's nested saved accounts become `openai-codex:<account id>`.

## `snapshot`

```jsonc
{
  "monitor_version": 1, "generated_at": 0,
  "live": {...},          // Agent Monitor /api/live minus `quotas`, plus `attention`
  "health": {...},        // /api/health: accounts, chains, models, speed
  "kanban": {"boards": [...]},
  "usage": {"today": <usage>, "month": <usage>},
  "top_sessions": [...],  // last 7 days, 10 rows (Hermes + agent CLIs)
  "clis": {...},          // /api/clis for the last 7 days (sessions capped at 20)
  "errors": {"<section>": "<Exception: message>"},
  "timings_ms": {"<section>": 0}
}
```

A failing section is `null` and its message lands in `errors`; the call
itself never fails because of one section.

### `live`

`now`, `running[]`, `counts` (`open, working, needs_approval, your_turn,
stalled, subagents`), `burn[]`, `activity[]` (60 minutes of message counts by
role), `tokens[]` (60 minutes, `{t, by: {source: {tokens, cost}}}`),
`providers`, `by_profile`, `last_1m`, `last_5m`, `last_60m`, `cost_60m`,
`by_source_60m`, `sampling_since`, `sample_every`, `attention[]`.

The token rate comes from consecutive `snapshot` samples: it fills while the
widget polls. Two samples further apart than 5 minutes carry no rate (that
delta cannot be placed in one minute). `sample_every` is the median gap
between samples in seconds (`null` with fewer than two). Agent CLI rates are
exact (from transcript timestamps) and need no sampling.

`running[]` rows: `id, parent, parent_title, profile, title, model, provider,
started, last, subagent, status (running|ended), end_reason, working, state
(working|needs_approval|waiting|stalled|ended), state_since, waiting_for,
long_tool_call, context_tokens, context_estimated, context_max, context_percent, loop_max, origin,
interactive, activity, tokens, cost, tools, messages, parent_model, kanban,
kanban_card, kanban_status, kind (hermes|claude-code|copilot-cli),
source_name, open, account_provider, account`. `open` is `{kind, url,
command, label, running, pid, tty, app, app_pid, background, attach}`
(`url` = `hermes://open/<id>` only when Hermes.app is installed on macOS;
process facts come from `ps` and are empty where it is unavailable).
`account_provider` / `account` are always `null` here.

Context of an open main Hermes session (`null` everywhere else):

- `context_tokens`: the provider-reported prompt size of the session's last API
  call (Hermes persists it as `sessions.model_config._usage_anchor`) plus
  chars / 4 for the messages appended since — the same rule as Hermes' own
  anchored estimate. Without a usable anchor (older Hermes, a fresh or just
  compressed session) it is chars / 4 of the whole active transcript and
  `context_estimated` is `true`.
- `context_max`: the profile's `model.context_length` when it pins the
  session's model, else Hermes' own model metadata from local sources only
  (Codex OAuth static table for `openai-codex`, then
  `agent.context_pin.advertised_context_length`: the persisted context cache,
  the models.dev disk cache and the built-in catalog). Never a network probe or
  a cache write; cached per process; `null` when unknown or when Hermes'
  package cannot be imported.
- `context_percent`: `context_tokens / context_max`, 0-100, one decimal.

### `attention[]`

Agent Bar's attention rules over running main sessions, most urgent first:

| kind | when |
| --- | --- |
| `approval` | the session waits for a command approval |
| `your_turn` | finished and waiting — only interactive sources (desktop, cli, tui, telegram, slack, discord, agent CLIs); kanban / cron workers never |
| `stalled` | a turn claims to run but wrote nothing for 10 min (30 min with a tool call in flight) |
| `loop` | 3+ identical consecutive tool calls in the last 30 min |
| `big_context` | context ≥ 200 000 tokens (see `context_tokens` above) |

Item: `{id, kind, session_id, profile, title, since, detail, source, value}`.
`id` is stable (`<kind>:<source>:<profile>:<session id>`) for notification
dedupe. `detail` is a short code: `approval` (or the CLI's own reason),
`finished`, `no_progress` / `tool:<name>`, `<n> identical tool calls`,
`<n> tokens`; `value` is the number for `loop` / `big_context`.

### `health`

`accounts[]` (per provider: pooled credentials with `status`, `locked`,
`exhausted_until`, `model_cooldowns`, plus the quota windows of the provider's
main cache row), `models[]` (`status`: ok, degraded, failing, cooldown,
account_exhausted, idle; 24 h calls / errors / error rate / error types,
`last_error`, `recovered`), `chains[]` (each profile's primary model and
fallbacks with `state` ok|risky|skip and `reason` same_backend |
no_credentials | locked | cooldown | error_rate), `speed[]` (24 h turn
seconds p50 / p90 and first-reply p50 per model). Usage rows without a billing
provider are reported under `unknown`.

### `kanban`

Per board: `counts` (all statuses), `blocked_by_kind`, `cards[]` — only
running, blocked, review, failing (`consecutive_failures > 0`, not done) and
done-but-failed in the last 24 h, at most 50 per board (`truncated` = how many
were left out). Card: `id, title, assignee, status, priority, created_at,
started_at, completed_at, last_heartbeat_at, consecutive_failures,
last_failure_error, block_kind, board, waiting_for (input|review|null),
failing, open`.

### `usage`

Agent Monitor's overview `usage` shape for today and for the month to date:
`days, totals, providers, daily[{day, by: {provider: {tokens, cost}},
sessions}], by_provider, by_model, by_profile`. Costs are Hermes' recorded
cost per model row, falling back to the session cost split by token share.
The widget derives "today vs budget" and the month-end projection from
`usage.month.daily`.

## `history`

`{days, history: [{provider, window, points: [[ts, used%], ...]}], burn: [...],
errors}`. `burn[]`: `{provider, window, used, per_hour, eta_full, reset,
fills_before_reset}` — the slope of used % over the last 2 h (at least 10 min
of readings, only readings after the last reset), `eta_full` when it keeps
rising, `fills_before_reset` when that is before the window's reset.

## `recommend`

Agent Monitor's `recommend_model`: `{recommended: [{model, provider, score,
reasons}], excluded: [{model, provider, reason}]}`. A model is excluded when
its status is not ok/degraded, its provider has no unlocked credential, the
best account of its provider has less than `--min-left` % in some window, or
it was never used here. Score: quota headroom, minus 20 when a window fills
before reset, error rate, recent use, 24 h median turn speed, `--prefer-provider`.

## `budget` / `set-budget`

`budget`: `{currency, today, yesterday, month_to_date, month_end_projection,
daily_budget, daily_used_pct, monthly_budget, monthly_used_pct}` — Hermes plus
agent-CLI spend at API prices. `set-budget` stores the widget's values and
answers `{budget: {daily, monthly}}` (`null` = not set).

## `accounts` / `set-priority` / `set-strategy`

`accounts` (read-only) answers which credential each profile uses:

- `pools[]` — one entry per credential pool: `{provider, owner, shared, used_by[], strategies{profile: s},
  explicit_strategy{}, rows[], quota, writable}`. A named profile with no rows of its own for a provider
  borrows the root (`default`) pool (Hermes' `read_credential_pool` fallback), so `owner` is `default` and
  `used_by` lists every borrower; a profile with its own rows gets its own pool. Rows carry
  `{id (short pool id), label, auth_type, source, priority, position, status: active|cooldown|exhausted,
  status_until, cooldown_models[], last_error_code, requests, tried_first, windows[], quota_match}` — never a
  token, key, refresh token or base URL. `windows` are attached only when a quota-cache account label
  matches the row (`quota_match: "label"`) or the pool has a single row (`"sole"`); otherwise the
  provider-level reading stays in `pool.quota`.
- `profiles[]` — `{profile, is_root, model{provider, model}, fallbacks[{provider, model, custom_route}],
  fallback_editable: false, fallback_command, strategies{}, pools{provider: own|shared}}`.
- `unavailable[]` — quota-cache providers without data: `{provider, base, reason, kind:
  signin|reauth|optin|rate_limited|error, retry_at, settings_path, command, external_tool, optin_key}`.
  `command` is a copyable terminal line (`hermes auth add <id>`, or the external tool's login such as
  `gh auth login`); `settings_path` is the desktop Settings route (`/settings?tab=providers&pview=accounts`
  or `…&pview=keys`).
- `capabilities` — `set_priority`, `set_strategy` true; `reorder_fallbacks` and
  `assign_account_to_profile` false (Hermes has neither non-interactively).

Writes validate against the current state, then run Hermes' own CLI in a child process, re-read the
result and log it in `quota_monitor.db` (`account_audit`):

- `set-priority --provider P --target <pool id> --priority N [--profile <owner>]` →
  `hermes --profile <owner> auth priority P <id> N` (N is zero-based, 0 = tried first under `fill_first`).
  Refused with `error_code: borrowed-pool` when the profile only borrows the pool (reorder the owner's pool
  instead — it applies to every sharing profile); a borrower never gets a copy of the rows.
- `set-strategy --provider P --strategy S [--profile p]` →
  `hermes --profile p config set credential_pool_strategies.P S` (one profile only).

Both answer `{action, changed, before, after, affected_profiles, command, cli_output, state}` or
`{error, error_code}` (`bad-provider`, `bad-profile`, `unknown-profile`, `no-pool`, `borrowed-pool`,
`unknown-target`, `bad-priority`, `bad-strategy`, `cli-failed`, `not-applied`). `--profile` defaults to
the process profile (`hermes -p X quota monitor …` selects X).

## Agent tools

When Hermes supports `register_tool`, the plugin registers read-only tools in
the `quota_monitor` toolset: `get_quota_status`, `get_model_health`,
`get_fallback_chains`, `get_budget`, `get_running_counts`,
`get_agent_attention`, `recommend_model`. They answer with numbers and status
codes only — never session titles or upstream error text — so other sessions'
content cannot steer the calling agent.

## Platform notes

macOS-only facts (`ps` ancestry, Hermes.app URLs, Claude Code's process
registry) degrade to empty values on Linux or when the tool is missing. Not
ported from Agent Monitor: the per-machine share of a shared Claude account
(`local`, `local_history`), per-session Claude account attribution, and the
native menu bar / notch / macOS widget surfaces.
