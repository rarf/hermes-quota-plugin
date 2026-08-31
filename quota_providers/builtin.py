"""Built-in provider quota fetchers reusing the core account-usage path.

OpenAI Codex, Anthropic, Nous, and OpenRouter already expose quota via
``agent.account_usage.fetch_account_usage`` (shipped in core).  We adapt those
snapshots into the plugin's QuotaResult shape and register them so the cache
builder treats them uniformly with the Grok/Gemini/Kimi fetchers.
"""

from __future__ import annotations

import math
from typing import Optional

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


for _pid in ("anthropic", "openrouter"):
    _register(_pid)(_make_fetcher(_pid))


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
        from agent.account_usage import (
            _resolve_codex_usage_credentials,
            _resolve_codex_usage_url,
        )
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
        with httpx.Client(timeout=15.0) as client:
            response = client.get(_resolve_codex_usage_url(base_url), headers=headers)
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
