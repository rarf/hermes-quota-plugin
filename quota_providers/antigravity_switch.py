"""Single-key enablement for the Antigravity Desktop/CLI switch.

``plugins.entries.quota.settings.antigravityEnabled`` is the one authoritative
setting and defaults to **false**:

* OFF -- the backend never resolves the Antigravity CLI (``agy``) and never
  starts a subprocess; the registered fetcher returns ``opt-in-disabled``.
* ON -- flipping the Desktop switch writes the same key through
  ``hermes_cli.plugins.save_plugin_setting`` and the next sweep runs the
  CLI-backed fetcher, with no manual ``hermes config set`` command.

There is no second hidden activation setting. The switch is a view/writer for
this key, not a separate gate.

``HERMES_QUOTA_ANTIGRAVITY_ENABLED`` keeps its existing precedence over the
config key (exactly like ``antigravity._antigravity_enabled``). When it is set,
the switch still writes the config key so the choice survives after the env
override is removed, but the effective state follows the env var. :func:`status`
reports ``env_override`` and a human-readable ``warning`` so the CLI and Desktop
can explain the conflict instead of pretending the toggle took effect.

The key is read through Hermes' ``load_config_readonly()``. That loader keeps
serving the last-known-good config (or a recent good backup) when ``config.yaml``
is malformed or temporarily unreadable, so a broken file is **not** a reliable
way to turn the switch off: the effective value follows the snapshot the
fetcher reads. Only a missing key in a readable config means off.

This module never reads a credential, starts a subprocess or makes a network
request itself. When the setting is on, the Antigravity fetcher invokes the
user's ``agy`` CLI; the CLI owns its login, credential store and network access.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .base import opt_in_flag

PROVIDER_ID = "antigravity"

#: The one authoritative plugin setting. Default false.
SETTING = "antigravityEnabled"

#: Existing environment override, kept for compatibility.
ENV = "HERMES_QUOTA_ANTIGRAVITY_ENABLED"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_ON_ACTIONS = frozenset({"on", "true", "1", "enable", "enabled"})
_OFF_ACTIONS = frozenset({"off", "false", "0", "disable", "disabled"})


class SwitchError(RuntimeError):
    """The switch cannot be read or written as requested."""


def _settings() -> dict[str, Any]:
    """Return the quota plugin's settings dict, or ``{}`` when unavailable.

    A missing Hermes core or a loader exception yields ``{}`` (the off
    default), but a malformed or temporarily unreadable ``config.yaml`` is not
    necessarily an exception: Hermes may serve its last-known-good snapshot
    instead. This helper follows whatever ``load_config_readonly()`` returns,
    exactly like the fetcher, rather than assuming a broken file means off.
    Never raises: this may run inside the status polling path.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:  # noqa: BLE001 - never escape the status polling path
        return {}
    if not isinstance(config, dict):
        return {}
    plugins = config.get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get("quota") if isinstance(entries, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    return settings if isinstance(settings, dict) else {}


def _env_flag() -> Optional[bool]:
    """Explicit value of the compatibility env var, or None when unset/invalid.

    Invalid values are treated as unset, matching
    ``antigravity._antigravity_enabled``: an unrecognised value must neither
    enable a backend that runs another application's CLI nor mask a valid
    config value.
    """
    raw = os.environ.get(ENV)
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def _config_state(settings: Optional[dict[str, Any]] = None) -> tuple[Optional[bool], bool]:
    """``(value, explicitly_set)`` for the one setting; absent means off."""
    if settings is None:
        settings = _settings()
    if SETTING not in settings:
        return None, False
    return opt_in_flag(settings.get(SETTING)), True


def _effective(settings: Optional[dict[str, Any]] = None) -> bool:
    """Effective state, with the env var taking precedence over config.

    Same precedence and parser as ``antigravity._antigravity_enabled``. Kept
    local so importing this module cannot pull in the CLI-backed fetcher; the
    two parsers must stay in lockstep, and
    ``test_status_matches_the_fetcher_gate`` is the drift tripwire until they
    share one helper.
    """
    env_value = _env_flag()
    if env_value is not None:
        return env_value
    value, explicit = _config_state(settings)
    return bool(value) if explicit else False


def enabled() -> bool:
    """Effective state of the one setting."""
    return _effective()


def status() -> dict[str, Any]:
    """Read-only switch state for ``quota status --json`` and the CLI.

    No credential material and no CLI execution. ``warning`` is non-None only
    when the env override is set, including when it agrees with config, because
    a subsequent switch flip cannot change the effective state while it is
    present.
    """
    settings = _settings()
    env_value = _env_flag()
    config_value, config_explicit = _config_state(settings)
    effective = _effective(settings)
    source = "env" if env_value is not None else ("config" if config_explicit else "default")
    warning = None
    if env_value is not None:
        warning = (
            f"{ENV} overrides the Antigravity switch"
            f" (currently {'on' if env_value else 'off'})"
        )
        if config_explicit and config_value != env_value:
            warning += (
                f"; the config key says {'on' if config_value else 'off'} "
                "but the environment wins until it is unset"
            )
    return {
        "provider": PROVIDER_ID,
        "setting": SETTING,
        "enabled": effective,
        "config": config_value if config_explicit else None,
        "config_explicit": config_explicit,
        "env": env_value,
        "env_override": env_value is not None,
        "source": source,
        "warning": warning,
    }


def switch_payload() -> dict[str, Any]:
    """The ``provider_switches`` root object for ``quota status --json``."""
    return {PROVIDER_ID: status()}


def set_switch(provider_id: str, enabled_value: bool) -> dict[str, Any]:
    """Write the one authoritative setting through Hermes' official writer.

    ``enabled_value`` must be a real ``bool``. Values that merely look
    boolean-ish are rejected instead of coerced: ``bool("false")`` is ``True``,
    so accepting strings here could persistently enable a backend that runs
    another application's CLI when the caller meant to disable it. The CLI maps
    its ``on``/``off`` action words to real booleans before calling this function.

    Raises :class:`SwitchError` when the provider is unknown, the value is not a
    bool, or Hermes refuses the write (managed install, administrator-managed
    key, unreadable config). The Desktop keeps its previous state when that
    happens.
    """
    if provider_id != PROVIDER_ID:
        raise SwitchError(f"provider {provider_id!r} has no polling switch")
    if not isinstance(enabled_value, bool):
        raise SwitchError(
            "the Antigravity switch needs a real bool (True or False), "
            f"not {type(enabled_value).__name__}"
        )
    try:
        from hermes_cli.plugins import save_plugin_setting
    except Exception as exc:  # noqa: BLE001 - missing/older Hermes core
        raise SwitchError(
            "Hermes core is unavailable; the Antigravity switch cannot be saved"
        ) from exc
    try:
        # Same writer the plugin SDK (`ctx.set_config`) and the Desktop's
        # plugin-settings action use: profile-aware, locked, and managed-key
        # aware. It does not print to stdout, so the CLI's JSON stays clean.
        save_plugin_setting("quota", (SETTING,), enabled_value)
    except Exception as exc:  # noqa: BLE001 - surface a stable CLI error
        raise SwitchError(f"could not save the Antigravity switch: {exc}") from exc
    return status()


def switch_command(provider_id: str, action: str) -> dict[str, Any]:
    """``hermes quota switch <provider> [on|off|status] --json`` payload."""
    action = str(action or "status").strip().lower()
    if provider_id != PROVIDER_ID:
        raise SwitchError(f"provider {provider_id!r} has no polling switch")
    if action == "status":
        return {"ok": True, **status()}
    if action in _ON_ACTIONS:
        return {"ok": True, **set_switch(provider_id, True)}
    if action in _OFF_ACTIONS:
        return {"ok": True, **set_switch(provider_id, False)}
    raise SwitchError(f"unknown switch state {action!r}; use on, off or status")
