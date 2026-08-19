"""Quota plugin — per-provider quota footer block + /quota command.

Registers:
  * a ``footer`` lifecycle hook that contributes the 📊 quota block to the
    runtime footer (reads the precomputed quota_cache.json so the footer never
    does network I/O);
  * a ``usage_extra`` lifecycle hook that appends the same quota block to the
    /usage command output;
  * a ``/quota`` slash command for on-demand detail (triggers a refresh when
    the cache is stale), plus a ``hermes quota`` CLI command.

The quota subsystem (fetchers + cache) lives entirely inside this plugin, so it
survives ``hermes update`` — the only core change is the generic ``footer`` and
``usage_extra`` hooks (see the upstream PRs footer-hook / usage-extra-hook).
"""

from __future__ import annotations

from typing import Any, Optional

from .quota_cache import read_quota_cache, quota_cache_age_seconds
from . import commands

_CACHE_MAX_AGE_S = 60 * 30


def _format_quota_block(quota_cache: dict[str, Any]) -> str:
    """Render the per-provider quota block, or '' when no data."""
    if not quota_cache:
        return ""
    providers = quota_cache.get("providers") or {}
    if not providers:
        return ""
    segs: list[str] = ["📊 quota:"]
    for name, rec in providers.items():
        if not isinstance(rec, dict):
            continue
        label = rec.get("label") or name
        windows = rec.get("windows") or []
        dated = [w for w in windows if w.get("reset_at")]
        if not dated:
            continue
        win_strs: list[str] = []
        for w in windows:
            wlabel = w.get("label") or "window"
            used = w.get("used_percent")
            tail = commands._short_reset(w.get("reset_at"))
            if used is None:
                win_strs.append(f"{wlabel}" + (f" (reset {tail})" if tail else ""))
                continue
            try:
                rem = str(max(0, min(100, round(100 - float(used)))))
            except (TypeError, ValueError):
                rem = "?"
            win_strs.append(f"{wlabel} {rem}%" + (f" (reset {tail})" if tail else ""))
        segs.append(f"• {label}: " + " · ".join(win_strs))
    return "\n".join(segs)


# -- footer hook ------------------------------------------------------------
def footer_segment(**kwargs: Any) -> Optional[str]:
    """Contribute the quota block to the runtime footer.

    Returns the 📊 block (or None when no fresh data), so the footer can append
    it after the built-in model / context / cwd fields.
    """
    try:
        if (quota_cache_age_seconds() or 10**9) <= _CACHE_MAX_AGE_S:
            return _format_quota_block(read_quota_cache())
    except Exception:
        pass
    return None


# -- usage_extra hook -------------------------------------------------------
def usage_extra(**kwargs: Any) -> Optional[str]:
    """Contribute the quota block to /usage output as a single text block.

    The gateway's /usage handler splits the returned string on newlines and
    appends each non-empty line, so we return the whole block as one string
    (matching the contract other ``usage_extra`` hooks follow).
    """
    try:
        if (quota_cache_age_seconds() or 10**9) <= _CACHE_MAX_AGE_S:
            block = _format_quota_block(read_quota_cache())
            if block:
                return block
    except Exception:
        pass
    return None


def _supports_hook(ctx: Any, hook_name: str) -> bool:
    """Return whether this Hermes runtime supports a lifecycle hook.

    Quota can run on older Hermes builds that do not yet expose the optional
    footer hooks. Do not register unknown hooks: Hermes keeps them for forward
    compatibility, but the plugin doctor quite correctly reports them as
    errors. The desktop widget and /quota command do not depend on these hooks.
    """
    try:
        from hermes_cli.plugins import VALID_HOOKS
        return hook_name in VALID_HOOKS
    except Exception:
        return False


def register(ctx) -> None:
    """Register supported quota surfaces without breaking older Hermes builds."""
    if _supports_hook(ctx, "footer"):
        ctx.register_hook("footer", footer_segment)
    if _supports_hook(ctx, "usage_extra"):
        ctx.register_hook("usage_extra", usage_extra)
    ctx.register_command(
        "quota",
        handler=commands.quota_command,
        description="Show per-provider quota / rate-limit status",
        args_hint="[refresh|<provider>|help]",
    )
    ctx.register_cli_command(
        name="quota",
        help="Show per-provider quota / rate-limit status",
        setup_fn=commands.setup_argparse,
        handler_fn=commands._handle_cli,
    )
