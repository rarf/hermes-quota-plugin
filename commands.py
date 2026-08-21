"""Slash + CLI command handlers for the quota plugin.

Mirrors the Hermes plugin convention (see the lifeos plugin): a synchronous
``quota_command(raw_args) -> str`` for the in-session ``/quota`` slash command,
and an argparse-backed ``hermes quota`` CLI command via ``register_cli_command``.

Subcommands (slash):
    /quota              → per-provider quota status (refreshes if cache is stale)
    /quota refresh      → force a re-fetch of every provider, then show status
    /quota <provider>   → status for a single provider (e.g. /quota grok)
    /quota help         → usage

CLI:
    hermes quota              → status
    hermes quota refresh      → force re-fetch
    hermes quota provider X   → single provider
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .quota_cache import read_quota_cache, quota_cache_age_seconds, refresh_quota_cache, MAX_AGE_S
from .quota_providers import PROVIDER_FETCHERS

_PROVIDER_IDS = tuple(PROVIDER_FETCHERS)

QUOTA_HELP = (
    "**/quota** — per-provider quota / rate-limit status\n"
    "\n"
    "• `/quota` — show all providers (auto-refreshes if the cache is stale)\n"
    "• `/quota refresh` — force a re-fetch of every provider\n"
    "• `/quota <provider>` — e.g. `/quota grok`, `/quota openai-codex`\n"
    "• `/quota help` — this message\n"
    "\n"
    "Also available as `hermes quota` on the CLI."
)


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


def _age_label() -> str:
    age = quota_cache_age_seconds()
    if age is None:
        return "never fetched"
    if age <= MAX_AGE_S:
        return f"fetched {int(age // 60)}m ago"
    return f"stale ({int(age // 60)}m old)"


def _render_quota(provider_filter: Optional[str]) -> str:
    """Render the per-provider quota breakdown (optionally filtered)."""
    cache = read_quota_cache()
    providers = cache.get("providers") or {}
    if not providers:
        return "📊 quota: no providers configured / no data fetched yet.\n" \
               "Run `/quota refresh` (or `hermes quota refresh`) to populate it."

    pf = (provider_filter or "").strip().lower()
    lines = [f"📊 **quota** ({_age_label()})", ""]
    shown = 0
    for name, rec in providers.items():
        if pf and pf not in (name.lower(), (rec.get("label") or "").lower()):
            continue
        shown += 1
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

    if pf and shown == 0:
        return f"📊 quota: no provider matching '{provider_filter}'."

    lines.append("")
    lines.append("_Run `/quota refresh` to force a re-fetch._")
    return "\n".join(lines)


def _render_quota_json() -> str:
    """Render the full quota cache as JSON for the desktop widget."""
    cache = read_quota_cache()
    # Ensure consistent shape for widget consumption
    providers = cache.get("providers") or {}
    out = {
        "fetched_at": cache.get("fetched_at"),
        "providers": providers,
    }
    return json.dumps(out, indent=2, sort_keys=True)


def quota_command(raw_args: str) -> str:
    """In-session ``/quota`` slash command handler.

    Signature matches Hermes ``register_command``: ``fn(raw_args: str) -> str``.
    """
    raw = (raw_args or "").strip()
    parts = raw.split()
    sub = parts[0].lower() if parts else ""

    if sub in ("help", "-h", "--help", "?"):
        return QUOTA_HELP
    if sub in ("refresh", "--refresh", "-r", "reload"):
        try:
            refresh_quota_cache()
        except Exception as e:  # pragma: no cover - defensive
            return f"⚠️ quota refresh failed: {e}"
        return _render_quota(None)
    if sub:
        # Anything else is treated as a provider filter.
        return _render_quota(sub)
    # Bare /quota: refresh if stale, then show.
    if (quota_cache_age_seconds() or 10**9) > MAX_AGE_S:
        try:
            refresh_quota_cache()
        except Exception:
            pass
    return _render_quota(None)


# -- CLI command (hermes quota ...) -----------------------------------------

def setup_argparse(subparser):
    subs = subparser.add_subparsers(dest="quota_command")
    status_p = subs.add_parser("status", help="Show per-provider quota (default)")
    status_p.add_argument("--json", action="store_true", help="Emit raw JSON for the desktop widget")
    status_p.add_argument(
        "--version-json",
        dest="version_json",
        action="store_true",
        help="Emit the installed build stamp (update self-check)",
    )
    status_p.add_argument(
        "--max-age",
        dest="max_age",
        type=int,
        default=None,
        help="Refresh automatically when the cache is older than N seconds",
    )
    subs.add_parser("refresh", help="Force a re-fetch of all providers")
    prov = subs.add_parser("provider", help="Show quota for one provider")
    prov.add_argument("name", help="provider id, e.g. anthropic, grok, openai-codex")
    for provider_id in _PROVIDER_IDS:
        subs.add_parser(provider_id, help=f"Show quota for {provider_id}")
    subparser.set_defaults(func=_handle_cli)


def _handle_cli(args):
    cmd = getattr(args, "quota_command", None) or "status"
    if cmd == "refresh":
        refresh_quota_cache()
        print(_render_quota(None))
        return
    if cmd == "provider":
        print(_render_quota(getattr(args, "name", None)))
        return
    if cmd in _PROVIDER_IDS:
        print(_render_quota(cmd))
        return
    if cmd == "status" and getattr(args, "json", False):
        # Widget path: honor its poll cadence — refresh when the cache is
        # older than the requested max age (defaults to MAX_AGE_S).
        max_age = getattr(args, "max_age", None)
        if max_age is None or max_age <= 0:
            max_age = MAX_AGE_S
        if (quota_cache_age_seconds() or 10**9) > max_age:
            try:
                refresh_quota_cache()
            except Exception:
                pass
        print(_render_quota_json())
        return
    if cmd == "status" and getattr(args, "version_json", False):
        # Update self-check: emit the installed build stamp so the widget can
        # compare it against GitHub master without any fs access.
        stamp_path = Path(__file__).resolve().parent / "version.json"
        try:
            print(stamp_path.read_text(encoding="utf-8"))
        except OSError:
            print('{"installed_sha": null}')
        return
    print(_render_quota(None))
