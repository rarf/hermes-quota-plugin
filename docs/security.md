# Privacy and security

- No telemetry. Cookies and tokens are never printed.
- Grok cookies are used only for the Grok billing request, and only when you opt in.
- Chrome/Firefox cookie values are never printed, cached, or written back to disk.
- Extra Claude accounts are opt-in and read-only: an access token is read from the
  listed `.credentials.json`, used for one usage request, and never cached,
  logged, or written back. Only the account `id`/`label`, its windows and
  machine-readable reasons reach the cache — never a token or a filesystem path.
- Missing credentials produce an explicit `unavailable` state — no fake zeros.
- **Desktop Nous card:** the Desktop half reads the focused profile's Nous
  plan/usage through Hermes's own read-only gateway method (`billing.state`,
  called via the Desktop SDK's `host.request`) on
  the configured refresh interval. The gateway may contact Nous Portal for that
  read; the renderer opens no network connection itself. The call is
  profile-scoped and skipped when the plugin cannot name the backend profile
  exactly. Only whitelisted plan/usage fields and the account wallet amount are
  read (including the plan, top-up credit bars, and the organisation slug used
  solely to derive a pseudonymous local match digest); card, payment-method,
  organisation name and other account cash fields are never rendered, cached
  in the query cache, or written to plugin storage, and the response is never
  logged. The whitelisted money reads are
  `usage.total_spendable_display`, the plan/top-up bar display fields and
  `monthly_cap` on credit-grant plans, plus `balance_display` as the
  usage-window wallet; each is mapped to the derived row or the single
  **Account balance** and the raw fields are not cached or stored. Any
  failure (older gateway, unknown profile, timeout) silently keeps the cached
  CLI row. On a credit-grant plan the cached CLI row is a whole-row fallback
  only, never a source of financial facts for a Billing row: `billing.state`
  exposes no signed-in user id, so another login's cached spend, rollover or
  Account balance cannot be proven to belong to the Billing account. A
  usage-window plan (Plus/Super/Ultra) is the narrow exception: Billing's
  dollar-credit model is unavailable there, but Billing's own `balance_display`
  still supplies the account wallet as the one **Account balance** without any
  cached account row. A fresh same-profile cached snapshot with the same exact
  plan label and access state and a matching pseudonymous organisation digest
  may additionally supply neutral `Cycle ends` and `Spend this period` facts,
  and its actual
  total-usable figure outranks Billing's wallet; when no such snapshot exists,
  Billing's wallet remains the balance. That cached total is display data, not
  verified money: it still counts the included subscription credits a pause
  withholds, so a paused plan never takes it and keeps Billing's own wallet (or
  the purchased wallet alone in the CLI row). The organisation digest cannot
  distinguish another signed-in user in the same organisation, so the cached
  total is not account proof. `Cycle ends` is deliberate because
  `billing.state` carries no cancellation state; a cancelled-at-period-end
  plan must not be labeled as renewing. Component
  subscription/top-up amounts are folded into that total, never separate
  lines; a missing total uses the wallet only when the snapshot explicitly
  reports zero included subscription credits. The live rate-limit line may
  merge under the same freshness/plan/state/organisation match. The raw slug
  is not rendered or stored in the query cache. The digest itself is kept in a renderer-wide
  WeakMap keyed by the live billing row; it is never an own property of the
  row, so it is not copied by object spread or JSON serialization, and it
  never enters the sanitized query-cache payload, plugin storage, or a rendered
  detail. It lives only as long as that in-memory row. Same
  organisation + same plan + same state is still only a same-organisation hint,
  **not account proof**: `billing.state`
  exposes no signed-in user id, so another user in the same organisation can
  match. The line is shown as cached account data with that residual limitation;
  every missing or disagreeing fact fails closed. A Billing plan with an exact
  documented label and no mergeable live claim may instead show a
  `(published)` ceiling from the official Nous API docs
  (https://portal.nousresearch.com/api-docs, retrieved 2026-11-06). Those docs
  publish RPM and TPM only, so a published fallback never invents RPH; a live
  account claim always wins, including its RPH value. The product-prefixed
  account label `Hermes Plus` maps to the documented Plus tier; a Free reading
  never inherits a paid ceiling and a paid reading never inherits Free's. A low-balance paid plan
  keeps its exact label, so its published ceiling is still shown even though the
  access state is `unknown`. The Free **Tool pool**
  line is the other non-financial cached fact, and only for a fresh same-profile
  Free snapshot whose `tool_access.enabled` switch is true and whose
  Portal-organisation digest matches Billing's `org_slug`; paid, disabled,
  unknown, stale and organisation-mismatched pools are suppressed. The
  cached Nous account fetcher separately decodes the existing
  Hermes-managed OAuth JWT locally for display-only rate/spend claims; the token
  itself never enters the cache, the renderer or a log, and only whitelisted
  rate/spend values are copied. When the fresh account snapshot omits
  `tool_access`, an enabled switch from the same-account JWT may fill that one
  non-financial field; an explicit fresh `enabled: false` always wins and the
  pool never crosses accounts or paid/free readings. Those facts are merged
  into the fresh account snapshot only when both snapshots prove the same
  Portal, organisation and user identity and a provably identical plan (name
  and/or tier), or a cached Free plan matched to a fresh Free reading; a
  missing, different identity or a changed paid tier contributes no claims, so
  a cached JWT from one login or an older plan can never decorate another
  login's card.
  `billing.state` is an app gateway method reached through the SDK's
  `host.request`, not a documented plugin API or compatibility contract; the
  CLI/TUI keep the account fetcher.
- **Catalog rule 9 coupling:** the CLI/TUI Nous fetcher imports
  `hermes_cli.nous_account`, an undocumented core module, because Hermes has no
  public Nous account-data seam; `hermes plugins validate` reports that private
  import as a rule 9 warning. It is disclosed here and in the PR and is not a
  compliance claim.
- The plugin does not request permission to override built-in Hermes tools.


## Trust boundary

Python plugins execute inside the host process; opt-in settings and timeouts are safeguards, not an OS sandbox. The extra Claude reader only accesses explicitly configured directories, uses access tokens in memory against the fixed Anthropic usage endpoint, refuses redirects, and never exchanges refresh tokens or writes Claude credentials. The primary account still uses Hermes core resolution, which may adopt or refresh an external login according to host policy. See [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security#borrowed-cli-logins).

`CLAUDE_CONFIG_DIR` is documented by Claude Code. The credential-file layout and usage endpoint are implementation-derived, not a public stability guarantee. Linux is verified; extra Windows accounts are experimental, and extra macOS Keychain accounts are unsupported. Vendor authorization/terms and Hermes catalog admission are separate from a passing scanner. No scanner result establishes blanket safety or catalog approval.

Never copy tokens into example config, issue reports or screenshots. User-supplied account labels are included in the local quota cache.

## Hermes catalog admission

[Current catalog rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission), especially rules 11 and 13, require third-party login reads to be disclosed and accepted by a maintainer. Refreshing/rotating another client's tokens or identifying as that vendor client is not admitted without an explicit ruling. **Rule 9 (public surfaces only)** is also relevant: the CLI/TUI Nous fetcher imports the undocumented core module `hermes_cli.nous_account`, which `hermes plugins validate` reports as a private-core warning. That coupling is disclosed here and in the PR; it is not a compliance claim, and Hermes exposes no public Nous account-data seam today. The Claude quota request now identifies as `hermes-quota-plugin/2.10.0`; it does not impersonate Claude Code. Extra-account access remains a third-party credential read and is not automatically approved by a successful local install or scanner.

This is a scoped assessment of the Claude multi-account change, not a fresh audit of every provider. Other adapters have separate credential/client-identity disclosures in [Providers](providers.md). The inherited primary core resolver may refresh a borrowed login according to Hermes host policy; read-only extra accounts do not change that behavior. Catalog publication/re-pinning requires a separate exact-revision review.
