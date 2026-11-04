# Privacy and security

- **Telemetry (catalog rule 13):** the plugin adds no telemetry and no
  third-party usage reporting. Cookies and tokens are never printed. This does
  not cover a vendor CLI it invokes: `agy` may send its own telemetry or write
  its own logs and local state (see the Antigravity note below).
- Grok cookies are used only for the Grok billing request, and only when you opt in.
- Chrome/Firefox cookie values are never printed, cached, or written back to disk.
- Antigravity is opt-in and CLI-only: the plugin runs
  `agy -p /usage --output-format json` as a bounded noninteractive subprocess
  (stdin closed, no shell, no permission-skipping flag, capped stdout, an
  allow-listed environment, `AGY_CLI_DISABLE_AUTO_UPDATE=true`, and a per-probe
  `--log-file` inside a private temporary directory). It never reads Antigravity
  credential files or OS secret stores, never refreshes OAuth tokens, never
  calls Google's quota API, and never impersonates the vendor client; `agy`
  owns the login and network access. The plugin reads only the command's
  structured quota payload, never displays or caches raw CLI output, and passes
  no credential in argv or the environment. Standard proxy variables are
  forwarded after validation: proxy URLs containing whitespace, control
  characters or malformed percent escapes are refused, and so is a proxy URL
  with embedded userinfo, so a proxy password is not forwarded. IPv6 proxy
  hosts must use bracket notation. Spaces and tabs around comma-separated
  `NO_PROXY` entries are trimmed; control characters inside an entry are
  refused. If a present proxy variable cannot be validated, the provider
  returns `agy-unsafe-proxy` instead of running `agy` without it, because
  dropping a configured proxy could send the request direct. The exception is
  an unusable `NO_PROXY` when no proxy URL is configured: it cannot change
  routing there, so it is withheld and the provider still runs. `agy --version` must be 1.1.11 or newer before `/usage` runs. The
  payload is undocumented and may change; malformed output becomes an
  unavailable state, not a guess.
  `agy` is still a vendor application: even a read-only `/usage` probe may send
  vendor telemetry and may create or update its own local state under its
  config directory (for example `conversation_summaries.db`, `installation_id`
  and bundled skills). The plugin controls only the per-probe log path and does
  not read or alter that state.
- Extra Claude accounts are opt-in and read-only: an access token is read from the
  listed `.credentials.json`, used for one usage request, and never cached,
  logged, or written back. Only the account `id`/`label`, its windows and
  machine-readable reasons reach the cache — never a token or a filesystem path.
- Missing credentials produce an explicit `unavailable` state — no fake zeros.
- The plugin does not request permission to override built-in Hermes tools.


## Trust boundary

Python plugins execute inside the host process; opt-in settings and timeouts are safeguards, not an OS sandbox. The extra Claude reader only accesses explicitly configured directories, uses access tokens in memory against the fixed Anthropic usage endpoint, refuses redirects, and never exchanges refresh tokens or writes Claude credentials. The primary account still uses Hermes core resolution, which may adopt or refresh an external login according to host policy. See [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security#borrowed-cli-logins).

`CLAUDE_CONFIG_DIR` is documented by Claude Code. The credential-file layout and usage endpoint are implementation-derived, not a public stability guarantee. Linux is verified; extra Windows accounts are experimental, and extra macOS Keychain accounts are unsupported. Vendor authorization/terms and Hermes catalog admission are separate from a passing scanner. No scanner result establishes blanket safety or catalog approval.

Antigravity no longer crosses the borrowed-credential boundary: the provider invokes the vendor's own CLI and consumes only its printed quota. The direct credential-file/keyring read, OAuth refresh, client-identity impersonation and quota API call have been removed. The command is the same one used by the accepted `antigravity-subscription-directsdk` catalog plugin. `agy` remains a vendor application, though: automating `/usage` is subject to vendor terms and may change without notice, and catalog admission of any later revision is still a maintainer decision.

Antigravity's executable trust check is POSIX-only. Group/world-writable files, set-user-ID/set-group-ID files, untrusted file or parent-directory owners, and non-sticky writable parent directories are rejected on POSIX; Windows requires only an absolute, existing regular `.exe`/`.com` file, with no ACL or ownership validation. Neither platform verifies a signature or the binary's provenance, and neither defends against another process already running as the same user. Windows process handling (`CREATE_NO_WINDOW`, `taskkill /T`) is implemented but covered only by mock-based tests, not the verified test matrix. On Windows, `taskkill /T` can enumerate descendants only while the parent is alive; if `agy` exits normally after leaving a descendant holding stdout, that descendant is not guaranteed to be cleaned up, though the reader still stops after a bounded grace and closes the plugin's pipe. Linux is verified.

Never copy tokens into example config, issue reports or screenshots. User-supplied account labels are included in the local quota cache.

## Hermes catalog admission

[Current catalog rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission): **rule 11 (credential routes)** prohibits a plugin from reading another tool's login — a vendor CLI's token file, a browser profile, a keychain or any other credential store — directly, and from refreshing, rotating or writing OAuth tokens or presenting itself as another vendor's client. **Rule 13 (risky-behaviour and telemetry disclosure)** requires risky behaviour, including vendor-CLI use and any remaining credential handling, to be disclosed in the README and PR; disclosure is not an exception to rule 11 and does not make a prohibited route admissible. The Claude quota request now identifies as `hermes-quota-plugin/2.11.0`; it does not impersonate Claude Code. Extra-account access remains a third-party credential read and is therefore outside rule 11's permitted routes; a successful local install, scanner result, opt-in or disclosure does not change that.

Antigravity's CLI-only path removes the direct credential read, token refresh and vendor-identity impersonation that rule 11 prohibits. It does not by itself approve a catalog revision, and vendor automation of `/usage` remains subject to the vendor's terms.

This is a scoped assessment of the Claude multi-account change, not a fresh audit of every provider. Antigravity has migrated to the CLI-only path above; other adapters may retain legacy browser/OS credential-store reads, configured credential files or vendor client identities and are disclosed in [Providers](providers.md). Those legacy routes do not meet rule 11, and disclosure does not waive it. The inherited primary core resolver may refresh a borrowed login according to Hermes host policy; read-only extra accounts do not change that behavior. Catalog publication/re-pinning requires a separate exact-revision review.
