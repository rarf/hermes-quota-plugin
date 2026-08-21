# Hermes Quota

Status-bar quota indicator, a native **Quota** page in Hermes Desktop, `hermes quota`
in the terminal, and `/quota` inside chats. Reads a precomputed local cache — the
UI never does network I/O, and quota checks never piggyback on agent responses.

## What you get

- **Status bar:** worst provider + bar, or all providers side by side. Toggle in Settings.
- **Quota page & sidebar:** per-window remaining % and reset time for every provider.
- **Cherry-pick providers:** in Quota Settings, click a provider to hide it. Your
  choice is local and persists. Want to drop OpenRouter? One click.
- **Slash + CLI:** `/quota`, `/quota refresh`, `/quota <provider>`; and
  `hermes quota`, `hermes quota refresh`, `hermes quota provider <name>`.

## Supported providers

anthropic, openai-codex, nous, openrouter, gemini, kimi (CLI or Coding Plan key),
plus the API-key providers the fetchers already read from Hermes env (see below).
Each fetcher is fail-open: a broken provider shows `unavailable (<reason>)` and
never blocks the rest.

## Grok is opt-in

Grok is the only provider we read from your browser (Firefox `grok.com` cookies) —
there is no clean API path right now. It is **disabled by default**. Turn it on:

```bash
export HERMES_QUOTA_GROK_ENABLED=1   # or: hermes config set plugins.entries.quota.settings.grokEnabled true
hermes quota refresh
```

When off, Grok simply reports `opt-in-disabled`. No cookies are read, no files written.

## Install

```bash
git clone https://github.com/rarf/hermes-quota-plugin.git
cd hermes-quota-plugin
./install.sh
```

One global backend + widget, enabled for every profile without touching other
plugins. Re-run anytime to update; `./uninstall.sh` removes it symmetrically.

> **Restart Hermes Desktop completely** after install/update. Reloading plugins
> refreshes the widget only — the Python backend mounts at process start.

Verify:

```bash
hermes plugins doctor quota && hermes quota refresh && hermes quota status
```

## Where the numbers come from

Providers with a CLI or OAuth login (anthropic, openai-codex, gemini, kimi CLI)
are read locally. API-key providers read `DEEPSEEK_API_KEY`, `OPENCODE_GO_API_KEY`,
`OLLAMA_API_KEY`, `MINIMAX_API_KEY`, `NOVITA_API_KEY`, `DEEPINFRA_API_KEY`,
`AI_GATEWAY_API_KEY`, `ZAI_API_KEY`/`GLM_API_KEY` from Hermes env. Refresh runs the
fetchers and writes `$HERMES_HOME/quota_cache.json`; the UI reads that file.

## Privacy & safety

- No telemetry. Cookies and tokens are never printed.
- Grok cookies are used only for the Grok billing request, and only when you opt in.
- Missing credentials produce an explicit `unavailable` state — no fake zeros.
- The plugin does not request permission to override built-in Hermes tools.

## Development

```bash
bash -n install.sh uninstall.sh
python -m py_compile __init__.py commands.py quota_cache.py quota_providers/*.py
hermes plugins doctor quota
hermes quota refresh
```

To add a provider, write a fetcher in `quota_providers/` that returns a
`QuotaResult` and register it. The cache and widget need no provider-specific changes.

MIT.