# hermes-quota-plugin

Per-provider **quota / rate-limit** status for Hermes, surfaced in **three
places** with zero core special-casing:

1. **Desktop status bar** (widget) — a chip showing the *worst* provider's
   remaining % + a tonal progress bar. Hover for a button affordance; click
   opens the full `/quota` page.
2. **`/quota` desktop page** — every configured provider with per-window
   remaining % and reset breakdown.
3. **Runtime footer + `/usage` + `/quota` CLI** — a quota block appended to the
   agent's final message and usage output, plus on-demand slash commands.

All three read the plugin's **precomputed cache** (`quota_cache.json`), so they
never do network I/O in the hot path.

## See it in action

### Desktop status bar

<img width="338" height="34" alt="image" src="https://github.com/user-attachments/assets/4bbb316e-cea1-4837-9aff-fbdef45287a5" />


The widget shows the provider with the lowest remaining quota and opens the
full breakdown when clicked.

### Quota page

<img width="2326" height="443" alt="image" src="https://github.com/user-attachments/assets/57f027fe-da1d-470f-8800-1493fab69b8d" />


The page shows provider windows, remaining percentage, reset time, and cache
freshness without exposing credentials or account identifiers.

### Configuration preview

<img width="807" height="235" alt="image" src="https://github.com/user-attachments/assets/db592a97-7508-4908-9e95-26508e82b49d" />


This is a clean configuration preview of the plugin's public settings surface;
it intentionally contains no account data or private paths.

---

## Features

| Feature | Where | Notes |
|---------|-------|-------|
| Status-bar chip (worst provider + bar) | Desktop widget | Font matches other status-bar items; severity-colored. |
| Hover affordance | Desktop widget | Highlights like a button. |
| Click → `/quota` page | Desktop widget | Full provider breakdown. |
| Per-window remaining % + reset | `/quota` page, footer, CLI | `resets in 3h 12m` or absolute date. |
| Show / hide status bar | Setting | Toggle in Settings ▸ Plugins ▸ Quota. |
| Show / hide docked pane | Setting | **Off by default** (optional side pane). |
| Reset format (relative / absolute) | Setting | Countdown style on `/quota`. |
| Show unconfigured providers | Setting | Off by default. |
| Refresh interval | Setting | Poll cadence (seconds). |
| Pluggable providers | Backend | Add a fetcher, register it — no other changes. |

Everything is **configurable** from Settings ▸ Plugins ▸ Quota (no env vars,
no core edits).

### Supported providers

| Provider     | Source                                            |
|--------------|---------------------------------------------------|
| openai-codex | core `account_usage` (Codex rate-limit windows)   |
| anthropic    | core `account_usage`                              |
| nous         | core `account_usage` (Nous credits)              |
| openrouter   | core `account_usage`                              |
| grok         | `grok_session.json` cookies → Grok billing API   |
| gemini       | `~/.gemini/oauth_creds.json` → Gemini CLI quota   |
| kimi         | `kimi_session.json` api key / token               |

New providers appear **automatically** in the `/quota` page and are considered
for the "worst" chip — no widget change needed.

---

## Install (simplified — one command)

```bash
git clone https://github.com/rarf/hermes-quota-plugin.git
cd hermes-quota-plugin
./install.sh
```

`install.sh` does **both** installs for you:

1. copies the backend + dashboard into `~/.hermes/plugins/quota/`
2. copies the desktop widget into `~/.hermes/desktop-plugins/quota/plugin.js`
3. enables the backend plugin (`hermes plugins enable quota`)
4. prints the final steps

After install:

1. **Restart Hermes Desktop completely.** Close and reopen the application. This
   remounts the Python `dashboard/plugin_api.py` backend in the Desktop's
   embedded Hermes process.
2. **Do not rely only on `Reload desktop plugins`.** That command hot-reloads
   the JavaScript widget, but it does not remount a changed Python dashboard API.
3. **Bot profiles:** the installer adds `quota` to each existing profile's
   `plugins.enabled` list while preserving other enabled plugins.
4. **CLI:** `hermes quota`, `hermes quota refresh`, `hermes quota <provider>`.

If the Desktop still says **Quota backend unavailable**, close every Hermes
Desktop window, verify `hermes plugins doctor quota`, and launch Desktop again.
The expected route is `GET /api/plugins/quota/quota`; the expected plugin API
health route is `GET /api/plugins/quota/health`.
To remove: `./uninstall.sh`.

> The optional `footer` and `usage_extra` hooks are used only when the running
> Hermes core exposes them. On current builds without those hooks, the plugin
> still provides the desktop widget, `/quota` page, and CLI command without
> registering unknown hooks or producing doctor errors.

---

## Manual install (if you prefer)

```bash
# Backend + dashboard
hermes plugins install https://github.com/rarf/hermes-quota-plugin.git
hermes plugins enable quota

# Desktop widget (separate location)
mkdir -p ~/.hermes/desktop-plugins/quota
cp desktop/plugin.js ~/.hermes/desktop-plugins/quota/plugin.js
# then restart Hermes Desktop completely so the Python API is remounted
```

## Command examples

### Plugin health check

```text
$ hermes plugins doctor quota
Plugin Doctor: .../plugins/quota
  manifest: quota 1.0.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 0 hook(s)
```

### Quota status

```text
$ hermes quota status
📊 **quota** (fetched 1m ago)

• **openai-codex** · Session 45% (reset tomorrow 06:43)
• **gemini**: unavailable (consumer-tier-deprecated)
• **grok**: unavailable (no-session-cookies)
• **kimi**: unavailable (no-credentials)

_Run `/quota refresh` to force a re-fetch._
```

Unavailable providers are reported honestly; the plugin never turns missing
credentials or failed probes into a fabricated `0%` value.

---

## Data path

```
chip → ctx.rest('/quota') → gateway → GET /api/plugins/quota/quota
     → dashboard/plugin_api.py → quota_cache.json
```

The Python backend is imported **only when** the plugin is enabled by Hermes
and the dashboard `manifest.json` declares `"api": "plugin_api.py"` (the
loader mounts `plugin_api.py`, *not* `api.py`). The installer resolves the
shared Hermes root on Windows and never installs per-profile copies.

The optional `footer` and `usage_extra` hooks are registered only when the
running Hermes build exposes them. On older builds the widget, `/quota`, and
API page still work without warnings.

---

## Extending

### Add a provider

Write a fetcher returning a `QuotaResult` and `@register("provider-id")` it in
`quota_providers/`. No changes to cache orchestration or the widget.

### Change widget appearance

- Colors: the `fill` / `rclass` ternaries in `desktop/plugin.js`.
- Severity: `toneForRemaining()` (`<=15` bad, `<=40` warn).
- Font: `text-[0.6875rem]` (matches status-bar items).

### Make the click target something else

`desktop/plugin.js` calls `host.navigate('/quota')` on click — swap for any
`host.*` verb (e.g. `host.openWorkspace(...)`).

> **Plugin-runtime constraints (learned the hard way):** the status-bar item is
> loaded uncompiled as plain `jsx()` — keep it simple. `useState` and `onClick`
> work; a `flex-col` container or `absolute`-positioned popover **breaks the
> render**. Use `host.navigate` / `host.openWorkspace` for richer surfaces, and
> register the item **once** (don't re-register on every poll — that hangs the
> renderer).

---

## Repo layout

```
hermes-quota-plugin/
├── install.sh            # installs backend + desktop widget (idempotent)
├── uninstall.sh          # removes both
├── plugin.yaml           # backend plugin manifest
├── commands.py           # /quota slash + CLI commands
├── quota_cache.py        # cache orchestration
├── quota_providers/      # pluggable provider fetchers
├── dashboard/
│   ├── manifest.json     # points "api" at plugin_api.py
│   └── plugin_api.py     # FastAPI router (GET /quota, POST /refresh)
├── desktop/
│   └── plugin.js         # the desktop status-bar widget
└── SPEC.md               # original design spec
```

## License

MIT — see [LICENSE](LICENSE).
