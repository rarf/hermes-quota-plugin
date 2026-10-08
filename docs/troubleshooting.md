# Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "backend unavailable" in a named profile | Plugin installed only at the global root | Re-run `./install.sh` (it links every profile), then restart |
| Old provider names or icons | The widget on the app machine is an older build than the backend | Update the widget copy there and reload desktop plugins; restart for backend changes |
| Notice: *Widget vX · backend vY* | The two halves are different builds | Reload desktop plugins; restart the app if the backend is the older one |
| *Update available* banner while `hermes plugins update` reports **already at catalog pin** | The install is Hermes-managed and pinned to the catalog entry's commit; the banner compares the installed build against the default branch | Update past the pin as described under [Updating](installation.md#updating), or wait for the catalog entry to advance |
| `unavailable (opt-in-disabled)` for grok | Grok is opt-in | `hermes config set plugins.entries.quota.settings.grokEnabled true` |
| `unavailable (config-invalid)` under an Anthropic account | A `claudeAccounts` entry is malformed (missing `id`/`configDir`, duplicate `id`, wrong type) | The row's detail line names the entry to fix; see [Claude accounts](claude-accounts.md) |
| `unavailable (unsupported-platform)` for an Anthropic account on macOS | Claude Code's macOS login lives in the Keychain, which this plugin does not read per directory | Point `configDir` at a directory containing `.credentials.json`, or use the default Hermes login |
| `unavailable (timeout)` after a refresh | A provider endpoint hung past the sweep budget | It drops its previous value until a refresh succeeds; check the provider's status page |
| `unavailable (503 …)` for opencode-go | Upstream flakiness, not your key | The fetcher already retries; it recovers on a later poll |
| Numbers not changing | Check the pane footer: `· <age> old · poll <N>s` | If the age grows past the interval, report it — the poll should be exact |
| `chrome-tcc-denied` / `chrome-keychain-denied` | macOS privacy prompts | Grant Full Disk Access / approve the Keychain item, then retry |
| `chrome-decrypt-failed` on Linux | Keyring locked or profile belongs to another browser | Unlock the login keyring, or check the profile is Chromium/Chrome/Brave |
| `chrome-app-bound` | App-Bound `v20` cookies | Not supported; use Firefox or `~/grok_session.json` |
| `chrome-crypto-missing` | Missing dependency | Install the `cryptography` package |

Typed failure reasons surfaced by a refresh: `chrome-tcc-denied`,
`chrome-keychain-denied`, `chrome-app-bound`, `chrome-unknown-prefix`
(unrecognized cookie format), `chrome-decrypt-failed`, `chrome-crypto-missing`.


## Remote Desktop: old names or missing icons

The renderer loads JavaScript on the **computer running Hermes Desktop**; quota CLI calls execute on the connected gateway. Updating the gateway's `desktop-plugins/quota/plugin.js` does not update a separate client computer.

1. Check **Widget** and **backend** versions in the Quota pane. Multi-account names and icons require widget **2.10.0 or newer**.
2. Update the widget on the Desktop computer from the same approved build as the backend, not merely an older catalog pin.
3. Run **Reload desktop plugins** in the Desktop command palette, or reopen the app if the client version has no reload command. This reloads client contributions; it is not a gateway restart.
4. If it remains wrong, report the two versions and the visible account heading (no credential files). Rendering source/tests does not prove the running client adopted it.
