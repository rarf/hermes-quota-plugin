"""Built-in provider quota fetchers and their normalized adapters.

OpenAI Codex, Nous, and OpenRouter reuse the core account-usage path. Anthropic
is fetched directly because its single OAuth payload contains both the legacy
windows and newer model-scoped limits. We adapt those snapshots into the
plugin's QuotaResult shape and register them so the cache builder treats them
uniformly with the other fetchers.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable
from .registry import register as _register


def _snapshot_to_result(snapshot) -> QuotaResult:
    provider = getattr(snapshot, "provider", "unknown")
    windows = []
    for w in getattr(snapshot, "windows", ()) or ():
        used = getattr(w, "used_percent", None)
        reset = getattr(w, "reset_at", None)
        reset_iso = None
        if reset is not None:
            from datetime import datetime, timezone

            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
            reset_iso = reset.isoformat()
        windows.append(
            QuotaWindow(
                label=str(getattr(w, "label", "") or "window"),
                used_percent=float(used) if used is not None else None,
                reset_at=reset_iso,
            )
        )
    return QuotaResult(
        label=str(provider),
        windows=windows,
        plan=getattr(snapshot, "plan", None),
        unavailable_reason=getattr(snapshot, "unavailable_reason", None),
        details=[str(d) for d in (getattr(snapshot, "details", ()) or ())],
    )


def _make_fetcher(provider_id: str):
    def _fetch() -> QuotaResult:
        try:
            from agent.account_usage import fetch_account_usage
        except Exception:
            return build_unavailable(provider_id, "fetcher-unavailable")
        try:
            snap = fetch_account_usage(provider_id)
        except Exception:
            return build_unavailable(provider_id, "fetch-error")
        if snap is None:
            return build_unavailable(provider_id, "no-data")
        return _snapshot_to_result(snap)

    return _fetch


def _core_anthropic_token() -> Optional[str]:
    """Resolvable Anthropic token per core auth, or None. Never raises."""
    try:
        from agent.anthropic_credentials import resolve_anthropic_token

        token = (resolve_anthropic_token() or "").strip()
        return token or None
    except Exception:  # noqa: BLE001 - standalone install / locked store
        return None


def _core_anthropic_is_oauth(token: str) -> Optional[bool]:
    """Use the core token classifier when it is available."""
    try:
        from agent.anthropic_adapter import _is_oauth_token

        return bool(_is_oauth_token(token))
    except Exception:
        # Standalone plugin installs may not ship the core classifier; the
        # vendor response remains the source of truth in that case.
        return None


_ANTHROPIC_OAUTH_REQUIRED_REASON = (
    "Anthropic account limits are only available for OAuth-backed Claude accounts."
)


_ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Keep one direct usage read below the cache refresh budget. The response is
# parsed for both the legacy top-level windows and the newer ``limits`` list.
_ANTHROPIC_TIMEOUT_S = 15.0


def parse_anthropic_scoped_limits(payload: Any) -> list[QuotaWindow]:
    """Model-scoped and all-model weekly limits from ``/api/oauth/usage``.

    Core maps only the fixed ``five_hour``/``seven_day*`` keys. Newer models
    get opaque codenamed keys instead; the display name lives in the
    ``limits`` list::

        {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
         "resets_at": "2026-09-30T08:00:00+00:00",
         "scope": {"model": {"id": null, "display_name": "Fable"}, "surface": null}}
    """
    limits = payload.get("limits") if isinstance(payload, dict) else None
    windows: list[QuotaWindow] = []
    seen: set[str] = set()
    for entry in limits if isinstance(limits, list) else ():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in {"session", "weekly_all", "weekly_scoped"}:
            continue
        percent = entry.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        percent = float(percent)
        if not math.isfinite(percent):
            continue

        if kind == "session":
            label = "Current session"
        elif kind == "weekly_all":
            label = "Current week"
        else:
            scope = entry.get("scope") or {}
            model = scope.get("model") if isinstance(scope, dict) else None
            if not isinstance(model, dict):
                model = {}
            name = model.get("display_name") or model.get("id")
            if not isinstance(name, str) or not name.strip():
                surface = scope.get("surface") if isinstance(scope, dict) else None
                name = surface
            if not isinstance(name, str) or not name.strip():
                continue
            label = f"{name.strip()} week"

        if label in seen:
            continue
        seen.add(label)
        reset = entry.get("resets_at")
        if isinstance(reset, str) and reset.endswith("Z"):
            reset = reset[:-1] + "+00:00"
        windows.append(QuotaWindow(
            label=label,
            used_percent=max(0.0, min(100.0, percent)),
            reset_at=reset if isinstance(reset, str) and reset else None,
        ))
    return windows


def _anthropic_usage_payload() -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Fetch the OAuth usage payload once, including all supported shapes."""
    token = _core_anthropic_token()
    if not token:
        return None, "no-credentials"
    if _core_anthropic_is_oauth(token) is False:
        return None, _ANTHROPIC_OAUTH_REQUIRED_REASON
    request = urllib.request.Request(
        _ANTHROPIC_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_ANTHROPIC_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "auth-failed"
        return None, f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return None, f"fetch-error:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "bad-json"
    return payload, None


def _parse_anthropic_usage(payload: dict[str, Any]) -> tuple[list[QuotaWindow], list[str]]:
    windows: list[QuotaWindow] = []
    seen: set[str] = set()
    for key, label in (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    ):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        utilization = window.get("utilization")
        if isinstance(utilization, bool):
            continue
        try:
            used = float(utilization)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(used):
            continue
        if used <= 1:
            used *= 100
        if label in seen:
            continue
        seen.add(label)
        reset = window.get("resets_at")
        if isinstance(reset, str) and reset.endswith("Z"):
            reset = reset[:-1] + "+00:00"
        windows.append(QuotaWindow(
            label=label,
            used_percent=max(0.0, min(100.0, used)),
            reset_at=reset if isinstance(reset, str) and reset else None,
        ))

    for window in parse_anthropic_scoped_limits(payload):
        if window.label not in seen:
            seen.add(window.label)
            windows.append(window)

    details: list[str] = []
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = extra.get("used_credits")
        monthly = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if (
            isinstance(used, (int, float))
            and not isinstance(used, bool)
            and isinstance(monthly, (int, float))
            and not isinstance(monthly, bool)
        ):
            details.append(f"Extra usage: {used:.2f} / {monthly:.2f} {currency}")
    return windows, details


def _fetch_anthropic() -> QuotaResult:
    """Fetch and parse Anthropic's usage payload in one network request."""
    try:
        payload, reason = _anthropic_usage_payload()
    except ImportError:
        return build_unavailable("anthropic", "fetcher-unavailable")
    except Exception:
        return build_unavailable("anthropic", "fetch-error")
    if payload is None:
        return build_unavailable("anthropic", reason or "no-data")

    windows, details = _parse_anthropic_usage(payload)
    if not windows and not details:
        return build_unavailable("anthropic", "no-data")
    return QuotaResult(
        label="anthropic",
        windows=windows,
        plan=None,
        unavailable_reason=None,
        details=details,
    )


for _pid in ("openrouter",):
    _register(_pid)(_make_fetcher(_pid))

_register("anthropic")(_fetch_anthropic)


# -- Nous Portal (direct account-info adapter) --------------------------------
# The core ``fetch_account_usage`` dispatcher does not route "nous" (only
# openai-codex / anthropic / openrouter), so the generic adapter above always
# produced ``no-data`` for it — for every account, paid or not. This fetcher
# reads the Portal account model directly (the same Hermes-managed OAuth state
# ``hermes portal status`` shows) and mirrors the semantics of the proposed
# ``portal usage --json`` contract (upstream hermes-agent PR #77791):
#   * a usage percentage only ever appears with a real positive denominator;
#   * free accounts render an honest status card (plan + free tool pool +
#     published rate ceiling) instead of fabricated zeros.


def _finite_usd(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nous_claim(account_info, name):
    claims = getattr(account_info, "raw_claims", None)
    return claims.get(name) if isinstance(claims, dict) else None


def _format_rate_limit(value) -> Optional[str]:
    number = _finite_usd(value)
    if number is None or number < 0:
        return None
    if number >= 1_000_000:
        return f"{number / 1_000_000:g}M"
    if number >= 1_000:
        return f"{number / 1_000:g}k"
    return f"{number:g}"


def _nous_tool_pool_labels(account_info) -> list[str]:
    """Names of the free Tool-Gateway categories this account may use."""
    try:
        import dataclasses

        coverage = getattr(getattr(account_info, "tool_access", None), "coverage", None)
        if coverage is None:
            return []
        if isinstance(coverage, dict):
            items = list(coverage.items())
        elif dataclasses.is_dataclass(coverage):
            items = [(f.name, getattr(coverage, f.name)) for f in dataclasses.fields(coverage)]
        else:
            return []
        pretty = {"browser_use": "browser-use", "fal_video": "fal-video", "openai_audio": "openai-audio"}
        return sorted(pretty.get(str(k), str(k)) for k, v in items if v is True)
    except Exception:
        return []


def _credit_line(access, attr, label) -> Optional[str]:
	"""Return a '$X.XX' detail line when the attribute is a finite USD amount."""
	amount = _finite_usd(getattr(access, attr, None))
	if amount is None:
		return None
	return f"{label}: ${amount:.2f}"


def _fetch_nous_portal() -> QuotaResult:
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        account = get_nous_portal_account_info()
    except Exception:
        return build_unavailable("nous", "fetcher-unavailable")
    if account is None or not getattr(account, "logged_in", False):
        return build_unavailable("nous", "not-logged-in")

    access = getattr(account, "paid_service_access_info", None)
    sub = getattr(account, "subscription", None)
    paid = getattr(account, "paid_service_access", None)

    windows: list[QuotaWindow] = []
    details: list[str] = []

    # Subscription gauge — only with a positive monthly denominator and a sane
    # remaining value (remaining > cap means rollover across periods, where the
    # monthly number stops being a meaningful denominator).
    monthly = _finite_usd(getattr(sub, "monthly_credits", None)) if sub is not None else None
    remaining = _finite_usd(getattr(sub, "credits_remaining", None)) if sub is not None else None
    if monthly is not None and monthly > 0 and remaining is not None and remaining <= monthly:
        used_pct = max(0.0, min(100.0, (monthly - remaining) / monthly * 100.0))
        windows.append(QuotaWindow(label="Subscription", used_percent=round(used_pct, 2)))
        details.append(f"${remaining:.2f} of ${monthly:.2f} subscription credits left")

    if access is not None:
        for attr, label in (
            ("subscription_credits_remaining", "Subscription credits"),
            ("purchased_credits_remaining", "Top-up credits"),
            ("total_usable_credits", "Total usable"),
        ):
            line = _credit_line(access, attr, label)
            if line is not None:
                details.append(line)

    if sub is not None:
        rollover = _finite_usd(getattr(sub, "rollover_credits", None))
        if rollover is not None and rollover > 0:
            details.append(f"Rollover: ${rollover:.2f}")
        period_end = getattr(sub, "current_period_end", None)
        if period_end:
            details.append(f"Renews: {period_end}")

    plan = (getattr(sub, "plan", None) if sub is not None else None) or None

    # Some paid Portal accounts expose spend and subscription rate limits in
    # raw claims without a subscription object or credit cap. These are useful
    # details, but spend is not a quota denominator and must not become a
    # percentage window.
    member_spend = _finite_usd(_nous_claim(account, "member_spend_usd"))
    member_spend_cap = _finite_usd(_nous_claim(account, "member_spend_cap_usd"))
    if member_spend is not None:
        suffix = " (no cap reported)" if member_spend_cap is None else ""
        details.append(f"Spend this period: ${member_spend:.2f}{suffix}")

    tier = _finite_usd(_nous_claim(account, "subscription_tier"))
    if plan is None and tier is not None and tier >= 0 and tier.is_integer():
        plan = f"Tier {int(tier)}"

    rate_limits = []
    for claim, unit in (("rate_limit_rpm", "RPM"), ("rate_limit_tpm", "TPM"), ("rate_limit_rph", "RPH")):
        formatted = _format_rate_limit(_nous_claim(account, claim))
        if formatted is not None:
            rate_limits.append(f"{formatted} {unit}")
    if rate_limits:
        details.append("Rate limits: " + " · ".join(rate_limits))

    if not windows and not details:
        if paid is False:
            # Free tier: the portal exposes no credit/usage numbers at all
            # (verified against a live free account). Show what IS true.
            details.append("Free tier - free models only")
            details.append("Rate ceiling: 50 RPM / 500k TPM (published)")
            pool = _nous_tool_pool_labels(account)
            if pool:
                details.append("Tool pool: " + ", ".join(pool))
            plan = plan or "Free"
        else:
            return build_unavailable("nous", "no-data")
    elif paid is False:
        details.append("Status: access depleted - top up to restore")

    return QuotaResult(label="nous", windows=windows, plan=plan, details=details)


_register("nous")(_fetch_nous_portal)


# -- OpenAI Codex (with per-model Spark limits) ------------------------------
# The core fetcher covers plan-level Session/Weekly windows, but drops
# ``additional_rate_limits`` — the per-model quotas (e.g. GPT-5.3-Codex-Spark)
# the Codex backend reports alongside them. This fetcher reuses the core
# credential resolution and parses the raw payload so Spark windows surface.

def _fetch_codex_with_models() -> QuotaResult:
    try:
        from agent.account_usage import _resolve_codex_usage_credentials
        try:
            from agent.account_usage import _codex_backend_urls
        except ImportError:
            _codex_backend_urls = None
        try:
            from agent.account_usage import _resolve_codex_usage_url
        except ImportError:
            _resolve_codex_usage_url = None
        if _codex_backend_urls is None and _resolve_codex_usage_url is None:
            raise ImportError("no Codex usage URL helper")
    except Exception:
        return build_unavailable("openai-codex", "fetcher-unavailable")

    import httpx

    try:
        token, base_url, account_id = _resolve_codex_usage_credentials(None, None)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        if _codex_backend_urls is not None:
            usage_url = _codex_backend_urls(base_url)[0]
        else:
            usage_url = _resolve_codex_usage_url(base_url)
        with httpx.Client(timeout=15.0) as client:
            response = client.get(usage_url, headers=headers)
            response.raise_for_status()
        payload = response.json() or {}
    except Exception:
        return build_unavailable("openai-codex", "fetch-error")

    from datetime import datetime, timezone

    def _iso(ts):
        if not isinstance(ts, (int, float)):
            return None
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

    def _window(raw: dict, label: str) -> Optional[QuotaWindow]:
        used = raw.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            return None
        return QuotaWindow(
            label=label,
            used_percent=float(used),
            reset_at=_iso(raw.get("reset_at")),
        )

    windows: list[QuotaWindow] = []
    rate_limit = payload.get("rate_limit") or {}
    for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        w = _window(rate_limit.get(key) or {}, label)
        if w is not None:
            windows.append(w)

    # Per-model limits (research-preview models like Codex Spark).
    for extra in payload.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        model_name = str(extra.get("limit_name") or "").strip()
        if not model_name:
            continue
        short = model_name.replace("GPT-", "").replace("-Codex-", " Codex ")
        inner = extra.get("rate_limit") or {}
        for key, label in (("primary_window", "5h"), ("secondary_window", "Weekly")):
            w = _window(inner.get(key) or {}, f"{short} · {label}")
            if w is not None:
                windows.append(w)

    details: list[str] = []
    reset_credits = payload.get("rate_limit_reset_credits") or {}
    banked = reset_credits.get("available_count")
    if isinstance(banked, (int, float)) and int(banked) > 0:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(f"You have {count} reset{plural} banked - use /usage reset to activate")
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")

    plan = str(payload.get("plan_type") or "").strip()
    plan = plan.title() if plan else None
    if not windows and not details:
        return build_unavailable("openai-codex", "no-data")
    return QuotaResult(
        label="openai-codex",
        windows=windows,
        plan=plan,
        unavailable_reason=None,
        details=details,
    )


_register("openai-codex")(_fetch_codex_with_models)
