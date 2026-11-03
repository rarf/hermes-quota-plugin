# Antigravity switch

Antigravity is the one provider whose Desktop visibility control is also a real
backend gate. Its quota is read only through the Antigravity CLI (`agy`), which
owns its own login and network access. There is exactly **one** setting:

```yaml
plugins:
  entries:
    quota:
      settings:
        antigravityEnabled: false   # default
```

The Antigravity pill in **Enabled providers** and `hermes quota switch` read and
write this same key. No second hidden activation setting exists.

## What OFF and ON mean

| Switch | Backend behavior |
| --- | --- |
| **Off** (default) | The registered Antigravity fetcher returns `opt-in-disabled` before it even looks for `agy`: no subprocess starts and no network request is made. Background sweeps and manual refreshes are both covered. |
| **On** | The next sweep runs `agy --version` (must be 1.1.11 or newer) and then `agy -p /usage --output-format json`. The CLI performs its own login, credential handling and network access; the plugin only parses the structured JSON. Turning the pill on fires an immediate background refresh, so no manual `hermes config set` command is required; from the CLI, the next scheduled or manual `hermes quota refresh` uses the new value. |

The switch is profile-scoped. It writes to the focused profile's
`config.yaml`, so `hermes --profile work quota switch antigravity on` and the
Desktop's focused-profile route affect only that profile.

## What ON runs

Antigravity quota comes only from the Antigravity CLI:

```bash
agy --version
agy -p /usage --output-format json
```

`agy --version` must report **1.1.11 or newer** — the first release whose
print-mode slash commands return a structured payload under
`--output-format json` instead of literal prompt text. The check is strict: a
missing, unparseable or older version is reported as unavailable, never assumed
to work.

The plugin does **not** read Antigravity credential files or an OS keyring,
refresh an OAuth token, impersonate the Antigravity client, or call Google's
quota API. The CLI owns its login, credential store and network access. The
plugin also withholds ambient credentials (`GEMINI_API_KEY`,
`GOOGLE_APPLICATION_CREDENTIALS`, `AGY_ADC_AUTH`, ...) from the child process and
sets `AGY_CLI_DISABLE_AUTO_UPDATE=true` so polling cannot mutate the CLI.

Only the structured `command.data.groups` JSON is read; the human-readable
`response` text is deliberately not scraped. `remaining_fraction` is *remaining*
on a 0–1 scale, so it is converted to `used = (1 - fraction) * 100`. A bucket
with a reset time but no fraction is shown with an unknown percentage — never as
exhausted.

**Argon is conditional.** `gemini-argon-wtu-weekly` is a separate Gemini 4 Argon
weekly pool. It appears only when the CLI actually returns that bucket: it is
never derived from, or duplicated with, the main Gemini pools. An account
without that pool reports only the buckets the CLI returned — typically four
windows, never a synthetic fifth.

Each CLI boundary can fail closed with its own reason: `agy-missing`,
`agy-unsafe-path`, `agy-version-unknown`, `agy-too-old`, `agy-error`,
`agy-refused`, `agy-invalid-json`, `agy-bad-json`, `agy-output-too-large`,
`timeout` or `no-data`. None of these is rendered as zero usage.

## Desktop

Settings ▸ **Enabled providers** lists Antigravity as one pill alongside the
other providers. That pill is the one switch; there is no separate Antigravity
card or settings section. It is off by default.

The other pills in that group are client-local visibility toggles. Antigravity's
is not: it reads and writes `antigravityEnabled`, so it is never stored in the
local hide list. Turning it on also clears any stale local Antigravity hide flag
left by an older widget build, and each flip is followed by a background
refresh.

## CLI

```bash
hermes quota switch antigravity status --json
hermes quota switch antigravity on
hermes quota switch antigravity off
```

Every invocation that reaches the command handler emits exactly one JSON object
on stdout; `--json` is accepted for consistency with the other `quota`
subcommands but does not change the output. On success the object has
`ok: true` plus the same fields as `status`. If the write is refused, the exit
code is non-zero and stdout is `{"ok": false, "provider": ..., "error": ...}`.
Argument-parser failures happen before the handler: an unknown state word or a
missing provider is a usage error on stderr with a non-zero exit, not JSON.

The stored value is always a real boolean: the `on`/`off` words are action
names, and there is no CLI path that passes a truthy string to the writer.

## Environment override

`HERMES_QUOTA_ANTIGRAVITY_ENABLED` keeps its existing precedence over the config
key, exactly as before:

```bash
HERMES_QUOTA_ANTIGRAVITY_ENABLED=0   # force off
HERMES_QUOTA_ANTIGRAVITY_ENABLED=1   # force on
```

While the variable is set to a recognised value, it wins over both the config
key and the switch. `quota switch ... on|off` still writes the config key so the
choice survives after the variable is removed, but the effective state does not
change. Status output explains this:

- `env_override: true`
- `env: true|false`
- `source: "env"`
- `warning: "HERMES_QUOTA_ANTIGRAVITY_ENABLED overrides ..."` including the
  config value when the two disagree

The Antigravity pill in **Enabled providers** is disabled while the env override
is active, and its hint names the variable and current value. Unset the variable
to use the switch.

An unrecognised env value (for example `maybe`) is treated as unset, matching the
fetcher: it neither enables the CLI path nor masks a valid config value.

## Existing users

- No `antigravityEnabled` key in a readable config: off. This is the default;
  `agy` is never invoked.
- Explicit `antigravityEnabled: true`: stays on.
- Explicit `antigravityEnabled: false`: stays off.
- Explicit env opt-in: stays on until the env var is unset; the switch reflects
  the env-effective state.

See [Configuration reads and malformed files](#configuration-reads-and-malformed-files)
for why a broken file is not the same as a missing key.

## Configuration reads and malformed files

`antigravityEnabled` is read through Hermes' `load_config_readonly()` by both the
switch and the fetcher. That loader is not a syntax validator: when
`config.yaml` is malformed or temporarily unreadable, Hermes keeps serving the
last-known-good config (in process) or the newest byte-exact good backup, and
falls back to defaults only when neither exists. The switch follows whatever
snapshot the loader returns.

A broken `config.yaml` is therefore **not** a reliable way to turn Antigravity
off. If the last good config had `antigravityEnabled: true`, polling stays on.
In a readable config, an absent key means off; to actually disable polling, set
the key to `false` and unset `HERMES_QUOTA_ANTIGRAVITY_ENABLED`.

## Switch write failures

The write uses Hermes' official plugin-settings writer
(`save_plugin_setting`), so it inherits the profile path, cross-process lock,
managed-install refusal and administrator-managed-key refusal. It also validates
its input as a real boolean before writing. If the write is refused or the
config cannot be read, the CLI returns `ok: false` with a stable error and the
Desktop flips the switch back. Refusals happen before the file is replaced; if a
write fails after the atomic replace, run `status` before retrying rather than
assuming the old value is still stored.
