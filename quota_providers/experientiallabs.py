"""Experiential Labs account balance and recent usage via the Cost / Account API.

GET /api/v1/credits returns ``{data: {total_credits, total_usage}}`` in USD;
remaining = total_credits - total_usage.
GET /api/v1/usage returns settled per-request rows for recent activity.

This provider is not yet in Hermes core so credentials are resolved from the
environment (``EXPLABS_API_KEY``).
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Optional

from .api_keys import amount as _amount, get_json as _get_json
from .base import AccountBalance, Deadline, QuotaResult, build_unavailable
from .registry import register

PROVIDER_ID = "experientiallabs"
_BASE_URL = "https://api.experientiallabs.ai/api/v1"
_CREDITS_PATH = "/credits"
_USAGE_PATH = "/usage"
# Env vars the docs reference; EXPLABS_API_KEY is canonical.
_ENV_KEYS = ("EXPLABS_API_KEY",)
_FETCH_BUDGET_S = 10.0
_HTTP_TIMEOUT_S = 7.0
_MAX_BODY = 1024 * 1024
_USAGE_ROWS = 5


# -- credential resolution ----------------------------------------------------


def _env_api_key() -> Optional[str]:
    """Scan known env var names for an Experiential Labs API key."""
    for name in _ENV_KEYS:
        value = os.environ.get(name)
        if not value:
            continue
        trimmed = value.strip().strip("\"'")
        if trimmed:
            return trimmed
    return None


def resolve_api_key() -> Optional[str]:
    """Resolve the Experiential Labs key from Hermes core or the environment.

    Tries Hermes' registry first (so a future core update that adds the provider
    works automatically), falls back to the environment so a standalone install
    still works.
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


# -- fetcher -------------------------------------------------------------------


@register(PROVIDER_ID)
def fetch_experientiallabs_quota() -> QuotaResult:
    try:
        secret = resolve_api_key()
    except (ImportError, RuntimeError):  # noqa: BLE001 - missing core / locked store
        return build_unavailable(PROVIDER_ID, "no-credentials")
    except Exception:  # noqa: BLE001 - unexpected resolver failure
        return build_unavailable(PROVIDER_ID, "fetch-error")
    if not secret:
        return build_unavailable(PROVIDER_ID, "no-credentials")

    try:
        return _fetch(secret)
    except Exception:  # noqa: BLE001 - unexpected fetch failure
        return build_unavailable(PROVIDER_ID, "fetch-error")


def _safe_int(value) -> int | None:
    """Parse a token field returning None on any non-coercible value.

    ``int(row.get("input_tokens") or 0)`` raises ``TypeError`` on non-numeric
    values.  We tolerate a missing/None field (counts as 0) and reject anything
    that would raise or produce a fractional value; returning None means "skip
    this row's token detail rather than lose the entire balance".
    """
    if value is None or value is True or value is False:
        return 0
    try:
        v = int(str(value).strip())
    except (ValueError, TypeError):
        return None
    return v


def _fetch(secret: str) -> QuotaResult:
    """Inner fetch body with its own exception guard via the caller."""
    from decimal import Decimal as _Decimal

    deadline = Deadline(_FETCH_BUDGET_S)
    details: list[str] = []
    account_balances: list[AccountBalance] = []
    has_data = False

    # -- 1. Credits (wallet) --------------------------------------------------
    if not deadline.expired():
        credits_url = _BASE_URL + _CREDITS_PATH
        payload, error = _get_json(credits_url, secret, deadline.slice(_HTTP_TIMEOUT_S))
        if error:
            details.append(f"Credits: unavailable ({error})")
        elif not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            details.append("Credits: parse-pending")
        else:
            data = payload["data"]
            total = _amount(data.get("total_credits"))
            used = _amount(data.get("total_usage"))
            if total is not None and used is not None:
                total_dec = _Decimal(total)
                used_dec = _Decimal(used)
                remaining_dec = max(_Decimal("0"), total_dec - used_dec)
                remaining = f"{remaining_dec:f}"
                account_balances.append(
                    AccountBalance(currency="USD", total_balance=remaining)
                )
                details.append(f"Remaining USD {remaining} ({used} used)")
                has_data = True
            else:
                details.append("Credits: parse-pending")

    # -- 2. Recent usage ------------------------------------------------------
    if not deadline.expired():
        usage_url = f"{_BASE_URL}{_USAGE_PATH}?limit={_USAGE_ROWS}"
        payload, error = _get_json(usage_url, secret, deadline.slice(_HTTP_TIMEOUT_S))
        if error:
            details.append(f"Usage: unavailable ({error})")
        elif not isinstance(payload, dict) or "data" not in payload:
            details.append("Usage: parse-pending")
        else:
            rows = payload["data"]
            if isinstance(rows, list) and rows:
                recent_total = _Decimal("0")
                detail_lines = []
                for row in rows[: _USAGE_ROWS]:
                    if not isinstance(row, dict):
                        continue
                    cost = _amount(row.get("real_cost_usd"))
                    model = str(row.get("model") or "?")
                    created = str(row.get("created_at", ""))[:10] if row.get("created_at") else ""
                    it = _safe_int(row.get("input_tokens"))
                    ot = _safe_int(row.get("output_tokens"))
                    ci = _safe_int(row.get("cached_input_tokens"))
                    rt = _safe_int(row.get("reasoning_tokens"))
                    # If any token field is non-coercible skip the token
                    # segment for this row rather than losing the balance.
                    token_str = ""
                    if it is not None and ot is not None and ci is not None and rt is not None:
                        token_sum = it + ot + ci + rt
                        token_str = f" · {token_sum}t"
                    # attribution_label deliberately omitted (may carry PII)
                    # id deliberately omitted (not user-facing)
                    if cost is not None:
                        recent_total += _Decimal(cost)
                    cost_str = cost if cost is not None else "?"
                    detail_lines.append(f"{model} · ${cost_str}{token_str} · {created}")
                if detail_lines:
                    details.append(f"Recent usage (last {len(detail_lines)}):")
                    details.extend(detail_lines)
                    details.append(f"Sum recent: ${_amount(str(recent_total)) or '?'}")
                    has_data = True

    if not has_data:
        return build_unavailable(PROVIDER_ID, "no-data")

    return QuotaResult(
        label=PROVIDER_ID,
        details=details,
        account_balances=account_balances,
        api_calls_available=None,  # credits endpoint does not report availability
    )