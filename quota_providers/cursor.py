"""Cursor (Cursor Agent CLI) quota fetcher — plugin standalone copy.

Data source: the two Connect RPCs the ``cursor-agent`` CLI itself calls,
authenticated with the CLI's own session token::

    POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage
    POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetPlanInfo
    Authorization: Bearer <access token>
    Content-Type: application/json   (Connect JSON, body "{}")

Observed ``GetCurrentPeriodUsage`` shape (int64 fields arrive as strings)::

    {"billingCycleStart": "1788974640000", "billingCycleEnd": "1791566640000",
     "planUsage": {"totalSpend": 21710, "includedSpend": 2000, "limit": 2000,
                   "autoPercentUsed": 89.3, "apiPercentUsed": 73.7,
                   "totalPercentUsed": 86.8, ...},
     "spendLimitUsage": {"pooledLimit": "750000", "pooledUsed": 208899,
                         "individualLimit": ..., "individualUsed": ...,
                         "limitType": "team"}, ...}

``GetPlanInfo`` answers ``{"planInfo": {"planName": "Team", ...}}``.

Token resolution (read-only; the CLI owns refresh):

1. macOS keychain item ``cursor-access-token`` (where the CLI stores it);
2. the CLI's ``auth.json`` fallback (``~/.cursor/auth.json`` on macOS,
   ``$XDG_CONFIG_HOME/cursor/auth.json`` on Linux,
   ``%APPDATA%\\Cursor\\auth.json`` on Windows), key ``accessToken``.

``Included`` and ``API`` percentages are server-reported and match the two
messages Cursor itself shows ("You've used N% of your included total/API
usage"). ``autoPercentUsed`` is not surfaced: Cursor never displays it and it
would otherwise become the widget's "worst window". A personal on-demand cap
becomes a used/limit window; a shared team pool is shown as a detail line only,
because it is not the user's own quota.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_PROVIDER_ID = "cursor"
_API_ROOT = "https://api2.cursor.sh/aiserver.v1.DashboardService"
_KEYCHAIN_SERVICE = "cursor-access-token"
# Keychain read plus two sequential RPCs must finish inside the cache sweep
# budget (quota_cache.REFRESH_BUDGET_S = 20s): 3 + 7 + 7 = 17s worst case.
_KEYCHAIN_TIMEOUT_S = 3
_HTTP_TIMEOUT_S = 7


# -- credential resolution ----------------------------------------------------


def _keychain_token() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    return token or None


def _auth_file_path() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, "Cursor", "auth.json")
    if sys.platform == "darwin":
        return os.path.join(home, ".cursor", "auth.json")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "cursor", "auth.json")


def _auth_file_token() -> Optional[str]:
    try:
        with open(_auth_file_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    token = data.get("accessToken") if isinstance(data, dict) else None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def resolve_access_token() -> Optional[str]:
    return _keychain_token() or _auth_file_token()


# -- parsing ------------------------------------------------------------------


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _pct(value: Any) -> Optional[float]:
    number = _num(value)
    if number is None:
        return None
    return round(max(0.0, min(100.0, number)), 2)


def _iso_from_ms(value: Any) -> Optional[str]:
    ms = _num(value)
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _dollars(cents: float) -> str:
    return f"${cents / 100.0:,.2f}"


def parse_usage(data: Any) -> tuple[list[QuotaWindow], list[str]]:
    if not isinstance(data, dict):
        return [], []
    reset = _iso_from_ms(data.get("billingCycleEnd"))
    windows: list[QuotaWindow] = []
    details: list[str] = []

    plan = data.get("planUsage")
    if isinstance(plan, dict):
        for key, label in (("totalPercentUsed", "Included"), ("apiPercentUsed", "API")):
            used = _pct(plan.get(key))
            if used is not None:
                windows.append(QuotaWindow(label=label, used_percent=used, reset_at=reset))

    spend = data.get("spendLimitUsage")
    if isinstance(spend, dict):
        # The user's own cap is a quota window; a team pool is context only.
        for used_key, limit_key, label, is_window in (
            ("individualUsed", "individualLimit", "On-demand", True),
            ("overallUsed", "overallLimit", "On-demand", True),
            ("pooledUsed", "pooledLimit", "Team on-demand", False),
        ):
            used = _num(spend.get(used_key))
            limit = _num(spend.get(limit_key))
            if used is not None and limit is not None and limit > 0:
                if is_window:
                    windows.append(
                        QuotaWindow(label=label, used_percent=_pct(used / limit * 100.0), reset_at=reset)
                    )
                details.append(f"{label}: {_dollars(used)} of {_dollars(limit)}")
                break

    return windows, details


# -- network ------------------------------------------------------------------


def _post(method: str, token: str) -> tuple[Optional[Any], Optional[str]]:
    request = urllib.request.Request(
        f"{_API_ROOT}/{method}",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "Accept": "application/json",
            "User-Agent": "hermes-quota-plugin",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "auth-failed"
        return None, f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return None, f"fetch-error:{type(exc).__name__}"
    try:
        return json.loads(body), None
    except Exception:
        return None, "bad-json"


def _plan_name(token: str) -> Optional[str]:
    data, _ = _post("GetPlanInfo", token)
    info = data.get("planInfo") if isinstance(data, dict) else None
    name = info.get("planName") if isinstance(info, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def fetch_cursor_quota() -> QuotaResult:
    try:
        token = resolve_access_token()
        if not token:
            return build_unavailable(_PROVIDER_ID, "no-credentials")
        data, reason = _post("GetCurrentPeriodUsage", token)
        if data is None:
            return build_unavailable(_PROVIDER_ID, reason or "no-data")
        windows, details = parse_usage(data)
        if not windows:
            return build_unavailable(_PROVIDER_ID, "no-data")
        return QuotaResult(
            label=_PROVIDER_ID,
            windows=windows,
            plan=_plan_name(token),
            unavailable_reason=None,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return build_unavailable(_PROVIDER_ID, f"fetch-error:{type(exc).__name__}")


from .registry import register as _register  # noqa: E402

_register(_PROVIDER_ID)(fetch_cursor_quota)
