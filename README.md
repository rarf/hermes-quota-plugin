# Hermes Quota

![Quota in Hermes Desktop](docs/images/quota-app.png)

See provider quotas, reset times and account balances in Hermes Desktop and the CLI—without adding network calls to every UI render.

**3.1 redesigns the Desktop views:** every saved account and quota window is
visible at once as compact rows with bars, and the status bar names the
limiting account with an explicit `+N`.

**3.0 adds Agent Monitor:** an opt-in view of live Hermes sessions, context usage,
model health, usage estimates and quota history inside Desktop. No separate
monitoring server. Existing quota views remain available without enrollment.

## New in 3.0

- Quota history and burn-rate forecasts, when enough readings are available.
- Live session states and attention indicators for the selected profile.
- Model health, fallback-chain visibility and recorded usage estimates.
- Status-bar agent chips, context guard and configurable notifications.
- Localized monitor UI: English, Turkish, German, Spanish, French, Russian,
  Japanese, Simplified Chinese, Traditional Chinese and Arabic (RTL).

Agent Monitor ships **off by default**. Enable collection only for the profile
that you want to monitor:

```bash
hermes config set plugins.entries.quota.settings.monitorEnabled true
```

For a named profile, use `hermes -p <profile> config set` with the same setting.
Disable it by setting the value to `false`. Disabling stops collection; it does
not erase previously collected history.

This first release deliberately keeps the hardened read-only scope from the
trial: no sibling-profile collection, external CLI transcripts, shared Kanban
boards, new credential-store reads, account/budget mutations or monitor agent
tools. Tabs for unavailable capabilities do not enable them. Usage costs are
**estimated API-price equivalents, not invoices**; month-end spending projection
is not provided. [Monitor behavior, privacy and limitations →](docs/monitor.md)

### Thank you, @Asimatasert

A huge thank-you to [@Asimatasert](https://github.com/Asimatasert) for proposing
and implementing the Agent Monitor integration, bringing the work from
[agent-monitor](https://github.com/Asimatasert/agent-monitor) into Hermes Quota,
and contributing the backend, Desktop views, localization and tests. This is
the contribution behind the 3.0 milestone. The integration retains the existing
quota architecture and adds maintainer-side safety boundaries and fixes.
[Original proposal #83](https://github.com/rarf/hermes-quota-plugin/issues/83).

## What you get

- A docked pane and status-bar indicator with per-provider usage.
- Model-specific quota windows and reset times when the vendor supplies them.
- Separate, configurable Claude account cards; no summed or invented allowances.
- Honest unavailable states and opt-in access to sensitive credential sources.
- Optional Ink TUI quota chip with an interactive account picker and theme colors.
- Codex special-reset announcements, separate from account renewals, with data from [Codex Resets](https://codex-resets.com/).
- A bundled `quota-check` skill (`quota:quota-check`) teaching agents to check
  quota before long work and to ask before splitting tasks or switching models.

## Install

```bash
git clone https://github.com/rarf/hermes-quota-plugin.git
cd hermes-quota-plugin
./install.sh
```

**The source installer enables quota for all existing profiles.** Review that scope before running it. For remote Desktop setups, install/update the widget on the computer running the app as well as the backend on the gateway. [Installation and updates](docs/installation.md).

```bash
hermes plugins doctor quota --ci
hermes quota refresh
hermes quota status
```

## Optional terminal panel

After installing the backend, copy the optional widget into one profile:

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/tui-widgets"
cp tui/quota-panel.mjs "${HERMES_HOME:-$HOME/.hermes}/tui-widgets/quota-panel.mjs"
hermes --tui
```

Run `/quota-select` to choose a provider/account; `/quota-panel` hides or reopens
the right-aligned, theme-colored quota chip. Requires Hermes' Ink user-widget SDK;
the classic CLI continues to use `/quota`. The main installer does not enable this
widget automatically. [TUI setup, commands and limitations](docs/tui-panel.md).

## Multiple Claude accounts

Additional accounts are explicitly configured by directory and display label. Labels such as **Work**, **Personal** or **Account 2** are user-configurable—not hardcoded. Linux is verified; additional platform limitations are documented.

[Configure Claude accounts →](docs/claude-accounts.md)

## Privacy and security

Extra Claude accounts are opt-in and read-only: no refresh-token exchange, credential write-back or tokens in the display cache. Requests identify this plugin rather than Claude Code. Primary-account resolution remains Hermes-core behavior. Other providers may use opt-in browser/OS credential-store reads and vendor client identities; see [provider disclosures](docs/providers.md). Python plugins are trusted host code, not sandboxed applications. **Catalog rule 11:** some provider integrations read vendor credential stores directly, use vendor OAuth handling or vendor client identities instead of Hermes-managed credential routes. They stay opt-in and disclosed, and 3.1.0 does not change them; whether that is acceptable for a catalog pin is for the catalog reviewers to decide. **Rule 9 coupling:** the CLI/TUI Nous row imports `hermes_cli.nous_account`, an undocumented Hermes core module, to use the existing Nous login; `hermes plugins validate` warns about that private-core import under catalog rule 9. It is disclosed here rather than claimed compliant, and Hermes currently exposes no public Nous account-data seam. [Security details and limitations](docs/security.md).

**Desktop Nous card (catalog rule 13):** the Desktop card reads your Nous billing/usage summary through Hermes's own read-only gateway method (`billing.state`, called through the Desktop SDK's `host.request`) on the configured refresh interval; the gateway may query Nous Portal for that read, while the Desktop renderer never sees your sign-in (the CLI/TUI row reads the same login through Hermes's core helper; see the rule 9 note above). Only whitelisted plan/usage fields and the account wallet amount are read (including the plan, top-up credit bars, and the usage-window wallet `balance_display`); card, payment-method, organization-name and other account cash fields are never rendered, cached or stored, and the response is not logged. Billing is the sole financial source for credit-grant plans; the cached account row is only a whole-row fallback. A usage-window plan (Plus/Super/Ultra) has no dollar-credit model in Billing, so Billing's own `balance_display` wallet supplies the prominent **Account balance** even when `usage.available` is false; a fresh same-profile cached account snapshot may add neutral `Cycle ends` and spend lines, and its reported total-usable figure outranks Billing's wallet when its exact plan label and access state match Billing's and its Portal-organisation digest matches Billing's `org_slug`; Billing's wallet remains the balance when no fresh matching snapshot exists. **Cached-balance risk:** that cached total is a point-in-time Portal figure, not a verification of the current wallet, and while a plan is paused it still counts the included subscription credits the pause withholds. A paused plan therefore never takes the cached total: Billing's own wallet (or, in the CLI row, the purchased wallet alone) is the only balance shown, so a suspended subscription allowance cannot reappear as spendable credit. Because the organisation digest cannot distinguish another signed-in user in the same organisation, the cached total is display data rather than an authoritative balance; Billing's own wallet, plan and windows never depend on that match. `Cycle ends` is deliberately neutral because `billing.state` carries no cancellation state: a cancelled-at-period-end plan would otherwise be mislabeled "Renews". Component subscription/top-up amounts are folded into that one balance, never rendered as separate lines, and the card never claims the top-up automatically pays included subscription usage. A missing total may use the wallet only when the snapshot explicitly reports zero included subscription credits; an unknown component is never summed into a fabricated total. Billing's plan, access state, windows and any amount it did supply stay authoritative, and a stale, missing or mismatched snapshot contributes nothing. Two further account facts may accompany a Billing row under the same exact-plan/access-state/Portal-organisation match: a live **Rate limits** line, and the Free **Tool pool** line when the account fetcher's live `tool_access.enabled` is true. That organisation match cannot distinguish another signed-in user in the same organisation, so the rate line and cached cycle-end/spend facts are cached account data with that residual limitation, not account proof; a missing, stale or mismatched fact fails closed. When no live claim is mergeable, an exact documented plan label (including the account API's product-prefixed `Hermes Plus`) gets its **published** RPM/TPM ceiling from the [official Nous API docs](https://portal.nousresearch.com/api-docs) (retrieved 2026-11-06), marked `(published)`; no RPH is invented, and a low-balance paid plan keeps its published ceiling while its access state is unknown. `billing.state` is an app gateway method reached through the SDK's `host.request`, not a documented plugin API or compatibility contract, so an unsupported or older gateway falls back to the cached CLI row.

## Grok authentication

Grok is opt-in. When enabled, the provider reuses Hermes' existing `xai-oauth`
access token as a Bearer token for the Grok billing endpoint. If the token is
unavailable or the endpoint rejects it as an authentication failure, the
provider preserves the browser fallback chain: `~/grok_session.json`, Firefox,
then Chromium/Chrome/Brave cookies. On Windows, Chromium cookie values use the
current user's DPAPI-protected Local State key and AES-256-GCM `v10`/`v11`
decryption. App-Bound `v20` cookies remain unsupported and are reported as
`chrome-app-bound`; cookie values are never printed or written back.

## Documentation

- [Installation, profiles and updates](docs/installation.md)
- [Usage, settings and commands](docs/usage.md)
- [Providers and their limitations](docs/providers.md)
- [Claude accounts and friendly names](docs/claude-accounts.md)
- [Codex special-reset announcements and source limitations](docs/codex-special-reset.md)
- [Troubleshooting—including remote names/icons](docs/troubleshooting.md)
- [Development and tests](docs/development.md) · [Add a provider](docs/add-provider.md)
- [Release process](docs/release-process.md)
- [Acknowledgements](docs/contributors.md)

MIT · [License](LICENSE)
