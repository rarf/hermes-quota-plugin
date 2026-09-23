"""Kimi (Kimi For Coding) quota fetcher — plugin standalone copy.

Quota endpoint (verified live 2026-09-23 with a Hermes-managed ``sk-kimi-*`` key)::

    GET https://api.kimi.com/coding/v1/usages
    -> {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"limit": "100", "used": "57", "remaining": "43",
                               "resetTime": "2026-09-23T04:08:05.435444Z"}}],
        "usages": {"limit_5h":         {"used_ratio": 0,      "reset_time": "..."},
                   "limit_month_total": {"used_ratio": 0.1559, "reset_time": "..."},
                   "limit_month_code":  {"used_ratio": 0,      "reset_time": "..."}}}

Credential resolution order (fail-open at every step):

1. Hermes-managed ``kimi-coding`` auth — ``hermes_cli.auth`` covers
   ``~/.hermes/.env`` env vars and the credential pool in auth.json (the same
   source ``hermes auth list kimi-coding`` reports), including the
   sk-kimi-* → api.kimi.com/coding base-URL redirect;
2. legacy ``~/kimi_session.json`` (``api_key`` / ``token`` keys) so standalone
   installs keep working.

Fail-open contract: 401/403 → ``auth-failed``; other HTTP → ``http-<code>``;
transport/parse failures → ``fetch-error:*``/``bad-json``; a 200 without any
parseable window → ``no-data``; no credential anywhere → ``no-credentials``.
Never raises, never logs the key.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_DEFAULT_BASE_URL = "https://api.kimi.com/coding"
_WEB_URL = "https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages"
_SESSION_PATH = os.path.join(os.path.expanduser("~"), "kimi_session.json")

# Server keys of the `usages` map -> display label. Unknown keys fall back to a
# humanized slug so a renamed/added plan window still shows up.
_USAGE_KEY_LABELS = {
    "limit_5h": "Session (5h)",
    "limit_month_total": "Monthly total",
    "limit_month_code": "Monthly code",
}


def _load_creds() -> tuple[Optional[str], Optional[str]]:
    """Legacy session file -> (api_key, web_token)."""
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None, None
    if isinstance(data, dict):
        return data.get("api_key"), data.get("token")
    return None, None


def _load_hermes_creds() -> tuple[Optional[str], Optional[str]]:
    """(api_key, base_url) from Hermes-managed kimi-coding auth, else (None, None).

    Mirrors the zai fetcher: core's resolver covers dotenv + credential pool;
    an unimportable core (standalone install) degrades to the session file.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, _resolve_api_key_provider_secret
        try:
            from hermes_cli.config import get_env_value_prefer_dotenv
        except ImportError:
            get_env_value_prefer_dotenv = None
        try:
            from hermes_cli.auth import _resolve_kimi_base_url
        except ImportError:
            _resolve_kimi_base_url = None
        pconfig = PROVIDER_REGISTRY.get("kimi-coding")
        if pconfig is None or getattr(pconfig, "auth_type", "") != "api_key":
            return None, None
        key, _source = _resolve_api_key_provider_secret("kimi-coding", pconfig)
        if not key:
            return None, None
        env_url = ((get_env_value_prefer_dotenv(pconfig.base_url_env_var)
                    if get_env_value_prefer_dotenv is not None and pconfig.base_url_env_var
                    else os.environ.get("KIMI_BASE_URL", "")) or "").strip()
        if _resolve_kimi_base_url is not None:
            base = _resolve_kimi_base_url(key, pconfig.inference_base_url, env_url)
        else:  # older core without the helper: replicate the redirect
            base = env_url or (
                _DEFAULT_BASE_URL if key.startswith("sk-kimi-") else pconfig.inference_base_url)
        return key, (base or _DEFAULT_BASE_URL)
    except Exception:  # noqa: BLE001 - standalone install / locked store
        return None, None


def _usages_url(base_url: Optional[str]) -> str:
    """``{base}/v1/usages`` without doubling a ``/v1`` suffix the base already has."""
    base = (base_url or "").strip().rstrip("/") or _DEFAULT_BASE_URL
    return base + "/usages" if base.endswith("/v1") else base + "/v1/usages"


def _as_float(value) -> Optional[float]:
    """Number from int/float/numeric-string; None for anything else."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _rate_label(window: dict) -> str:
    """Human label for a rate-window dict like {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}."""
    duration = _as_float(window.get("duration")) if isinstance(window, dict) else None
    unit = str((window or {}).get("timeUnit") or "").upper() if isinstance(window, dict) else ""
    if duration and duration > 0:
        minutes = int(duration) if "MINUTE" in unit else int(duration * 60) if "HOUR" in unit else None
        if minutes is not None:
            if minutes >= 60 and minutes % 60 == 0:
                return f"Rate ({minutes // 60}h)"
            return f"Rate ({minutes}m)"
        if "SECOND" in unit:
            return f"Rate ({int(duration)}s)"
        return f"Rate ({int(duration)} {unit.lower().removeprefix('time_unit_') or 'window'})"
    return "Rate limit"


def _parse_block(block: dict) -> QuotaWindow:
    scope = block.get("scope")
    window = block.get("window")
    if isinstance(scope, str) and scope.strip():
        label = scope
    elif isinstance(window, str) and window.strip():
        label = window
    elif isinstance(window, dict):
        label = _rate_label(window)
    else:
        label = "window"
    detail = block.get("detail") or {}
    limit = _as_float(block.get("limit", detail.get("limit")))
    used = _as_float(block.get("used", detail.get("used")))
    remaining = _as_float(block.get("remaining", detail.get("remaining")))
    reset = block.get("resetTime", detail.get("resetTime"))
    used_pct: Optional[float] = None
    if used is not None and limit not in (None, 0):
        used_pct = round(100.0 * used / limit, 2)
    if remaining is not None and limit not in (None, 0):
        used_pct = round(100.0 * (1 - remaining / limit), 2)
    return QuotaWindow(label=str(label), used_percent=used_pct, reset_at=reset)


def _parse_usage_entry(key: str, entry: dict) -> Optional[QuotaWindow]:
    """One ``usages`` map entry (ratio-based plan window) -> QuotaWindow."""
    if not isinstance(entry, dict):
        return None
    ratio = _as_float(entry.get("used_ratio"))
    if ratio is None:
        return None
    label = _USAGE_KEY_LABELS.get(key) or key.replace("limit_", "").replace("_", " ").title()
    return QuotaWindow(label=label, used_percent=round(ratio * 100.0, 2),
                       reset_at=entry.get("reset_time"))


def _fetch_with(headers: dict, url: str, method: str = "GET", body: Optional[bytes] = None) -> Optional[QuotaResult]:
    req = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return build_unavailable("kimi", "auth-failed")
        return build_unavailable("kimi", f"http-{e.code}")
    except Exception as e:
        return build_unavailable("kimi", f"fetch-error:{type(e).__name__}")
    try:
        data = json.loads(raw)
    except Exception:
        return build_unavailable("kimi", "bad-json")
    windows: list[QuotaWindow] = []
    usages = data.get("usages")
    if isinstance(usages, dict):
        for key, entry in usages.items():
            parsed = _parse_usage_entry(str(key), entry)
            if parsed is not None:
                windows.append(parsed)
    if isinstance(data.get("usage"), dict):
        windows.append(_parse_block(data["usage"]))
    limits = data.get("limits")
    if isinstance(limits, list):
        for blk in limits:
            if isinstance(blk, dict):
                windows.append(_parse_block(blk))
    elif isinstance(limits, dict):
        windows.append(_parse_block(limits))
    if not windows:
        return build_unavailable("kimi", "no-data")
    return QuotaResult(label="kimi", windows=windows, plan=None, unavailable_reason=None)


def fetch_kimi_quota() -> QuotaResult:
    api_key, base_url = _load_hermes_creds()
    if api_key:
        return _fetch_with({"Authorization": f"Bearer {api_key}"},
                           _usages_url(base_url), "GET") or build_unavailable("kimi", "no-data")
    legacy_key, token = _load_creds()
    if legacy_key:
        return _fetch_with({"Authorization": f"Bearer {legacy_key}"},
                           _usages_url(_DEFAULT_BASE_URL), "GET") or build_unavailable("kimi", "no-data")
    if token:
        return _fetch_with({"Authorization": f"Bearer {token}"}, _WEB_URL, "POST", b"{}") or build_unavailable("kimi", "no-data")
    return build_unavailable("kimi", "no-credentials")


from .registry import register as _register  # noqa: E402

_register("kimi")(fetch_kimi_quota)
