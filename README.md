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

- **Desktop:** press **⌘K → "Reload desktop plugins"** (or restart Hermes
  Desktop). The quota chip appears on the status bar.
- **CLI:** `hermes quota`, `hermes quota refresh`, `hermes quota <provider>`.

To remove: `./uninstall.sh`.

> Requires a Hermes core that includes the generic `footer` and `usage_extra`
> lifecycle hooks (upstream in `rarf/hermes-agent`, branches `footer-hook` /
> `usage-extra-hook`). Without them the plugin still loads but the footer/usage
> block contributes nothing — no errors. The **desktop widget** and `/quota`
> page work independently of those hooks.

---

## Manual install (if you prefer)

```bash
# Backend + dashboard
hermes plugins install https://github.com/rarf/hermes-quota-plugin.git
hermes plugins enable quota

# Desktop widget (separate location)
mkdir -p ~/.hermes/desktop-plugins/quota
cp desktop/plugin.js ~/.hermes/desktop-plugins/quota/plugin.js
# then ⌘K → Reload desktop plugins
```

---

## Data path

```
chip → ctx.rest('/quota') → gateway → GET /api/plugins/quota/quota
     → dashboard/plugin_api.py → quota_cache.json
```

The Python backend is imported **only when** the plugin is in
`plugins.enabled` in `~/.hermes/config.yaml` **and** the dashboard
`manifest.json` declares `"api": "plugin_api.py"` (the loader mounts
`plugin_api.py`, *not* `api.py`).

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
