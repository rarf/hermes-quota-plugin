# hermes-quota-plugin

Standalone **Hermes Agent** plugin that surfaces per-provider quota / rate-limit
status in two places, with zero core special-casing:

- a **📊 quota block** appended to the runtime footer (via the generic `footer`
  lifecycle hook), and
- the same block appended to the `/usage` command (via the generic
  `usage_extra` lifecycle hook), plus a dedicated **`/quota`** slash command
  for on-demand detail.

The plugin carries its **own quota subsystem** (fetchers + cache) so it survives
`hermes update` — the only core change it needs is the two generic hooks
(`footer`, `usage_extra`), contributed upstream in
[rarf/hermes-agent](https://github.com/rarf/hermes-agent) (`footer-hook` and
`usage-extra-hook` branches).

## Install

```bash
# Clone into the user plugin dir (Hermes auto-discovers ~/.hermes/plugins/<name>/)
git clone https://github.com/rarf/hermes-quota-plugin.git \
  ~/.hermes/plugins/quota

# Enable it
hermes plugins enable quota
```

> Requires Hermes core that includes the `footer` and `usage_extra` hooks
> (PRs `footer-hook` / `usage-extra-hook`, or any build containing them).
> Without them the plugin still loads but contributes nothing — no errors.

## Usage

- **Footer**: once `runtime_footer` is enabled in `config.yaml`
  (`display.runtime_footer.enabled: true`), the final message gains a quota
  block, e.g.

  ```
  gpt-5.4 · 6% · ~
  📊 quota:
  • anthropic: Current session 54% (reset today 13:09) · Current week 44% (reset tomorrow 23:59)
  • grok: Weekly 0% (reset today 18:00)
  • openai-codex: Session 67% (reset Aug 08 04:37)
  ```

- **`/usage`**: shows the same quota block appended after the account/credits
  sections.

- **`/quota`**: full per-provider breakdown (or `/quota refresh` to force a
  re-fetch).

## How it works

Reading live quota on every final message would mean N network calls per reply
(and the footer has no live agent/credentials in scope). Instead:

- provider fetchers live in `quota_providers/` (a pluggable registry);
- `refresh_quota_cache()` runs them on a schedule (cron) and writes a small
  JSON summary to `$HERMES_HOME/quota_cache.json`;
- the footer hook and `/quota` command read that JSON — pure, offline, fast.

Each fetcher is **fail-open**: a fetch error yields an `unavailable_reason`
record (never fake zeros), so one broken provider can't abort the whole refresh.

### Supported providers

| Provider       | Source                                            |
|----------------|---------------------------------------------------|
| openai-codex   | core `account_usage` (Codex rate-limit windows)   |
| anthropic      | core `account_usage`                              |
| nous           | core `account_usage` (Nous credits)              |
| openrouter     | core `account_usage`                              |
| grok           | `grok_session.json` cookies → Grok billing API   |
| gemini         | `~/.gemini/oauth_creds.json` → Gemini CLI quota   |
| kimi           | `kimi_session.json` api key / token               |

Adding a provider = write a fetcher returning a `QuotaResult` and
`@register("provider-id")` it. No changes to the cache orchestration.

## License

MIT — see [LICENSE](LICENSE).
