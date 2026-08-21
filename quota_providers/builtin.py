"""Built-in provider quota fetchers reusing the core account-usage path.

OpenAI Codex, Anthropic, Nous, and OpenRouter already expose quota via
``agent.account_usage.fetch_account_usage`` (shipped in core).  We adapt those
snapshots into the plugin's QuotaResult shape and register them so the cache
builder treats them uniformly with the Grok/Gemini/Kimi fetchers.
"""

from __future__ import annotations

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


for _pid in ("anthropic", "nous", "openrouter"):
    _register(_pid)(_make_fetcher(_pid))


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
