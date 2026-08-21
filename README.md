# Hermes Quota

[![License](https://img.shields.io/github/license/rarf/hermes-quota-plugin)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/rarf/hermes-quota-plugin)](https://github.com/rarf/hermes-quota-plugin/commits/master)
[![Hermes plugin](https://img.shields.io/badge/Hermes-plugin-7c5cff)](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)

See provider quota before the next request fails.

Hermes Quota adds:

- a small status-bar indicator for the lowest remaining quota;
- a native **Quota** page in Hermes Desktop;
- `hermes quota` in the terminal;
- `/quota` inside Hermes chats.

The UI reads a local cache. It does not add quota requests to normal agent
responses.

![Hermes Quota status bar](https://github.com/user-attachments/assets/4bbb316e-cea1-4837-9aff-fbdef45287a5)

![Hermes Quota page](https://github.com/user-attachments/assets/57f027fe-da1d-470f-8800-1493fab69b8d)

## Install

### Windows

Run from **Git Bash**:

```bash
git clone https://github.com/rarf/hermes-quota-plugin.git
cd hermes-quota-plugin
./install.sh
```

### macOS and Linux

Run from a normal shell:

```bash
git clone https://github.com/rarf/hermes-quota-plugin.git
cd hermes-quota-plugin
./install.sh
```

The installer:

1. validates the staged Python files;
2. preflights and validates every affected plugin list;
3. replaces old plugin files cleanly, so removed files do not linger;
4. rolls back files and configuration if any update step fails;
5. installs the backend and Desktop widget;
6. enables `quota` without allowing built-in tool overrides;
7. adds `quota` to existing Hermes profiles without removing other plugins.

> [!IMPORTANT]
> Close **every** Hermes Desktop window and reopen the app after installation or
> update. **Reload desktop plugins** refreshes JavaScript, but it cannot mount the
> Python API backend.

Verify:

```bash
hermes plugins doctor quota
hermes quota refresh
hermes quota status
```

A healthy installation reports `OK` from Plugin Doctor and prints one honest
status per provider. Missing credentials appear as `unavailable`; they are not
reported as fake zero quota.

## Use

### Terminal

```bash
hermes quota                 # cached status for all providers
hermes quota refresh         # refresh every provider, then show status
hermes quota grok            # one provider (short form)
hermes quota provider grok   # one provider (explicit form)
```

### Hermes chat

```text
/quota
/quota refresh
/quota grok
```

### Hermes Desktop

- The status bar shows one chip per configured provider, side by side (dot +
  label + worst remaining %). Click any chip to open the full page. Switch to
  the single "worst provider" chip via **Quota → Settings → Status bar mode**.
- Open **Quota** from the sidebar.
- Use the page settings to show unconfigured providers, change reset formatting,
  adjust polling, or enable the optional docked pane.

## Providers

| Provider | Quota source |
|---|---|
| OpenAI Codex | Hermes account usage |
| Anthropic | Hermes account usage |
| Nous | Hermes account usage |
| OpenRouter | Hermes account usage |
| Grok | Local Grok browser session → Grok billing endpoint |
| Gemini | Gemini CLI OAuth credentials → Google quota endpoint |
| Kimi | Local Kimi session credentials → Kimi usage endpoint |
| OpenCode Go | `OPENCODE_API_KEY` or local OpenCode auth file → Zen Go usage endpoint |

## OpenCode Go

Provider fetcher for [OpenCode Go](https://opencode.ai/docs/go) ($10/month
subscription; 5-hour / Weekly / Monthly usage windows). Reads the Zen usage
endpoint (`GET https://opencode.ai/zen/go/v1/usage`) with your API key — set
`OPENCODE_API_KEY`, or just run `opencode auth login` once and the key is
picked up from OpenCode's local auth file. Then `hermes quota opencode-go`.
No network auth is performed; see `quota_providers/opencode_go.py`.

## Cache and refresh behavior

Quota is stored at:

```text
$HERMES_HOME/quota_cache.json
```

When `HERMES_HOME` is unset, the default root is:

| Platform | Hermes root |
|---|---|
| Windows | `%LOCALAPPDATA%\hermes` |
| macOS/Linux with XDG | `$XDG_DATA_HOME/hermes` |
| Other macOS/Linux setups | `~/.hermes` |

The footer, CLI status command, and Desktop widget normally read this cache.
A manual `refresh` fetches all providers immediately. If the Desktop backend
receives a quota request while the cache is absent or older than 30 minutes, it
starts one deduplicated background refresh; concurrent requests do not start
extra refreshes.

## Grok on Windows

The plugin can read the signed-in Firefox profile automatically. It selects only
unexpired `grok.com` cookies from Firefox's local `cookies.sqlite`, keeps their
values in memory, and sends them only to Grok's billing endpoint.

1. Sign in to <https://grok.com> in Firefox.
2. Close Firefox so its cookie database can be copied cleanly.
3. Run:

```bash
hermes quota refresh
hermes quota grok
```

If Firefox is unavailable, copy only the `Cookie` header value from a logged-in
Grok request to the clipboard, then run from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File `
  "$env:LOCALAPPDATA\hermes\plugins\quota\scripts\import-grok-cookies.ps1"
```

The importer writes `%USERPROFILE%\grok_session.json` and never prints the
cookie.

### Optional Grok debug capture

Raw Grok billing responses are **not written by default**. For temporary parser
debugging, explicitly enable capture for one refresh:

```bash
HERMES_QUOTA_DEBUG=1 hermes quota refresh
```

This writes `%USERPROFILE%\grok_last_response.bin` using an atomic replacement and
owner-only permissions where the operating system supports POSIX permission
bits. Treat it as private account data and delete it after debugging. Never
attach cookies, session files, or raw billing responses to a public issue.

## Privacy and security

- No telemetry or analytics are added.
- Tokens and cookies are never printed.
- Credentials are sent only to the matching provider endpoint.
- Provider failures degrade to explicit `unavailable` states.
- The plugin requests no permission to override built-in Hermes tools.
- The Desktop API is mounted under Hermes' authenticated plugin namespace.
- Raw Grok responses are persisted only when `HERMES_QUOTA_DEBUG=1` is set.

This is an in-process third-party Hermes plugin. Review the source before
installing it in a sensitive environment.

## Update

From the cloned repository:

```bash
git pull --ff-only
./install.sh
hermes plugins doctor quota
hermes quota refresh
```

Then close every Hermes Desktop window and reopen the app.

For reproducible automation, check out a full trusted commit SHA before running
`./install.sh` and record that SHA in the deployment log.

## Uninstall

From the cloned repository:

```bash
./uninstall.sh
```

The uninstaller uses the same platform-aware Hermes root as the installer,
removes both plugin surfaces, removes `quota` from existing enabled and disabled
lists without changing unrelated plugins, and restores the previous state if any
step fails.

Close every Hermes Desktop window and reopen the app afterward so the Python
backend is fully unmounted.

## Troubleshooting

### `Quota backend unavailable`

1. Close every Hermes Desktop window.
2. Reopen Hermes Desktop.
3. Run:

```bash
hermes plugins doctor quota
hermes quota refresh
```

Reloading Desktop JavaScript alone is not enough.

### Grok: `no-session-cookies`

Sign in to Grok in Firefox, close Firefox, and run `hermes quota refresh`.

### Grok: `auth-failed` or `cloudflare-blocked`

The session expired or Grok rejected it. Sign in again and refresh. Do not paste
cookies into chat or an issue.

### A provider is `unavailable`

This is expected when that provider is not configured or does not expose a
supported quota endpoint. Other providers continue to work.

## Agent install contract

For an automated agent installing this repository:

1. inspect `install.sh`, `plugin.yaml`, `dashboard/plugin_api.py`,
   `desktop/plugin.js`, and `quota_providers/`;
2. record `git rev-parse HEAD`;
3. run `bash -n install.sh uninstall.sh scripts/*.sh`;
4. choose `python` or `python3`, then run `-m unittest discover -s tests -v`;
5. run `./install.sh`;
6. verify with `hermes plugins doctor quota`, `hermes quota refresh`, and
   `hermes quota status`;
7. tell the user that a complete Hermes Desktop restart is still required.

Do not claim the Desktop page is working until the app has restarted and the
backend has been mounted.

## Development

Main files:

```text
plugin.yaml                       runtime manifest
__init__.py                       CLI, slash command, optional hooks
commands.py                       command parsing and formatting
quota_cache.py                    cache orchestration
quota_providers/                  provider fetchers
dashboard/plugin_api.py           authenticated Desktop API
dashboard/manifest.json           dashboard API manifest
desktop/plugin.js                 native Desktop widget
scripts/hermes-home.sh             shared platform path resolution
scripts/hermes-config.sh           shared config transaction helpers
scripts/import-grok-cookies.ps1   manual Grok cookie fallback
```

Run all local checks:

```bash
bash -n install.sh uninstall.sh scripts/*.sh
PYTHON=
for candidate in python python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PYTHON="$candidate"; break
  fi
done
[ -n "$PYTHON" ] || { echo "Python 3.9+ is required" >&2; exit 2; }
"$PYTHON" -m py_compile \
  __init__.py commands.py quota_cache.py dashboard/plugin_api.py \
  quota_providers/*.py
"$PYTHON" -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
