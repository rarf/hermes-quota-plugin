"""Z.ai (GLM Coding Plan) quota fetcher — plugin standalone copy.

Reads the same undocumented monitor endpoints Z.ai's own subscription UI uses
(stable in practice, verified by several independent consumers 2026-08/09):

    GET https://api.z.ai/api/monitor/usage/quota/limit   — the quota meters
    GET https://api.z.ai/api/biz/subscription/list       — plan name (optional)

Auth: ``Authorization: Bearer <api key>`` — the same Z.ai API key Hermes core
stores for the ``zai`` provider. Resolution mirrors other fetchers here: core's
``hermes_cli.auth._resolve_api_key_provider_secret("zai", ...)`` (covers
~/.hermes/.env env vars and the credential pool in auth.json) when importable,
then a plain env-var fallback so the plugin also works standalone.

Quota response (``code``/``msg``/``success`` envelope around ``data``)::

    {"code": 200, "msg": "Operation successful", "success": true,
     "data": {"level": "pro", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5,
         "usage": 800000000, "currentValue": 127694464,
         "remaining": 672305536, "percentage": 15,
         "nextResetTime": 1770648402389},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 53,
         "nextResetTime": 1772272043542},
        {"type": "TIME_LIMIT", "unit": 5, "number": 1,
         "usage": 4000, "currentValue": 1828, "remaining": 2172,
         "percentage": 45, "nextResetTime": 1773596236982,
         "usageDetails": [{"modelCode": "search-prime", "usage": 1433},
                          {"modelCode": "web-reader", "usage": 462}]}]}}

Mapping rules (window identity comes from ``unit``, never array position —
Z.ai is free to reorder ``limits``):

* ``TOKENS_LIMIT`` (or the newer ``CREDIT_LIMIT`` alias) with ``unit: 3`` is
  the 5-hour rolling session window → ``Session``;
* ``unit: 6`` is the multi-day window → ``Weekly``;
* ``TIME_LIMIT`` is the monthly web-search/reader/Zread call allowance →
  ``Monthly web tools`` — a percent bar only when a positive limit exists,
  otherwise the counts land in ``details`` (never a fake percent);
* ``percentage`` is the server-reported used percent (0-100); when absent it
  is computed from ``currentValue``/``usage`` (real denominator);
* ``nextResetTime`` is epoch milliseconds (defensively also accepted: epoch
  seconds and ISO-8601 strings); 0/None mean absent;
* ``data.level`` is the plan tier (lite/pro/max), best-effort only.

Fail-open contract: a 200 envelope with ``success: false`` (verified live:
what a valid key without an active Coding Plan receives) → ``no-subscription``;
schema surprises or no usable limits → ``no-data``; never a fabricated zero
and never an exception.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

PROVIDER_ID = "zai"
_QUOTA_PATH = "/api/monitor/usage/quota/limit"
_SUBSCRIPTION_PATH = "/api/biz/subscription/list"
_DEFAULT_API_ROOT = "https://api.z.ai"
# Same env vars Hermes core's zai overlay checks, in priority order.
_ENV_KEYS = ("ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY")

_TOKEN_LIMIT_TYPES = ("TOKENS_LIMIT", "CREDIT_LIMIT")
_UNIT_SESSION = 3  # hours bucket → 5-hour rolling window
_UNIT_WEEKLY = 6  # day/week bucket → multi-day window
# TIME_LIMIT entries carry unit 5 (month); the parser stays tolerant and does
# not require it, so a renamed unit cannot hide the tool allowance.


# -- credential resolution ----------------------------------------------------


def _env_api_key() -> Optional[str]:
    for name in _ENV_KEYS:
        value = os.environ.get(name)
        if not value:
            continue
        trimmed = value.strip().strip("\"'")
        if trimmed:
            return trimmed
    return None


def resolve_api_key() -> Optional[str]:
    """Resolve the Z.ai API key the way Hermes core does, or None.

    Core's resolver covers ``~/.hermes/.env`` env vars plus the credential
    pool in auth.json (where a connected Z.ai key lives). Only a missing core
    (standalone install) falls through to this module's env-var check.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, _resolve_api_key_provider_secret
        pconfig = PROVIDER_REGISTRY.get(PROVIDER_ID)
        if pconfig is not None and getattr(pconfig, "auth_type", "") == "api_key":
            key, _source = _resolve_api_key_provider_secret(PROVIDER_ID, pconfig)
            if key:
                return key
    except Exception:  # noqa: BLE001 - standalone install / locked store
        pass
    return _env_api_key()


def _api_root() -> str:
    """Monitor API root. The China mirror serves the same paths."""
    override = os.environ.get("ZAI_MONITOR_BASE_URL", "").strip().rstrip("/")
    return override or _DEFAULT_API_ROOT


# -- tolerant parsing -----------------------------------------------------------


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


def _parse_reset(value: Any) -> Optional[str]:
    """Normalize nextResetTime (epoch ms / epoch s / ISO-8601) to ISO-8601 UTC."""
    number = _as_float(value)
    if number is not None:
        if number <= 0:
            return None
        if number < 100_000_000_000:  # seconds, not milliseconds
            number *= 1000.0
        try:
            return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    return None


def _used_percent(entry: dict) -> Optional[float]:
    """Server-reported percentage, else currentValue/usage with a real denominator."""
    percent = _as_float(entry.get("percentage"))
    if percent is None:
        used = _as_float(entry.get("currentValue"))
        limit = _as_float(entry.get("usage"))
        if used is None or limit is None or limit <= 0:
            return None
        percent = used / limit * 100.0
    if percent < 0:  # a negative server value is a schema surprise, not -3% used
        return None
    return max(0.0, min(100.0, percent))


def _format_count(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:g}k"
    return f"{value:g}"


def parse_quota_payload(data: Any) -> QuotaResult:
    """Map the quota/limit envelope onto Session/Weekly/Monthly-web-tools windows.

    Returns a QuotaResult even for unusable payloads (``unavailable_reason``
    set); never raises.
    """
    if not isinstance(data, dict):
        return build_unavailable(PROVIDER_ID, "no-data")
    if data.get("success") is False:
        # Verified live: a valid key without an active Coding Plan gets a
        # 200 envelope whose success flag is false.
        return build_unavailable(PROVIDER_ID, "no-subscription")
    inner = data.get("data")
    if not isinstance(inner, dict) or not isinstance(inner.get("limits"), list):
        return build_unavailable(PROVIDER_ID, "no-data")

    windows: list[QuotaWindow] = []
    details: list[str] = []
    session = weekly = monthly = None

    for entry in inner.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "").strip().upper()
        unit = _as_float(entry.get("unit"))
        reset = _parse_reset(entry.get("nextResetTime"))
        if kind in _TOKEN_LIMIT_TYPES and unit is not None:
            percent = _used_percent(entry)
            if percent is None:
                continue
            if unit == _UNIT_SESSION:
                session = QuotaWindow(label="Session", used_percent=round(percent, 2), reset_at=reset)
                used = _as_float(entry.get("currentValue"))
                if used is not None and used > 0:
                    details.append(f"Session tokens used: {_format_count(used)}")
            elif unit == _UNIT_WEEKLY:
                weekly = QuotaWindow(label="Weekly", used_percent=round(percent, 2), reset_at=reset)
        elif kind == "TIME_LIMIT":
            used = _as_float(entry.get("currentValue"))
            limit = _as_float(entry.get("usage"))
            percent = _used_percent(entry)
            if percent is not None and limit is not None and limit > 0:
                monthly = QuotaWindow(
                    label="Monthly web tools", used_percent=round(percent, 2), reset_at=reset
                )
                breakdown = [
                    (str(item.get("modelCode") or "").strip(), _as_float(item.get("usage")))
                    for item in (entry.get("usageDetails") or [])
                    if isinstance(item, dict)
                ]
                parts = [f"{name} {int(count)}" for name, count in breakdown if name and count]
                suffix = f" ({', '.join(parts)})" if parts else ""
                remaining = _as_float(entry.get("remaining"))
                left = f", {int(remaining)} left" if remaining is not None else ""
                details.append(
                    f"Web tools: {int(used)} of {int(limit)} this month{left}{suffix}"
                    if used is not None
                    else f"Web tools limit: {int(limit)} this month{suffix}"
                )
            elif used is not None and used >= 0:
                # No positive limit → no honest percent bar; counts as details only.
                details.append(f"Web tools used this month: {int(used)} (no limit reported)")

    if session is not None:
        windows.append(session)
    if weekly is not None:
        windows.append(weekly)
    if monthly is not None:
        windows.append(monthly)

    if not windows:
        return build_unavailable(PROVIDER_ID, "no-data")

    plan = _plan_label(inner.get("level"))
    return QuotaResult(
        label=PROVIDER_ID, windows=windows, plan=plan, unavailable_reason=None, details=details
    )


def _plan_label(level: Any) -> Optional[str]:
    """data.level (lite/pro/max) as a display label; never invented."""
    if not isinstance(level, str):
        return None
    text = level.strip()
    if not text:
        return None
    if text.lower() in ("lite", "pro", "max"):
        return text.capitalize()
    return text


def _subscription_plan(payload: Any) -> tuple[Optional[str], Optional[str]]:
    """(plan, renew_iso) from the optional subscription/list endpoint; best-effort."""
    if not isinstance(payload, dict):
        return None, None
    items = payload.get("data")
    if isinstance(items, dict):  # tolerate a single-object shape
        items = [items]
    if not isinstance(items, list):
        return None, None
    for item in items:
        if not isinstance(item, dict):
            continue
        valid = item.get("valid")
        status = str(item.get("status") or "").strip().upper()
        if valid is False or (status and status not in ("ACTIVE", "NORMAL", "OK")):
            continue
        name = str(item.get("productName") or item.get("name") or "").strip()
        renew = _parse_reset(item.get("renewTime") or item.get("expireTime"))
        if name:
            return name, renew
    return None, None


# -- network --------------------------------------------------------------------


def _get_json(url: str, api_key: str) -> Any:
    """GET ``url`` with the bearer key; raises on HTTP/parse failure."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "hermes-quota-plugin",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=15) as resp:
        raw = resp.read()
    return json.loads(raw)


def _http_reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "auth-failed"
        return f"http-{exc.code}"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "bad-json"
    return f"fetch-error:{type(exc).__name__}"


def fetch_zai_quota() -> QuotaResult:
    api_key = resolve_api_key()
    if not api_key:
        return build_unavailable(PROVIDER_ID, "no-credentials")

    root = _api_root()
    try:
        quota_payload = _get_json(f"{root}{_QUOTA_PATH}", api_key)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return build_unavailable(PROVIDER_ID, _http_reason(exc))

    try:
        result = parse_quota_payload(quota_payload)
    except Exception:  # noqa: BLE001 - schema surprise, never raise
        return build_unavailable(PROVIDER_ID, "no-data")
    if result.unavailable_reason is not None:
        return result

    # Optional plan-name enrichment; a failure here never blanks the meters.
    try:
        subscription = _get_json(f"{root}{_SUBSCRIPTION_PATH}", api_key)
        plan, renew = _subscription_plan(subscription)
    except Exception:  # noqa: BLE001 - best-effort
        plan, renew = None, None
    if plan:
        result.plan = plan
    if renew:
        result.details.append(f"Renews: {renew}")
    return result


from .registry import register as _register  # noqa: E402

_register(PROVIDER_ID)(fetch_zai_quota)
