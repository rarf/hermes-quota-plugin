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

Token resolution uses the CLI's own session token and refresh token:

1. macOS keychain item ``cursor-access-token`` (where the CLI stores it);
2. the CLI's ``auth.json`` fallback (``~/.cursor/auth.json`` on macOS,
   ``$XDG_CONFIG_HOME/cursor/auth.json`` on Linux,
   ``%APPDATA%\\Cursor\\auth.json`` on Windows), keys ``accessToken`` and
   ``refreshToken``.

If Cursor returns 401/403, the refresh token is exchanged once and the
refreshed access token is persisted back to the same local credential store.
The provider never logs token values.
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
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_PROVIDER_ID = "cursor"
_API_ROOT = "https://api2.cursor.sh/aiserver.v1.DashboardService"
_REFRESH_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
_KEYCHAIN_SERVICE = "cursor-access-token"
_KEYCHAIN_REFRESH_SERVICE = "cursor-refresh-token"
# Keychain read plus two sequential RPCs must finish inside the cache sweep
# budget (quota_cache.REFRESH_BUDGET_S = 20s): 3 + 7 + 7 = 17s worst case.
_REFRESH_TIMEOUT_S = 7
_KEYCHAIN_TIMEOUT_S = 3
_HTTP_TIMEOUT_S = 7


# -- credential resolution ----------------------------------------------------


def _keychain_token(service: str = _KEYCHAIN_SERVICE) -> Optional[str]:
    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
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


def _auth_file_data() -> Optional[dict[str, Any]]:
    try:
        with open(_auth_file_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _auth_file_token() -> Optional[str]:
    data = _auth_file_data()
    token = data.get("accessToken") if data else None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def resolve_access_token() -> Optional[str]:
    return _keychain_token() or _auth_file_token()


def resolve_refresh_token(access_token: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(refresh_token, storage_kind)`` for a rejected access token."""
    data = _auth_file_data()
    file_access = data.get("accessToken") if data else None
    file_refresh = data.get("refreshToken") if data else None
    if (
        isinstance(file_access, str)
        and file_access.strip() == access_token
        and isinstance(file_refresh, str)
        and file_refresh.strip()
    ):
        return file_refresh.strip(), "auth-file"

    # The access-token lookup intentionally remains the fast path. Only an
    # authentication failure reaches this fallback, so the extra keychain read
    # does not consume the normal refresh budget.
    refresh = _keychain_token(_KEYCHAIN_REFRESH_SERVICE)
    return (refresh, "keychain") if refresh else (None, None)


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
        # Prefer the user's own cap when both individual and overall values are
        # present. The shared team pool is independent context and must still be
        # retained as a detail line.
        for used_key, limit_key in (
            ("individualUsed", "individualLimit"),
            ("overallUsed", "overallLimit"),
        ):
            used = _num(spend.get(used_key))
            limit = _num(spend.get(limit_key))
            if used is not None and limit is not None and limit > 0:
                windows.append(
                    QuotaWindow(
                        label="On-demand",
                        used_percent=_pct(used / limit * 100.0),
                        reset_at=reset,
                    )
                )
                details.append(f"On-demand: {_dollars(used)} of {_dollars(limit)}")
                break

        pooled_used = _num(spend.get("pooledUsed"))
        pooled_limit = _num(spend.get("pooledLimit"))
        if pooled_used is not None and pooled_limit is not None and pooled_limit > 0:
            details.append(
                f"Team on-demand: {_dollars(pooled_used)} of {_dollars(pooled_limit)}"
            )

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


def _refresh_access_token(refresh_token: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Exchange a Cursor refresh token for the next access-token pair."""
    request = urllib.request.Request(
        _REFRESH_URL,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {refresh_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hermes-quota-plugin",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_REFRESH_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, None, "auth-failed"
        return None, None, f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return None, None, f"fetch-error:{type(exc).__name__}"

    try:
        payload = json.loads(body)
    except Exception:
        return None, None, "bad-json"
    if not isinstance(payload, dict):
        return None, None, "bad-json"

    access = payload.get("accessToken") or payload.get("access_token")
    rotated = payload.get("refreshToken") or payload.get("refresh_token")
    if not isinstance(access, str) or not access.strip():
        return None, None, "bad-json"
    if not isinstance(rotated, str) or not rotated.strip():
        rotated = None
    return access.strip(), rotated.strip() if rotated else None, None


def _persist_auth_file(access_token: str, refresh_token: Optional[str]) -> None:
    data = _auth_file_data()
    if data is None:
        return
    path = _auth_file_path()
    temporary_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(path) or ".",
            prefix=".auth.json.",
            delete=False,
        ) as fh:
            temporary_path = fh.name
            json.dump(data | {
                "accessToken": access_token,
                **({"refreshToken": refresh_token} if refresh_token else {}),
            }, fh)
            fh.write("\n")
        os.chmod(temporary_path, os.stat(path).st_mode & 0o777)
        os.replace(temporary_path, path)
        temporary_path = None
    except Exception:
        # A concurrent Cursor CLI write or a read-only config directory must
        # not turn a successful quota response into an unavailable result.
        return
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _store_keychain_token(service: str, token: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        # With -w as the final option, security reads the password from stdin
        # instead of exposing it in the process argument list.
        subprocess.run(
            ["security", "add-generic-password", "-s", service, "-U", "-w"],
            input=f"{token}\n",
            check=False,
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _persist_refreshed_credentials(
    storage_kind: Optional[str], access_token: str, refresh_token: Optional[str]
) -> None:
    if storage_kind == "auth-file":
        _persist_auth_file(access_token, refresh_token)
    elif storage_kind == "keychain":
        _store_keychain_token(_KEYCHAIN_SERVICE, access_token)
        if refresh_token:
            _store_keychain_token(_KEYCHAIN_REFRESH_SERVICE, refresh_token)


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
        if data is None and reason == "auth-failed":
            refresh_token, storage_kind = resolve_refresh_token(token)
            if refresh_token:
                fresh_token, rotated_refresh, refresh_reason = _refresh_access_token(refresh_token)
                if fresh_token:
                    _persist_refreshed_credentials(
                        storage_kind,
                        fresh_token,
                        rotated_refresh or refresh_token,
                    )
                    token = fresh_token
                    data, reason = _post("GetCurrentPeriodUsage", token)
                else:
                    reason = refresh_reason or reason
        if data is None:
            return build_unavailable(_PROVIDER_ID, reason or "no-data")
        windows, details = parse_usage(data)
        if not windows and not details:
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
