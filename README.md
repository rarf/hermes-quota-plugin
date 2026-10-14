# Hermes Quota

![Quota in Hermes Desktop](docs/images/quota-app.png)

See provider quotas, reset times and account balances in Hermes Desktop and the CLI—without adding network calls to every UI render.

## What you get

- A docked pane and status-bar indicator with per-provider usage.
- Model-specific quota windows and reset times when the vendor supplies them.
- Separate, configurable Claude account cards; no summed or invented allowances.
- Honest unavailable states and opt-in access to sensitive credential sources.

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

## Multiple Claude accounts

Additional accounts are explicitly configured by directory and display label. Labels such as **Work**, **Personal** or **Account 2** are user-configurable—not hardcoded. Linux is verified; additional platform limitations are documented.

[Configure Claude accounts →](docs/claude-accounts.md)

## Usage provenance (where a number comes from, and what it means)

Every provider row carries normalized **provenance** so a percentage is never read
as something it is not. It flows from the fetchers through the cache into the
CLI (`hermes quota`) and the Desktop pane.

- **Source / scope** — the endpoint and the budget the figures cover, e.g.
  `api.anthropic.com/api/oauth/usage` scoped to *this login*.
- **Plan source** — a plan is *verified* only when it is provider response
  metadata (`provider-response`) or a local credential field
  (`local-credential`). A user-chosen display label is `user-label` and is never
  presented as a verified plan. A plan is **never inferred from an email
  address** — an `.edu` (or any) domain says nothing about a tier. The primary
  Claude login's plan is read from the local `.credentials.json` **only when its
  `accessToken` exactly matches the resolved primary credential**; from any other
  source, or on a mismatch, the plan stays `unknown` rather than being guessed or
  attached across logins.
- **Billing mode** — `subscription` (a plan allowance), `pay-as-you-go`
  (a purchased balance/spend), `mixed` when a single record shows both, or
  `unknown`. A subscription allowance is not the same money as a wallet balance.
  A credits-balance figure alone is not treated as proof of pay-as-you-go money
  (it may be included or promotional), so it never changes a subscription record
  into `mixed` and a balance-only record stays `unknown`.
- **Meter** — what the numbers measure: `subscription allowance`,
  `credits balance`, `chat queries`, or `unknown`.
- **Account** — the row's display label. It is never a verified identity, and
  no email or token is ever written to the cache or the UI.

Unknown is honest: a field the provider does not expose stays `unknown`, and no
limit is invented. Malformed or unrecognized provenance is dropped rather than
shown.

### Grok has two different meters

Grok is read from the signed-in **grok.com browser session**, not from the
Hermes `xai-oauth` provider, and each record says so:

- **Primary** — `grok.com GetGrokCreditsConfig`, the **Grok Build / Weekly
  Limit** allowance the grok.com panel shows.
- **Fallback** — `grok.com /rest/rate-limits`, the **per-model chat query**
  meter on a rolling 2h window. This is reached only when the primary parse
  fails, and it is not the Build/weekly allowance.

Whether either meter shares a budget with the Hermes `xai-oauth` provider
**cannot be established from a browser session** — the row's note says it may
share and is unverified, rather than asserting a relationship. Because the
browser session cannot verify a subscription or plan, no billing mode or plan is
claimed: a free or unknown Grok account is not labelled `subscription`. The Grok
read stays **opt-in** and cookie-gated exactly as before.

## Privacy and security

Extra Claude accounts are opt-in and read-only: no refresh-token exchange, credential write-back or tokens in the display cache. Requests identify this plugin rather than Claude Code. Primary-account resolution remains Hermes-core behavior. Other providers may use opt-in browser/OS credential-store reads and vendor client identities; see [provider disclosures](docs/providers.md). Python plugins are trusted host code, not sandboxed applications. Third-party login reads require disclosure and maintainer judgment for catalog admission. [Security details and limitations](docs/security.md).

## Documentation

- [Installation, profiles and updates](docs/installation.md)
- [Usage, settings and commands](docs/usage.md)
- [Providers and their limitations](docs/providers.md)
- [Claude accounts and friendly names](docs/claude-accounts.md)
- [Troubleshooting—including remote names/icons](docs/troubleshooting.md)
- [Development and tests](docs/development.md) · [Add a provider](docs/add-provider.md)
- [Acknowledgements](docs/contributors.md)

MIT · [License](LICENSE)
