"""NanoGPT subscription usage (GET) and prepaid USD balance (POST).

GET /api/v1/subscription/usage: active, limits, quota counters and resetAt (ms).
POST /api/check-balance: usd_balance (decimal string); cash is a separate pool.
"""
from datetime import datetime, timezone
import json
import urllib.error
import urllib.request

from .api_keys import amount
from .base import AccountBalance, Deadline, QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect
from .registry import register

_BASE = "https://api.nano-gpt.com/api"


def _api_key():
    """Read the profile-aware key without requiring a NanoGPT provider plugin."""
    from hermes_cli.config import get_env_value_prefer_dotenv

    key = get_env_value_prefer_dotenv("NANOGPT_API_KEY")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _request_json(path, secret, deadline, *, balance=False):
    if deadline.expired():
        return None, "timeout"
    headers = {"Accept": "application/json"}
    headers.update({"x-api-key": secret} if balance else {"Authorization": f"Bearer {secret}"})
    try:
        request = urllib.request.Request(_BASE + path, headers=headers,
                                         method="POST" if balance else "GET")
        with urlopen_no_redirect(request, timeout=deadline.slice(7)) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None, "response-too-large"
        return json.loads(body), None
    except urllib.error.HTTPError as exc:
        code = exc.code
        if exc.fp is not None:
            exc.close()
        return None, "auth-failed" if code in (401, 403) else f"http-{code}"
    except (ValueError, UnicodeError):
        return None, "bad-json"
    except TimeoutError:
        return None, "timeout"
    except Exception:
        return None, "fetch-error"


def _reset_at(value):
    stamp = amount(value)
    if stamp is None or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(stamp) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _windows(payload):
    limits = payload.get("limits")
    limits = dict(limits) if isinstance(limits, dict) else {}
    token_limits = payload.get("tokenLimits")
    if isinstance(token_limits, dict) and "total" in token_limits:
        limits["tokens"] = token_limits["total"]
    windows = []
    for key, label in (("dailyInputTokens", "Daily tokens"),
                       ("weeklyInputTokens", "Weekly tokens"), ("dailyImages", "Daily images"),
                       ("tokens", "Trial tokens")):
        row = payload.get(key)
        if not isinstance(row, dict) or (key in limits and limits[key] is None):
            continue
        if row.get("degraded") is True:
            windows.append(QuotaWindow(label, reset_at=_reset_at(row.get("resetAt"))))
            continue
        used, limit = amount(row.get("used")), amount(limits.get(key))
        fraction = amount(row.get("percentUsed"))
        remaining = amount(row.get("remaining"))
        # percentUsed is always a fraction, including values above one.
        if fraction is not None and fraction >= 0:
            percent = fraction * 100
        elif used is not None and used >= 0 and limit is not None and limit > 0:
            percent = used / limit * 100
        elif used is not None and used >= 0 and remaining is not None and used + remaining > 0:
            percent = used / (used + remaining) * 100
        else:
            continue
        windows.append(QuotaWindow(label, float(percent), _reset_at(row.get("resetAt"))))
    return windows


@register("nano-gpt")
def fetch_nanogpt_quota() -> QuotaResult:
    try:
        secret = _api_key()
        if not secret:
            return build_unavailable("nano-gpt", "no-credentials")
        deadline = Deadline(10)
        payload, error = _request_json("/v1/subscription/usage", secret, deadline)
        if error:
            return build_unavailable("nano-gpt", error)
        if not isinstance(payload, dict) or not isinstance(payload.get("active"), bool):
            return build_unavailable("nano-gpt", "parse-pending")
        windows = _windows(payload) if payload["active"] else []
        details = []
        if payload["active"] is False:
            details.append("No active subscription; prepaid balance is separate")
        elif not windows:
            details.append("Subscription quota unavailable (unknown)")
        balance, error = _request_json("/check-balance", secret, deadline, balance=True)
        usd = amount(balance.get("usd_balance")) if isinstance(balance, dict) and error is None else None
        balances = []
        if usd is not None:
            balances.append(AccountBalance("USD", f"{usd:f}"))
            details.append(f"Prepaid balance: USD {usd:f} (separate from included quota)")
        else:
            details.append(f"Prepaid balance unavailable ({error or 'parse-pending'})")
        return QuotaResult(label="nano-gpt", windows=windows, details=details,
                           account_balances=balances,
                           unavailable_reason=None if windows or balances else "no-data")
    except (ImportError, RuntimeError):
        return build_unavailable("nano-gpt", "no-credentials")
    except Exception:
        return build_unavailable("nano-gpt", "fetch-error")
