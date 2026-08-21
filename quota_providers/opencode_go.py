"""OpenCode Go quota fetcher — plugin standalone copy.

OpenCode Go (https://opencode.ai/docs/go) is a low-cost subscription that
exposes usage limits as three rolling dollar-denominated windows:

  * 5 hour limit  — $12 of usage
  * Weekly limit  — $30 of usage
  * Monthly limit — $60 of usage

The authoritative numbers come from the same endpoint CodexBar uses:

    GET https://opencode.ai/zen/go/v1/usage
    Authorization: Bearer <API key>

The API key is the OpenCode Zen key copied from the console
(https://opencode.ai/auth).  Resolution order:

1. ``OPENCODE_API_KEY`` environment variable (the same var OpenCode itself
   and CodexBar read);
2. ``opencode`` entry in OpenCode's own auth file,
   ``~/.local/share/opencode/auth.json`` (written by ``opencode auth login``
   / the ``/connect`` TUI command).  The file stores one record per provider;
   we accept either a plain API-key object or an OAuth record whose nested
   payload carries the key.

Note on OAuth: OpenCode's CLI supports an OAuth *device flow* against the
console (``POST {console}/auth/device/code`` → ``/auth/device/token`` with
``client_id: opencode-cli``), but that flow authenticates the console account
and is not required to read usage — the Zen usage endpoint accepts the static
API key directly, so this fetcher never performs network auth.

Parsing mirrors CodexBar's tolerant approach: window dicts are located under
``rollingUsage`` / ``weeklyUsage`` / ``monthlyUsage`` (plus common snake_case
aliases), percentages accept both 0–100 and 0–1 fractions, resets arrive as
either ``resetInSec`` seconds or absolute timestamps, and percent can be
computed from ``used/limit`` pairs when no direct field exists.  Anything the
parser cannot find becomes an honest ``unavailable_reason``, never fake zeros.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_PROVIDER_ID = "opencode-go"
_API_URL = "https://opencode.ai/zen/go/v1/usage"
def _auth_file_candidates() -> tuple[str, ...]:
    """Known locations of OpenCode's local auth file across platforms.

    Linux/macOS use ``~/.local/share/opencode/auth.json``; on Windows the CLI
    keeps state under ``%LOCALAPPDATA%\\opencode\\auth.json`` (with an
    XDG-style override via ``XDG_DATA_HOME``).
    """
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".local", "share", "opencode", "auth.json")]
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        candidates.append(os.path.join(xdg_data, "opencode", "auth.json"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "opencode", "auth.json"))
    return tuple(candidates)


_AUTH_PATHS = _auth_file_candidates()
_ENV_KEYS = ("OPENCODE_API_KEY",)

_PERCENT_KEYS = (
    "usagePercent",
    "usedPercent",
    "percentUsed",
    "percent",
    "usage_percent",
    "used_percent",
    "utilization",
    "utilizationPercent",
    "utilization_percent",
    "usage",
)
_RESET_IN_SEC_KEYS = (
    "resetInSec",
    "resetInSeconds",
    "resetSeconds",
    "reset_sec",
    "reset_in_sec",
    "resetsInSec",
    "resetsInSeconds",
    "resetIn",
    "resetSec",
)
_RESET_AT_KEYS = (
    "resetAt",
    "resetsAt",
    "reset_at",
    "resets_at",
    "nextReset",
    "next_reset",
)
_USED_KEYS = ("used", "usage", "consumed", "count", "usedTokens")
_LIMIT_KEYS = ("limit", "total", "quota", "max", "cap", "tokenLimit")

_ROLLING_KEYS = ("rollingUsage", "rolling", "rolling_usage", "rollingWindow", "rolling_window")
_WEEKLY_KEYS = ("weeklyUsage", "weekly", "weekly_usage", "weeklyWindow", "weekly_window")
_MONTHLY_KEYS = ("monthlyUsage", "monthly", "monthly_usage", "monthlyWindow", "monthly_window")


# -- credential resolution ----------------------------------------------------


def _read_env_api_key() -> Optional[str]:
    for key in _ENV_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        trimmed = value.strip().strip("\"'")
        if trimmed:
            return trimmed
    return None


def _extract_api_key(value: Any) -> Optional[str]:
    """Pull an API key out of one auth.json provider record."""
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    if not isinstance(value, dict):
        return None
    # Plain API-key record: {"type": "api", "key": "..."}
    for field in ("key", "apiKey", "api_key"):
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    # OAuth-style record: {"type": "oauth", "refresh": ..., "access": ...} or
    # nested payloads where the Zen key rides along under extra fields.
    for container in (value, value.get("payload"), value.get("data")):
        if not isinstance(container, dict):
            continue
        for field in ("zenApiKey", "zen_api_key", "goApiKey", "go_api_key"):
            candidate = container.get(field)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _load_auth_file_key() -> Optional[str]:
    for path in _AUTH_PATHS:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # Preferred: the opencode-go provider record; fall back to plain
        # "opencode" / "opencode-zen" entries which share the console key.
        for provider in ("opencode-go", "opencode", "opencode-zen", "zen"):
            if provider in data:
                key = _extract_api_key(data[provider])
                if key:
                    return key
    return None


def resolve_api_key() -> Optional[str]:
    return _read_env_api_key() or _load_auth_file_key()


# -- tolerant response parsing ------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    return int(number) if number is not None else None


def _parse_timestamp(value: Any) -> Optional[str]:
    """Normalize epoch seconds/millis or ISO-8601 text to an ISO-8601 string."""
    number = _as_float(value)
    if number is not None:
        if number > 1_000_000_000_000:
            number /= 1000.0
        if number > 1_000_000_000:
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            return None
    return None


def _window_percent(window: dict) -> Optional[float]:
    percent = None
    direct = False
    for key in _PERCENT_KEYS:
        value = _as_float(window.get(key))
        if value is not None:
            percent = value
            direct = True
            break
    if percent is None:
        used = next((_as_float(window[k]) for k in _USED_KEYS if _as_float(window.get(k)) is not None), None)
        limit = next((_as_float(window[k]) for k in _LIMIT_KEYS if _as_float(window.get(k)) is not None), None)
        if used is not None and limit is not None and limit > 0:
            percent = (used / limit) * 100.0
    if percent is None:
        return None
    # A direct percent may arrive as a fraction (0..1); computed used/limit
    # values are already 0..100 and must not be rescaled.
    if direct and 0.0 <= percent <= 1.0:
        percent *= 100.0
    return max(0.0, min(100.0, percent))


def _window_reset(window: dict, now: float) -> Optional[str]:
    for key in _RESET_IN_SEC_KEYS:
        seconds = _as_int(window.get(key))
        if seconds is not None and seconds >= 0:
            return datetime.fromtimestamp(now + seconds, tz=timezone.utc).isoformat()
    for key in _RESET_AT_KEYS:
        reset_at = _parse_timestamp(window.get(key))
        if reset_at is not None:
            return reset_at
    return None


def _parse_window(window: dict, label: str, now: float) -> Optional[QuotaWindow]:
    if not isinstance(window, dict):
        return None
    used_percent = _window_percent(window)
    if used_percent is None:
        return None
    return QuotaWindow(
        label=label,
        used_percent=round(used_percent, 2),
        reset_at=_window_reset(window, now),
    )


def _first_dict(record: dict, keys: tuple[str, ...]) -> Optional[dict]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return None


def parse_usage_payload(data: Any, now: Optional[float] = None) -> list[QuotaWindow]:
    """Extract Go's rolling/weekly/monthly windows from any reasonable shape."""
    if not isinstance(data, dict):
        return []
    moment = time.time() if now is None else now

    renews_at = _parse_timestamp(data.get("renewsAt") or data.get("renewAt"))

    # Direct shape: {"rollingUsage": {...}, "weeklyUsage": {...}, ...}
    rolling = _first_dict(data, _ROLLING_KEYS)
    weekly = _first_dict(data, _WEEKLY_KEYS)
    monthly = _first_dict(data, _MONTHLY_KEYS)

    # Nested shapes: {"data": {...}} / {"result": {...}} / {"usage": {...}}
    if rolling is None:
        for wrapper in ("data", "result", "usage", "billing", "payload"):
            nested = data.get(wrapper)
            if isinstance(nested, dict):
                found = parse_usage_payload(nested, now=moment)
                if found:
                    return found
        # Last resort: scan one level deep for dicts whose keys mention the
        # window names (CodexBar's "candidates" strategy, simplified).
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            lower = str(key).lower()
            if rolling is None and any(t in lower for t in ("rolling", "hour", "5h")):
                rolling = value
            elif weekly is None and "week" in lower:
                weekly = value
            elif monthly is None and "month" in lower:
                monthly = value

    windows: list[QuotaWindow] = []
    parsed_rolling = _parse_window(rolling, "5-hour", moment) if rolling else None
    if parsed_rolling is None:
        return []
    windows.append(parsed_rolling)
    parsed_weekly = _parse_window(weekly, "Weekly", moment) if weekly else None
    if parsed_weekly:
        windows.append(parsed_weekly)
    parsed_monthly = _parse_window(monthly, "Monthly", moment) if monthly else None
    if parsed_monthly:
        windows.append(parsed_monthly)
    if renews_at and len(windows) < 3:
        windows[-1].reset_at = windows[-1].reset_at or renews_at
    return windows


# -- network ------------------------------------------------------------------


def fetch_usage(api_key: str) -> QuotaResult:
    request = urllib.request.Request(
        _API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "hermes-quota-plugin",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return build_unavailable(_PROVIDER_ID, "auth-failed")
        return build_unavailable(_PROVIDER_ID, f"http-{exc.code}")
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return build_unavailable(_PROVIDER_ID, f"fetch-error:{type(exc).__name__}")

    try:
        data = json.loads(raw)
    except Exception:
        return build_unavailable(_PROVIDER_ID, "bad-json")

    windows = parse_usage_payload(data)
    plan = None
    if isinstance(data, dict):
        plan_value = data.get("plan") or data.get("planType") or data.get("plan_type")
        if isinstance(plan_value, str) and plan_value.strip():
            plan = plan_value.strip().title()
    if not windows:
        return build_unavailable(_PROVIDER_ID, "no-data")
    return QuotaResult(label=_PROVIDER_ID, windows=windows, plan=plan, unavailable_reason=None)


def fetch_opencode_go_quota() -> QuotaResult:
    api_key = resolve_api_key()
    if not api_key:
        return build_unavailable(_PROVIDER_ID, "no-credentials")
    return fetch_usage(api_key)


from .registry import register as _register  # noqa: E402

_register("opencode-go")(fetch_opencode_go_quota)
