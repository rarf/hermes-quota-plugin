"""Direct DeepSeek API balance (not DeepSeek models routed via OpenRouter).

GET https://api.deepseek.com/user/balance, Bearer API key. Documented shape:
{"is_available": true, "balance_infos": [{"currency": "USD",
"total_balance": "12.50", "granted_balance": "0", "topped_up_balance": "12.50"}]}.
Balances have no quota denominator; never invent a percentage or plan.
"""
from .api_keys import amount, get_json, resolve_api_key
from .base import AccountBalance, QuotaResult, build_unavailable
from .registry import register


@register("deepseek")
def fetch_deepseek_quota() -> QuotaResult:
    try:
        secret = resolve_api_key("deepseek")
        if not secret:
            return build_unavailable("deepseek", "no-credentials")
        # The endpoint is account-level, not a per-key budget. Native primary
        # only: never add balances together across credentials or currencies.
        payload, error = get_json("https://api.deepseek.com/user/balance", secret)
        if error:
            return build_unavailable("deepseek", error)
        if not isinstance(payload, dict) or not isinstance(payload.get("is_available"), bool):
            return build_unavailable("deepseek", "parse-pending")
        balances = payload.get("balance_infos")
        if not isinstance(balances, list) or not balances:
            return build_unavailable("deepseek", "parse-pending")
        details = ["API calls available: " + ("yes" if payload["is_available"] else "no")]
        currencies = set()
        account_balances = []
        for row in balances:
            if not isinstance(row, dict) or row.get("currency") not in ("USD", "CNY"):
                return build_unavailable("deepseek", "parse-pending")
            currency = row["currency"]
            values = [amount(row.get(k)) for k in ("total_balance", "granted_balance", "topped_up_balance")]
            if any(v is None for v in values) or currency in currencies:
                return build_unavailable("deepseek", "parse-pending")
            currencies.add(currency)
            total, granted, topped = values
            account_balances.append(AccountBalance(currency, f"{total:f}", f"{granted:f}", f"{topped:f}"))
            details.append(f"{currency} total: {total:f}; granted: {granted:f}; topped-up: {topped:f}")
        details.append("Direct API account balance (native credential); currencies are not combined")
        details.append("The balance endpoint does not report per-key spending limits")
        return QuotaResult(label="deepseek", details=details,
                           account_balances=account_balances, api_calls_available=payload["is_available"])
    except Exception:
        return build_unavailable("deepseek", "fetch-error")
