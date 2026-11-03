# Hermes Quota

![Quota in Hermes Desktop](docs/images/quota-app.png)

See provider quotas, reset times and account balances in Hermes Desktop and the CLI—without adding network calls to every UI render.

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

Extra Claude accounts are opt-in and read-only: no refresh-token exchange, credential write-back or tokens in the display cache. Requests identify this plugin rather than Claude Code. Primary-account resolution remains Hermes-core behavior. Antigravity now delegates login and network access to the vendor's `agy` CLI, so it no longer reads Antigravity credential files or OS secret stores or impersonates the vendor client. The extra Claude-account reader and other providers still use opt-in browser/OS credential-store reads, configured credential files and vendor client identities; see [provider disclosures](docs/providers.md). Python plugins are trusted host code, not sandboxed applications. **Catalog limitation:** those remaining provider credential readers and OAuth handling do not meet current Hermes catalog rule 11, which requires Hermes-managed credential routes and prohibits direct credential-store reads, OAuth refresh and vendor-client impersonation. Disclosure under rule 13 or a user opt-in does not waive rule 11. This release does not migrate those remaining integrations or establish eligibility for a new catalog pin. [Security details and limitations](docs/security.md).

**Telemetry (catalog rule 13):** the plugin adds no telemetry or third-party usage reporting. The Antigravity provider delegates login and network access to the vendor's `agy` CLI, which may send its own telemetry and write its own local state even for a read-only `/usage` probe; the plugin does not read or alter that state. A standard proxy variable that cannot be validated makes Antigravity unavailable (`agy-unsafe-proxy`) instead of running the CLI without that proxy. See [Providers and their limitations](docs/providers.md).

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
- [Antigravity's backend switch in Enabled providers](docs/antigravity-switch.md)
- [Claude accounts and friendly names](docs/claude-accounts.md)
- [Codex special-reset announcements and source limitations](docs/codex-special-reset.md)
- [Troubleshooting—including remote names/icons](docs/troubleshooting.md)
- [Development and tests](docs/development.md) · [Add a provider](docs/add-provider.md)
- [Release process](docs/release-process.md)
- [Acknowledgements](docs/contributors.md)

MIT · [License](LICENSE)
