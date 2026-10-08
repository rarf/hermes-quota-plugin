# Privacy and security

- No telemetry. Cookies and tokens are never printed.
- Grok cookies are used only for the Grok billing request, and only when you opt in.
- Chrome/Firefox cookie values are never printed, cached, or written back to disk.
- Extra Claude accounts are opt-in and read-only: an access token is read from the
  listed `.credentials.json`, used for one usage request, and never cached,
  logged, or written back. Only the account `id`/`label`, its windows and
  machine-readable reasons reach the cache — never a token or a filesystem path.
- Missing credentials produce an explicit `unavailable` state — no fake zeros.
- The plugin does not request permission to override built-in Hermes tools.


## Trust boundary

Python plugins execute inside the host process; opt-in settings and timeouts are safeguards, not an OS sandbox. The extra Claude reader only accesses explicitly configured directories, uses access tokens in memory against the fixed Anthropic usage endpoint, refuses redirects, and never exchanges refresh tokens or writes Claude credentials. The primary account still uses Hermes core resolution, which may adopt or refresh an external login according to host policy. See [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security#borrowed-cli-logins).

`CLAUDE_CONFIG_DIR` is documented by Claude Code. The credential-file layout and usage endpoint are implementation-derived, not a public stability guarantee. Linux is verified; extra Windows accounts are experimental, and extra macOS Keychain accounts are unsupported. Vendor authorization/terms and Hermes catalog admission are separate from a passing scanner. No scanner result establishes blanket safety or catalog approval.

Never copy tokens into example config, issue reports or screenshots. User-supplied account labels are included in the local quota cache.

## Hermes catalog admission

[Current catalog rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission), especially rules 11 and 13, require third-party login reads to be disclosed and accepted by a maintainer. Refreshing/rotating another client's tokens or identifying as that vendor client is not admitted without an explicit ruling. The Claude quota request now identifies as `hermes-quota-plugin/2.10.0`; it does not impersonate Claude Code. Extra-account access remains a third-party credential read and is not automatically approved by a successful local install or scanner.

This is a scoped assessment of the Claude multi-account change, not a fresh audit of every provider. Other adapters have separate credential/client-identity disclosures in [Providers](providers.md). The inherited primary core resolver may refresh a borrowed login according to Hermes host policy; read-only extra accounts do not change that behavior. Catalog publication/re-pinning requires a separate exact-revision review.
