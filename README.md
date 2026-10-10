# Hermes Quota

![Quota in Hermes Desktop](docs/images/quota-app.png)

See provider quotas, reset times and account balances in Hermes Desktop and the CLI—without adding network calls to every UI render.

## What you get

- A docked pane and status-bar indicator with per-provider usage.
- Model-specific quota windows and reset times when the vendor supplies them.
- Separate, configurable Claude account cards; no summed or invented allowances.
- Honest unavailable states and opt-in access to sensitive credential sources.
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

## Multiple Claude accounts

Additional accounts are explicitly configured by directory and display label. Labels such as **Work**, **Personal** or **Account 2** are user-configurable—not hardcoded. Linux is verified; additional platform limitations are documented.

[Configure Claude accounts →](docs/claude-accounts.md)

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
