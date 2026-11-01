"""Xiaomi MiMo (Token Plan) quota fetcher — plugin standalone copy.

Data source: the MiMo platform console API (``https://platform.xiaomimimo.com/api/v1``),
authenticated with the Xiaomi SSO console session cookie. The ``tp-`` inference
key cannot read quota — verified live 2026-10-23: ``token-plan-sgp.xiaomimimo.com``
serves only ``/v1/*`` inference paths (``/v1/user/balance`` and
``/v1/tokenPlan/usage`` answer 404) and every console endpoint answers
``401 {"code":401,"loginUrl":"https://account.xiaomi.com/pass/serviceLogin…}``
to a Bearer key.

Documented console response shapes (CodexBar ``docs/mimo.md``; payload fields
cross-checked against captured console responses):

  GET /api/v1/tokenPlan/usage
      {"code": 0, "data": {"monthUsage": {"percent": 0.39,
          "items": [{"name": "month_total_token", "used": 14954038035,
                     "limit": 38000000000, "percent": 0.39}]},
       "usage": {"items": [{"name": "plan_total_token", "used": …, "limit": …},
                           {"name": "compensation_total_token", …}]}}}
  GET /api/v1/tokenPlan/detail
      {"data": {"planCode": "pro", "planName": "Pro",
                "currentPeriodEnd": "2026-11-05 23:59:59",
                "expired": false, "enableAutoRenew": true}}
  GET /api/v1/balance
      {"data": {"balance": "25.51", "currency": "USD"}}

Cookie source (opt-in, like ``grok``): the ``XIAOMI_MIMO_COOKIE`` env var (a raw
``Cookie:`` header value, e.g. set in ``$HERMES_HOME/.env``) or
``~/mimo_session.json`` holding ``{"cookies": {...}}``, ``{"cookies": "…"}`` or
a raw header string. Required names: ``api-platform_serviceToken`` and
``userId`` (optional: ``api-platform_ph``, ``api-platform_slh``). Copy them from
the Network tab of https://platform.xiaomimimo.com/#/console/balance while
logged in. Cookie values never leave this module: they go into request headers
only, never into labels, details or logs.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .api_keys import amount
from .base import (
    AccountBalance,
    Deadline,
    QuotaResult,
    QuotaWindow,
    build_unavailable,
    opt_in_flag,
    urlopen_no_redirect,
)

_PLATFORM = "https://platform.xiaomimimo.com/api/v1"
_USAGE_URL = f"{_PLATFORM}/tokenPlan/usage"
_DETAIL_URL = f"{_PLATFORM}/tokenPlan/detail"
_BALANCE_URL = f"{_PLATFORM}/balance"
_SESSION_PATH = os.path.join(os.path.expanduser("~"), "mimo_session.json")
_REQUIRED_COOKIES = ("api-platform_serviceToken", "userId")

_ITEM_LABELS = {
    "month_total_token": "Monthly tokens",
    "plan_total_token": "Plan credits",
    "compensation_total_token": "Compensation credits",
}
#: Bound on parsed usage items per response (one overflow diagnostic max).
_MAX_ITEMS = 8


def _clean_str(value: object) -> Optional[str]:
    if isinstance(value, str) and value.strip() and len(value) <= 64:
        return value.strip()
    return None


def _int(value: object) -> Optional[int]:
    """Finite non-negative int from JSON number or numeric string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 10**18 else None
    if isinstance(value, float):
        return int(value) if 0 <= value <= 10**18 and value.is_integer() else None
    if isinstance(value, str) and value.strip().isdigit():
        return _int(int(value.strip()))
    return None


def _server_pct(value: object) -> Optional[float]:
    """Server-reported percent as 0..100; a fraction <= 1 is scaled."""
    if isinstance(value, bool):
        return None
    try:
        pct = float(str(value))
    except (TypeError, ValueError):
        return None
    if pct != pct or pct in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if 0.0 <= pct <= 1.0:
        pct *= 100.0
    return max(0.0, min(100.0, pct)) if 0.0 <= pct <= 100.0 else None


def _fmt_tokens(value: int) -> str:
    for limit, suffix in ((10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K")):
        if value >= limit:
            return f"{value / limit:.2f}{suffix}"
    return str(value)


def _parse_period_end(value: object) -> Optional[str]:
    """``currentPeriodEnd`` ('2026-11-05 23:59:59', console-reported) → ISO UTC.

    The console prints the period end without a zone; CodexBar renders the same
    field as UTC, so it is carried as UTC. A value that does not parse yields
    ``None`` — a reset instant is never invented.
    """
    text = _clean_str(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return dt.isoformat()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _pairs_from_header(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in str(text).split(";"):
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if sep and name and value and name not in pairs and len(name) <= 64:
            pairs[name] = value
    return pairs


def _header_from_pairs(pairs: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def _load_cookie() -> tuple[Optional[str], Optional[str]]:
    """(cookie header, reason). Never returns or logs a partial secret."""
    raw = os.environ.get("XIAOMI_MIMO_COOKIE", "").strip()
    if not raw:
        raw = _cookie_from_env_file()
    if not raw:
        raw = _cookie_from_session_file()
    if not raw:
        return None, "no-session-cookies"
    pairs = _pairs_from_header(raw)
    if not all(name in pairs for name in _REQUIRED_COOKIES):
        return None, "incomplete-cookies"
    return _header_from_pairs(pairs), None


def _cookie_from_env_file() -> str:
    """``XIAOMI_MIMO_COOKIE`` straight from ``$HERMES_HOME/.env`` when the
    process env does not carry it (a standalone test run)."""
    try:
        from hermes_constants import get_hermes_home

        path = os.path.join(str(get_hermes_home()), ".env")
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                name, sep, value = line.partition("=")
                if sep and name.strip() == "XIAOMI_MIMO_COOKIE":
                    value = value.strip().strip('"').strip("'")
                    if value:
                        return value
    except Exception:  # noqa: BLE001 - fail-open: no env file is not an error
        pass
    return ""


def _cookie_from_session_file() -> str:
    """``~/mimo_session.json``: ``{"cookies": {...}}``, ``{"cookies": "…"}``
    or a raw header string (same shape grok's session file uses)."""
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cookies = data.get("cookies") or data if isinstance(data, dict) else data
        if isinstance(cookies, dict):
            return _header_from_pairs(
                {str(k): str(v) for k, v in cookies.items() if str(k) and str(v)}
            )
        if isinstance(cookies, str) and cookies.strip():
            return cookies.strip()
    except Exception:  # noqa: BLE001 - fail-open: absent/unreadable file
        pass
    return ""


def _get_json(url: str, cookie: str, timeout: float):
    """(payload, safe reason) for one cookie-authenticated GET.

    Uses the no-redirect seam so the session cookie is never replayed to a
    redirect target; response bodies are byte-capped. Response text is never
    surfaced — only the machine-readable reason codes.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Cookie": cookie,
                "Accept": "application/json",
                "Accept-Language": "en",
            },
        )
        with urlopen_no_redirect(req, timeout=timeout) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None, "response-too-large"
        try:
            return json.loads(body), None
        except (ValueError, UnicodeError):
            return None, "bad-json"
    except urllib.error.HTTPError as exc:
        code = exc.code
        if exc.fp is not None:
            exc.close()
        return None, "auth-failed" if code in (401, 403) else f"http-{code}"
    except TimeoutError:
        return None, "timeout"
    except Exception:  # noqa: BLE001 - fail-open by contract
        return None, "fetch-error"


def _data_of(payload: object) -> dict:
    """Console envelopes wrap the body in ``data``; tolerate both shapes."""
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict):
            return inner
    return payload if isinstance(payload, dict) else {}


def _usage_windows(items: list, reset_at: Optional[str]):
    """(windows, details) from one console usage item list.

    A used_percent is emitted only from used/limit or a server-reported
    percent — never fabricated from a bare count.
    """
    windows: list[QuotaWindow] = []
    details: list[str] = []
    for item in items[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        name = _clean_str(item.get("name")) or "token"
        label = _ITEM_LABELS.get(name, name.replace("_", " ").capitalize())
        used = _int(item.get("used"))
        limit = _int(item.get("limit"))
        if limit and limit > 0 and used is not None:
            pct = max(0.0, min(100.0, 100.0 * used / limit))
            windows.append(
                QuotaWindow(label=label, used_percent=round(pct, 2), reset_at=reset_at)
            )
        else:
            pct = _server_pct(item.get("percent"))
            if pct is not None:
                windows.append(
                    QuotaWindow(label=label, used_percent=round(pct, 2), reset_at=reset_at)
                )
            elif used:
                details.append(f"{label} used: {_fmt_tokens(used)}")
    return windows, details


def fetch_mimo_quota() -> QuotaResult:
    if not _mimo_enabled():
        return build_unavailable("xiaomi", "opt-in-disabled")
    try:
        cookie, reason = _load_cookie()
    except Exception:  # noqa: BLE001 - fail-open by contract
        return build_unavailable("xiaomi", "fetch-error")
    if not cookie:
        return build_unavailable(
            "xiaomi",
            reason or "no-session-cookies",
            details=[
                "Quota needs the platform.xiaomimimo.com console session cookie "
                "(api-platform_serviceToken + userId), not the tp- inference key",
            ],
        )

    try:
        deadline = Deadline(15)
        payloads: dict[str, dict] = {}
        errors: dict[str, str] = {}
        for name, url in (
            ("usage", _USAGE_URL),
            ("detail", _DETAIL_URL),
            ("balance", _BALANCE_URL),
        ):
            if deadline.expired():
                errors[name] = "timeout"
                continue
            payload, error = _get_json(url, cookie, deadline.slice(8))
            if error:
                errors[name] = error
            elif isinstance(payload, dict):
                payloads[name] = payload
            else:
                errors[name] = "parse-pending"

        if not payloads:
            reason = errors.get("usage") or next(iter(errors.values()), "no-data")
            return build_unavailable("xiaomi", reason)

        plan: Optional[str] = None
        reset_at: Optional[str] = None
        details: list[str] = []
        balances: list[AccountBalance] = []
        windows: list[QuotaWindow] = []

        detail = _data_of(payloads.get("detail"))
        plan = _clean_str(detail.get("planName")) or _clean_str(detail.get("planCode"))
        reset_at = _parse_period_end(detail.get("currentPeriodEnd"))
        if detail.get("expired") is True:
            details.append("Token Plan expired")
        if reset_at:
            details.append(f"Plan period ends {reset_at}")

        usage = _data_of(payloads.get("usage"))
        month = usage.get("monthUsage")
        month = month if isinstance(month, dict) else {}
        month_items = month.get("items")
        month_items = month_items if isinstance(month_items, list) else []
        plan_block = usage.get("usage")
        plan_block = plan_block if isinstance(plan_block, dict) else {}
        plan_items = plan_block.get("items")
        plan_items = plan_items if isinstance(plan_items, list) else []
        windows, usage_details = _usage_windows(month_items, reset_at)
        if not windows:
            windows, usage_details = _usage_windows(plan_items, reset_at)
        details.extend(usage_details)

        balance = _data_of(payloads.get("balance"))
        total = amount(balance.get("balance"))
        currency = _clean_str(balance.get("currency"))
        if total is not None and currency:
            granted = amount(balance.get("granted_balance"))
            topped = amount(balance.get("topped_up_balance") or balance.get("charge_balance"))
            balances.append(
                AccountBalance(
                    currency,
                    f"{total:f}",
                    f"{granted:f}" if granted is not None else None,
                    f"{topped:f}" if topped is not None else None,
                )
            )
        elif total is not None:
            details.append(f"Balance: {total:f}")

        if not windows and not details and not balances and not plan:
            return build_unavailable("xiaomi", "parse-pending")
        return QuotaResult(
            label="xiaomi", windows=windows, plan=plan, details=details,
            account_balances=balances,
        )
    except Exception:  # noqa: BLE001 - fail-open by contract
        return build_unavailable("xiaomi", "fetch-error")


# MiMo quota reads the console SESSION COOKIE (user-session material), so it is
# opt-in like grok and antigravity, default off. Enable with:
#   hermes config set plugins.entries.quota.settings.mimoEnabled true
def _mimo_enabled() -> bool:
    val = os.environ.get("HERMES_QUOTA_MIMO_ENABLED", "").strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get("quota") if isinstance(entries, dict) else None
        if isinstance(entry, dict):
            settings = entry.get("settings")
            if isinstance(settings, dict) and "mimoEnabled" in settings:
                return opt_in_flag(settings.get("mimoEnabled"))
    except Exception:  # noqa: BLE001 - fail-open: unreadable config means off
        pass
    return False


from .registry import register as _register  # noqa: E402

_register("xiaomi")(fetch_mimo_quota)
