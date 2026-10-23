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
    hermes quota monitor ...  → Agent Monitor views as JSON (snapshot, history,
                                recommend, budget, set-budget, accounts,
                                set-priority, set-strategy; see docs/monitor.md)
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .quota_cache import (
    iter_account_records,
    local_tz,
    read_quota_cache,
    quota_cache_age_seconds,
    refresh_quota_cache,
    timezone_name,
    MAX_AGE_S,
)
from .quota_providers import PROVIDER_FETCHERS
from .codex_special_reset import normalize_entry as normalize_special_reset

_PROVIDER_IDS = tuple(PROVIDER_FETCHERS)

# Friendly provider names. Keep this table in step with the Desktop widget's
# PROVIDER_META (desktop/plugin.js) and the TUI panel's PROVIDER_NAMES
# (tui/quota-panel.mjs): one provider must not read differently per surface.
_PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai-codex": "OpenAI Codex",
    "nous": "Nous Portal",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "experientiallabs": "Experiential Labs",
    "gemini": "Google Gemini",
    "kimi": "Kimi / Moonshot",
    "grok": "xAI Grok",
    "opencode-go": "OpenCode Go",
    "copilot": "Copilot",
    "zai": "Z.ai",
    "commandcode": "CommandCode",
    "cursor": "Cursor",
    "antigravity": "Antigravity",
    "minimax": "MiniMax",
    "ollama": "Ollama",
}

# Words a user actually types for a provider. Only used to resolve a
# ``/quota <name>`` filter; the stored ids and cache keys never change.
_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "chatgpt": "openai-codex",
    "codex": "openai-codex",
    "openai": "openai-codex",
    "google": "gemini",
    "moonshot": "kimi",
    "xai": "grok",
    "opencode": "opencode-go",
}

# A row with no credential is a provider this machine has never configured: it
# has no quota to read, so the dashboard hides it. Every other unavailable
# reason means something IS configured and failing, which must stay visible.
_HIDDEN_REASON = "no-credentials"

_BAR_WIDTH = 10
_BAR_FILLED = "█"
_BAR_EMPTY = "░"


QUOTA_HELP = (
    "**/quota** — per-provider quota / rate-limit status\n"
    "\n"
    "• `/quota` — dashboard of providers with a saved account (auto-refreshes if stale)\n"
    "• `/quota refresh` — force a re-fetch of every provider\n"
    "• `/quota <provider>` — e.g. `/quota grok`, `/quota openai codex`, `/quota conta 2`\n"
    "• `/quota help` — this message\n"
    "\n"
    "Providers without a saved account are hidden; naming one still explains why.\n"
    "Also available as `hermes quota` on the CLI."
)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; a trailing ``Z`` must work on Python 3.9."""
    text = str(value).strip() if value else ""
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _short_reset(reset_iso: Optional[str]) -> str:
    """Render an ISO reset as a short local '<day> <time>' string."""
    dt = _parse_iso(reset_iso)
    if dt is None:
        return ""
    local = dt.astimezone(local_tz())
    now = datetime.now(local_tz())
    delta = (local.date() - now.date()).days
    if delta == 0:
        day = "today"
    elif delta == 1:
        day = "tomorrow"
    else:
        day = local.strftime("%b %d")
    return f"{day} {local.strftime('%H:%M')}"


def _reset_tail(reset_iso: Optional[str]) -> str:
    """' · resets in 1h 17m' — relative while it is still easy to picture."""
    dt = _parse_iso(reset_iso)
    if dt is None:
        return ""
    seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return " · reset due"
    minutes = int(seconds // 60) + (1 if seconds % 60 else 0)
    if minutes < 60:
        return f" · resets in {minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f" · resets in {hours}h {mins}m" if mins else f" · resets in {hours}h"
    days, rem_hours = divmod(hours, 24)
    if days < 7:
        return f" · resets in {days}d {rem_hours}h" if rem_hours else f" · resets in {days}d"
    local = dt.astimezone(local_tz())
    return f" · resets {local.strftime('%b %d %H:%M')}"


def _age_label() -> str:
    age = quota_cache_age_seconds()
    if age is None:
        return "never fetched"
    if age <= MAX_AGE_S:
        return f"updated {int(age // 60)}m ago"
    return f"stale ({int(age // 60)}m old)"


def _base_provider(name: str) -> str:
    """``anthropic:account2`` -> ``anthropic`` (the provider, not the row)."""
    return str(name).split(":", 1)[0]


def _provider_label(name: str, rec: Any) -> str:
    """Friendly provider name plus the account, when the row is an account."""
    base = _base_provider(name)
    pretty = _PROVIDER_NAMES.get(base)
    if not pretty:
        raw = rec.get("label") if isinstance(rec, dict) else None
        pretty = str(raw or base).split(" · ", 1)[0].strip() or base
    account = ""
    if isinstance(rec, dict):
        value = rec.get("account_label")
        if isinstance(value, str) and value.strip():
            account = value.strip()
        else:
            # Account rows store "<provider> · <account>" as their label; the
            # Desktop and TUI split it the same way.
            label = rec.get("label")
            if isinstance(label, str) and " · " in label:
                account = label.split(" · ", 1)[1].strip()
    if not account and ":" in str(name):
        account = str(name).split(":", 1)[1].strip()
    return f"{pretty} · {account}" if account else pretty


def _matches_filter(pf: str, name: str, rec: Any) -> bool:
    """Whether a ``/quota <filter>`` matches this provider row.

    A row matches on its cache key, its friendly name, its base provider and —
    for an account row — its account label, so ``/quota anthropic`` lists the
    primary login and every configured Claude account together.
    """
    target = _PROVIDER_ALIASES.get(pf, pf)
    candidates = {str(name).lower(), _base_provider(name).lower()}
    friendly = _PROVIDER_NAMES.get(_base_provider(name))
    if friendly:
        candidates.add(friendly.lower())
    if isinstance(rec, dict):
        for key in ("label", "provider", "account_label"):
            value = rec.get(key)
            if isinstance(value, str) and value.strip():
                candidates.add(value.strip().lower())
    # Match what the dashboard actually shows: a row without an account_label
    # still displays the account from its label or its "<provider>:<id>" key.
    full_label = _provider_label(name, rec).lower()
    candidates.add(full_label)
    if " · " in full_label:
        candidates.add(full_label.split(" · ", 1)[1].strip())
    return target in candidates


def _remaining(used_percent: Any) -> Optional[int]:
    """Provider-reported used % -> remaining %, clamped and honest.

    A non-finite value (NaN/Infinity) is not a quota reading: report unknown
    rather than clamping it to a comfortable-looking 100% or 0%.
    """
    if used_percent is None:
        return None
    try:
        value = 100.0 - float(used_percent)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(100, int(round(value))))


def _bar(remaining: int) -> str:
    """A 10-cell bar filled in proportion to what is LEFT."""
    filled = int(remaining * _BAR_WIDTH / 100.0 + 0.5)
    filled = max(0, min(_BAR_WIDTH, filled))
    return _BAR_FILLED * filled + _BAR_EMPTY * (_BAR_WIDTH - filled)


def _worst_remaining(rec: Any) -> Optional[int]:
    """The lowest remaining % across a row's windows (its worst meter)."""
    if not isinstance(rec, dict):
        return None
    values = [
        _remaining(w.get("used_percent"))
        for w in (rec.get("windows") or [])
        if isinstance(w, dict)
    ]
    known = [v for v in values if v is not None]
    return min(known) if known else None


def _special_reset_line(cache: dict[str, Any]) -> str:
    """One-line Codex special-reset announcement, or '' when there is none.

    Announcement metadata only: never an account allowance and never a
    per-account reset time. The validator lives in ``codex_special_reset`` so
    the CLI cannot accept a record the widgets would reject.
    """
    entry = normalize_special_reset(cache.get("codex_special_reset"))
    announcement = entry.get("announcement") if isinstance(entry, dict) else None
    if not isinstance(announcement, dict):
        return ""
    announced = _parse_iso(announcement.get("announced_at"))
    if announced is None:
        return ""
    title = "Codex banked reset" if announcement.get("reset_type") == "banked" \
        else "Codex special reset"
    zone = local_tz()
    announced_local = announced.astimezone(zone)
    scheduled = _parse_iso(announcement.get("scheduled_for"))
    scheduled_local = scheduled.astimezone(zone) if scheduled is not None else None
    parts = [f"✨ **{title}**"]
    if scheduled_local is not None:
        # The publisher's estimate is the actionable time; the announcement
        # timestamp is when the post appeared.
        parts.append(f"scheduled {scheduled_local.strftime('%b %d %H:%M')}")
        same_day = scheduled_local.date() == announced_local.date()
        parts.append(f"announced {announced_local.strftime('%H:%M' if same_day else '%b %d %H:%M')}")
    else:
        # Date and time: an announcement without the hour is not actionable.
        parts.append(f"announced {announced_local.strftime('%b %d %H:%M')}")
        status = announcement.get("status")
        if status == "scheduled":
            parts.append("scheduled")
        elif status == "recorded":
            parts.append("recorded")
    if entry.get("stale") is not False:
        parts.append("stale")
    parts.append("codex-resets.com")
    return " · ".join(parts)


def _render_quota(provider_filter: Optional[str]) -> str:
    """Render the per-provider quota dashboard (optionally filtered).

    Layout: one block per provider/account, worst remaining quota first, then
    one line per real meter with a proportional bar. A provider with no saved
    account is hidden from the unfiltered view; asking for it by name still
    explains why it is absent.
    """
    cache = read_quota_cache() or {}
    providers = cache.get("providers") or {}
    if not providers:
        return "📊 **Quota** — no data yet.\n" \
               "Run `/quota refresh` (or `hermes quota refresh`) to fetch it."

    pf = " ".join((provider_filter or "").split()).lower()
    rows: list[tuple[int, str, dict]] = []
    hidden = 0
    for index, (name, rec) in enumerate(iter_account_records(providers)):
        if not isinstance(rec, dict):
            continue
        if pf:
            if not _matches_filter(pf, name, rec):
                continue
        elif rec.get("unavailable_reason") == _HIDDEN_REASON:
            # No credential: this provider has no account on this machine.
            hidden += 1
            continue
        rows.append((index, name, rec))

    if not rows:
        if pf:
            return f"📊 **Quota** — no provider matching '{provider_filter}'."
        return "📊 **Quota** — no provider has a saved account yet.\n" \
               "Configure one, then run `/quota refresh` (see docs/providers.md)."

    # Most constrained first: the row that needs attention is at the top.
    def _sort_key(row):
        worst = _worst_remaining(row[2])
        return (worst is None, worst if worst is not None else 0, row[0])

    rows.sort(key=_sort_key)

    # One shared column width keeps every bar in the same place down the page.
    label_width = 0
    for _index, _name, rec in rows:
        for window in rec.get("windows") or []:
            if isinstance(window, dict):
                label_width = max(label_width, len(str(window.get("label") or "window")))
    label_width = min(max(label_width, 6), 20)

    blocks: list[str] = []
    for _index, name, rec in rows:
        detail = rec.get("details")
        details = [d.strip() for d in detail if isinstance(d, str) and d.strip()] \
            if isinstance(detail, list) else []
        windows = [w for w in (rec.get("windows") or []) if isinstance(w, dict)]
        title = _provider_label(name, rec)
        plan = rec.get("plan")
        if isinstance(plan, str) and plan.strip():
            title = f"{title} ({plan.strip()})"
        reason = rec.get("unavailable_reason")
        if reason:
            head = f"**{title}** — unavailable ({reason})"
        else:
            head = f"**{title}**"
        body = [head]
        for window in windows:
            label = str(window.get("label") or "window")
            remaining = _remaining(window.get("used_percent"))
            tail = _reset_tail(window.get("reset_at"))
            if remaining is None:
                body.append(f"  {label:<{label_width}}  no data{tail}")
            else:
                body.append(
                    f"  {label:<{label_width}}  {_bar(remaining)}  {remaining:>3}% left{tail}"
                )
        for line in details:
            body.append(f"  · {line}")
        if not windows and not details and not reason:
            body.append("  no usage data reported")
        blocks.append("\n".join(body))

    out = [f"📊 **Quota** · {_age_label()}", ""]
    show_special = not pf or bool(
        _matches_filter(pf, "openai-codex", {"label": "openai-codex"})
    )
    special = _special_reset_line(cache) if show_special else ""
    if special:
        out.append(special)
        out.append("")
    out.append("\n\n".join(blocks))
    out.append("")
    if hidden:
        plural = "s" if hidden != 1 else ""
        out.append(f"_{hidden} provider{plural} without a saved account hidden "
                   f"— `/quota <provider>` shows one._")
    out.append("_Run `/quota refresh` to force a re-fetch._")
    return "\n".join(out)


def _plugin_stamp() -> dict[str, Any]:
    """Installed build info for the widget (manifest version + commit stamp).

    Read here, not in the widget, so a mount costs ONE ``cli.exec`` instead of
    two — the extra round-trip was part of the slow reload after a gateway
    switch.
    """
    out: dict[str, Any] = {"plugin_version": None, "installed_sha": None}
    root = Path(__file__).resolve().parent
    try:
        for line in (root / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                out["plugin_version"] = line.split(":", 1)[1].strip().strip("'\"")
                break
    except OSError:
        pass
    try:
        stamp = json.loads((root / "version.json").read_text(encoding="utf-8"))
        if isinstance(stamp, dict):
            out["installed_sha"] = stamp.get("installed_sha")
    except (OSError, ValueError):
        pass
    return out


def _render_quota_json() -> str:
    """Render the full quota cache as JSON for the desktop widget.

    Carries the cache age and the installed build stamp alongside the data so
    the widget can poll the cache (never the network), schedule its own refresh,
    and compare versions without further round-trips.
    """
    cache = read_quota_cache()
    # Ensure consistent shape for widget consumption
    providers = cache.get("providers") or {}
    out = {
        "fetched_at": cache.get("fetched_at"),
        "providers": providers,
        "age_s": quota_cache_age_seconds(),
        # The user's zone for every human timestamp on the client surfaces;
        # clients must not inherit their own process TZ.
        "timezone": cache.get("timezone") or timezone_name(),
        # Additive root contract shared with the TUI/Desktop reset panels.
        # It is announcement metadata only, never account quota/reset_at.
        "codex_special_reset": normalize_special_reset(
            cache.get("codex_special_reset")),
    }
    out.update(_plugin_stamp())
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
        # Anything else is a provider filter. Keep the whole phrase, not just
        # the first word, so "conta 2" and "openai codex" still match.
        return _render_quota(raw)
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
    status_p.add_argument(
        "--cached",
        action="store_true",
        help="Never refresh: emit the cache as-is (desktop widget poll path)",
    )
    subs.add_parser("refresh", help="Force a re-fetch of all providers")
    prov = subs.add_parser("provider", help="Show quota for one provider")
    prov.add_argument("name", help="provider id, e.g. anthropic, grok, openai-codex")
    for provider_id in _PROVIDER_IDS:
        subs.add_parser(provider_id, help=f"Show quota for {provider_id}")
    _setup_monitor_argparse(subs)
    subparser.set_defaults(func=_handle_cli)


def _setup_monitor_argparse(subs) -> None:
    """``hermes quota monitor <view>``: one JSON object per call (docs/monitor.md)."""
    mon = subs.add_parser("monitor", help="Agent Monitor views as JSON (live, health, usage, kanban, history)")
    msubs = mon.add_subparsers(dest="monitor_command")
    snap = msubs.add_parser("snapshot", help="Everything the widget shows in one poll")
    hist = msubs.add_parser("history", help="Quota history + burn rate")
    hist.add_argument("--days", type=int, default=1, help="Days of history (1-31, default 1)")
    rec = msubs.add_parser("recommend", help="Rank the models work can be routed to now")
    rec.add_argument("--prefer-provider", dest="prefer_provider", default=None)
    rec.add_argument("--exclude", dest="exclude", action="append", default=[],
                     help="Provider to leave out (repeatable or comma separated)")
    rec.add_argument("--min-left", dest="min_left", type=int, default=10,
                     help="Minimum %% left in every quota window (default 10)")
    rec.add_argument("--top", type=int, default=3)
    bud = msubs.add_parser("budget", help="Spend today / month to date vs the stored budget")
    setb = msubs.add_parser("set-budget", help="Store the daily / monthly budget (USD, 0 clears)")
    setb.add_argument("--daily", type=float, default=None)
    setb.add_argument("--monthly", type=float, default=None)
    acc = msubs.add_parser("accounts", help="Credential pools, strategies, fallbacks and sign-in gaps (read-only)")
    setp = msubs.add_parser("set-priority", help="Reorder one pooled credential via `hermes auth priority`")
    setp.add_argument("--provider", required=True)
    setp.add_argument("--target", required=True, help="Short pool id of the credential (see `accounts`)")
    setp.add_argument("--priority", required=True, help="New zero-based position (0 = tried first)")
    sets = msubs.add_parser("set-strategy", help="Set a profile's pool strategy via `hermes config set`")
    sets.add_argument("--provider", required=True)
    sets.add_argument("--strategy", required=True, help="fill_first | round_robin | least_used | random")
    for p in (setp, sets):
        # The pool owner / profile to change. `hermes --profile X ...` also selects X when this is omitted
        # (Hermes consumes the first --profile flag as the process profile).
        p.add_argument("--profile", "--target-profile", dest="target_profile", default=None)
    for p in (mon, snap, hist, rec, bud, setb, acc, setp, sets):
        p.add_argument("--json", action="store_true", help="Emit JSON (the only output format)")


def _handle_monitor(args) -> int:
    from . import quota_monitor as qm

    cmd = getattr(args, "monitor_command", None) or "snapshot"
    try:
        if cmd == "snapshot":
            payload = qm.snapshot()
        elif cmd == "history":
            payload = qm.history_payload(getattr(args, "days", 1) or 1)
        elif cmd == "recommend":
            exclude = [p.strip() for raw in (getattr(args, "exclude", None) or []) for p in raw.split(",") if p.strip()]
            payload = qm.recommend_payload(getattr(args, "prefer_provider", None) or None, exclude,
                                           getattr(args, "min_left", 10), getattr(args, "top", 3))
        elif cmd == "budget":
            payload = qm.budget_payload()
        elif cmd == "set-budget":
            payload = qm.set_budget(getattr(args, "daily", None), getattr(args, "monthly", None))
        elif cmd == "accounts":
            payload = qm.accounts_view()
        elif cmd == "set-priority":
            payload = qm.set_priority(args.provider, args.target, args.priority, getattr(args, "target_profile", None))
        elif cmd == "set-strategy":
            payload = qm.set_strategy(args.provider, args.strategy, getattr(args, "target_profile", None))
        else:
            payload = {"monitor_version": 1, "error": f"unknown monitor command: {cmd}"}
    except Exception as e:  # the widget parses stdout: always answer with one JSON object
        print(json.dumps({"monitor_version": 1, "error": f"{type(e).__name__}: {e}"[:300]}))
        return 1
    print(json.dumps(payload, default=str, separators=(",", ":")))
    return 1 if "error" in payload else 0


def _handle_cli(args):
    cmd = getattr(args, "quota_command", None) or "status"
    if cmd == "monitor":
        return _handle_monitor(args)
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
        # Two readers share this payload:
        #  * the widget polls with --cached and wants the cache NOW; its refresh
        #    runs out-of-band so the poll cadence stays exact (blocking the poll
        #    on a provider fetch is what stretched a 60s interval past 100s);
        #  * a plain call with --max-age blocks until the cache is fresh enough.
        if not getattr(args, "cached", False):
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
