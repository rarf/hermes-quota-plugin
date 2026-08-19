# Hermes Quota

[![License](https://img.shields.io/github/license/rarf/hermes-quota-plugin)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/rarf/hermes-quota-plugin)](https://github.com/rarf/hermes-quota-plugin/commits/master)
[![Hermes plugin](https://img.shields.io/badge/Hermes-plugin-7c5cff)](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode)

See your AI provider quota before the next request fails.

Hermes Quota adds a small status indicator, a detailed quota page, and a CLI
command to Hermes Desktop. It reads a local cache, so checking the status never
adds another network call to a normal agent response.

## What you get

- A status-bar indicator for the provider with the lowest remaining quota.
- A `/quota` page with every provider, window, percentage, and reset time.
- `hermes quota` and `/quota` commands.
- Honest unavailable states when a provider has no credentials or no supported
  quota endpoint.
- Optional footer and `/usage` integration on Hermes builds that expose those
  lifecycle hooks.

![Hermes Quota status bar](https://github.com/user-attachments/assets/4bbb316e-cea1-4837-9aff-fbdef45287a5)

![Hermes Quota page](https://github.com/user-attachments/assets/57f027fe-da1d-470f-8800-1493fab69b8d)

The configuration preview below shows the public settings surface. It contains
no account data or private paths.

![Hermes Quota settings preview](https://github.com/user-attachments/assets/db592a97-7508-4908-9e95-26508e82b49d)

## Install

Run this from Git Bash on Windows, or from a normal shell on macOS/Linux:

```bash
git clone https://github.com/rarf/hermes-quota-plugin.git
cd hermes-quota-plugin
./install.sh
```

The installer copies the backend and Desktop widget, enables the plugin, and
adds it to existing Bot profiles without removing their other enabled plugins.

> **Important:** close and reopen Hermes Desktop after installation or an
> update. `Reload desktop plugins` reloads JavaScript, but it does not remount
> the Python dashboard backend.

Verify the installation:

```bash
hermes plugins doctor quota
hermes quota refresh
hermes quota status
```

## Providers

| Provider | How quota is read |
|---|---|
| OpenAI Codex | Hermes account usage |
| Anthropic | Hermes account usage |
| Nous | Hermes account usage |
| OpenRouter | Hermes account usage |
| Grok | Local browser session, then Grok billing endpoint |
| Gemini | Gemini CLI credentials |
| Kimi | Local Kimi session credentials |

### Grok on Windows

If the Grok bearer token can run models but cannot read billing, the plugin uses
the logged-in Firefox profile automatically. It reads only `grok.com` cookies
from Firefox's local `cookies.sqlite`; values stay in memory and are never
printed.

1. Sign in to `https://grok.com` in Firefox.
2. Close Firefox, so its cookie database can be copied cleanly.
3. Run:

```powershell
hermes quota refresh
hermes quota grok
```

No token or cookie needs to be pasted into chat.

If Firefox is unavailable, the plugin still accepts a manually exported
browser session. Copy only the `Cookie` header value from a logged-in Grok
request, then run:

```powershell
powershell -ExecutionPolicy Bypass -File `
  "$env:LOCALAPPDATA\hermes\plugins\quota\scripts\import-grok-cookies.ps1"
```

The importer reads the clipboard locally and writes
`%USERPROFILE%\grok_session.json`. It never prints the cookie.

A rejected or expired browser session is reported as `auth-failed` or
`cloudflare-blocked`, not as a fake `0%` quota.

## Commands

```text
$ hermes plugins doctor quota
Plugin Doctor: .../plugins/quota
  manifest: quota 1.0.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 0 hook(s)
```

```text
$ hermes quota status
📊 **quota** (fetched 1m ago)

• **openai-codex** · Session 45% (reset tomorrow 06:43)
• **grok** · Weekly 72% (reset tomorrow 18:00)
• **gemini**: unavailable (consumer-tier-deprecated)

_Run `/quota refresh` to force a re-fetch._
```

Useful commands:

```bash
hermes quota                    # all providers
hermes quota refresh            # fetch fresh data
hermes quota grok               # one provider
```

Inside a Hermes chat:

```text
/quota
/quota refresh
/quota grok
```

## How it works

```text
Provider quota sources
        │
        ▼
  quota_cache.json
     ┌──┴──┐
     ▼     ▼
 Desktop  CLI/chat
 widget   /quota
```

The Desktop widget calls the plugin API at:

```text
/api/plugins/quota/quota
```

The backend reads the precomputed cache. Provider refreshes happen only when
requested or scheduled, not inline with every model response.

## Privacy and safety

- No telemetry or analytics are added.
- Cookies and tokens are never printed by the plugin or importer.
- Grok browser cookies are used only for the Grok billing request.
- Missing credentials produce an explicit unavailable state.
- The plugin does not request permission to override built-in Hermes tools.
- The installer keeps one global plugin copy and updates Bot profile settings
  without deleting unrelated configuration.

## Troubleshooting

### Desktop says `Quota backend unavailable`

Close every Hermes Desktop window and reopen it. The Python dashboard backend is
mounted when the Desktop backend starts; reloading JavaScript alone is not
sufficient.

Then run:

```bash
hermes plugins doctor quota
hermes quota refresh
```

### Grok says `no-session-cookies`

Use Firefox, sign in to Grok, close Firefox, and run `hermes quota refresh`.
The plugin searches the standard Firefox profile automatically.

### Grok says `auth-failed` or `cloudflare-blocked`

The browser session expired or Grok rejected the request. Sign in again and
refresh the quota. Do not paste cookies into an issue or chat.

## Development

The plugin is a standalone Hermes plugin. The main files are:

```text
plugin.yaml                       runtime manifest
__init__.py                       CLI, slash command, optional hooks
quota_cache.py                    cache orchestration
quota_providers/                  provider fetchers
 dashboard/plugin_api.py          authenticated Desktop API routes
desktop/plugin.js                 Desktop widget
scripts/import-grok-cookies.ps1   manual local cookie fallback
```

Run the local checks:

```bash
bash -n install.sh
python -m py_compile __init__.py commands.py quota_cache.py
hermes plugins doctor quota
hermes quota refresh
```

To add a provider, register a fetcher in `quota_providers/` that returns a
`QuotaResult`. The cache and widget do not need provider-specific changes.

## License

MIT. See [LICENSE](LICENSE).
