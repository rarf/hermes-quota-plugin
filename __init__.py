"""Quota plugin — per-provider quota footer block + /quota command.

Registers:
  * a ``footer`` lifecycle hook that contributes the 📊 quota block to the
    runtime footer (reads the precomputed quota_cache.json so the footer never
    does network I/O);
  * a ``usage_extra`` lifecycle hook that appends the same quota block to the
    /usage command output;
  * a ``/quota`` slash command for on-demand detail (triggers a refresh when
    the cache is stale).

The quota subsystem (fetchers + cache) lives entirely inside this plugin, so it
survives ``hermes update`` — the only core change is the generic ``footer`` and
``usage_extra`` hooks (see the upstream PRs).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from .quota_cache import read_quota_cache, quota_cache_age_seconds, refresh_quota_cache

_CACHE_MAX_AGE_S = 60 * 30


def _short_reset(reset_iso: Optional[str]) -> str:
    """Render an ISO reset timestamp as a short local 'reset <when>' string."""
    if not reset_iso:
        return ""
    try:
        dt = datetime.fromisoformat(reset_iso)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    now = datetime.now()
    delta = (local.date() - now.date()).days
    if delta == 0:
        day = "today"
    elif delta == 1:
        day = "tomorrow"
    else:
        day = local.strftime("%b %d")
    return f"{day} {local.strftime('%H:%M')}"


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
            tail = _short_reset(w.get("reset_at"))
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

    Returns the 📊 block (or '' when no fresh data), so the footer can append it.
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
    (matching the contract other `usage_extra` hooks follow).
    """
    try:
        if (quota_cache_age_seconds() or 10**9) <= _CACHE_MAX_AGE_S:
            block = _format_quota_block(read_quota_cache())
            if block:
                return block
    except Exception:
        pass
    return None


# -- /quota command ---------------------------------------------------------
def quota_command(raw_args: str) -> str:
    """/quota — show per-provider quota; refresh when the cache is stale."""
    args = (raw_args or "").strip().lower()
    if args in ("refresh", "--refresh", "-r"):
        try:
            cache = refresh_quota_cache()
        except Exception as e:  # pragma: no cover - defensive
            return f"quota refresh failed: {e}"
    else:
        if (quota_cache_age_seconds() or 10**9) > _CACHE_MAX_AGE_S:
            try:
                refresh_quota_cache()
            except Exception:
                pass
        cache = read_quota_cache()

    providers = cache.get("providers") or {}
    fetched = cache.get("fetched_at") or "unknown"
    if not providers:
        return "📊 quota: no providers configured / no data fetched."

    lines = [f"📊 **quota** (fetched {fetched})", ""]
    for name, rec in providers.items():
        if not isinstance(rec, dict):
            continue
        label = rec.get("label") or name
        reason = rec.get("unavailable_reason")
        if reason:
            lines.append(f"• **{label}**: unavailable ({reason})")
            continue
        windows = rec.get("windows") or []
        if not windows:
            lines.append(f"• **{label}**: no window data")
            continue
        for w in windows:
            wlabel = w.get("label") or "window"
            used = w.get("used_percent")
            tail = _short_reset(w.get("reset_at"))
            if used is None:
                lines.append(f"• **{label}** · {wlabel}" + (f" (reset {tail})" if tail else ""))
            else:
                try:
                    rem = max(0, min(100, round(100 - float(used))))
                except (TypeError, ValueError):
                    rem = "?"
                lines.append(
                    f"• **{label}** · {wlabel} {rem}%" + (f" (reset {tail})" if tail else "")
                )
    lines.append("")
    lines.append("_Run `/quota refresh` to force a re-fetch._")
    return "\n".join(lines)


def register(ctx) -> None:
    """Register the quota plugin's hooks and command."""
    ctx.register_hook("footer", footer_segment)
    ctx.register_hook("usage_extra", usage_extra)
    ctx.register_command("quota", quota_command, description="Show per-provider quota / rate-limit status")
