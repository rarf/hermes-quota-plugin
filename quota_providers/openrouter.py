"""OpenRouter per-local-credential quota and ONE explicitly scoped wallet.

GET /api/v1/key: {"data": {"limit": number|null, "limit_remaining": number,
"usage": number, "usage_daily": number, "usage_weekly": number,
"usage_monthly": number, "limit_reset": string|null}}.
GET /api/v1/credits: {"data": {"total_credits": number, "total_usage": number}}.

/key cannot establish account identity; equal balances cannot establish it
either. Never sum wallets or query management /keys to inventory an account.
With exactly one distinct local credential the card drops the generated key
scope and shows the wallet as the large Account balance row; every key fact
(cap, remaining, reset interval, usage, failure) stays, minus that prefix. With
two or more credentials every key and the one scoped wallet stay explicit. The
result also carries structured ``credential_scope`` / ``key_limit_state``
evidence so the Desktop never has to infer a key's health from free-form
``details`` text. All credential/HTTP helpers are provider-local to avoid
coupling other fetchers.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import queue
import threading
import time
import urllib.error
import urllib.request

from .base import AccountBalance, QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect
from .registry import register

_BASE_URL = "https://openrouter.ai/api/v1"
_FETCH_BUDGET_S = 10.0
_HTTP_TIMEOUT_S = 7.0


def _env_value(name):
    # Core handles the active profile and deliberate .env edits over stale
    # inherited values. Do not scan unrelated environment variables ourselves.
    from hermes_cli.config import get_env_value_prefer_dotenv

    return get_env_value_prefer_dotenv(name)


def _resolve_credentials() -> list[tuple[str, str]]:
    """Read native credentials + saved pool without selecting/rotating it."""
    candidates = []
    resolution_failed = False
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        # Pin the quota endpoint, bypassing runtime pool load/select, which can
        # seed/persist credentials and alter rotation/cooldown state. Never use
        # the currently selected model's custom endpoint for billing requests.
        primary = resolve_runtime_provider(
            requested="openrouter", explicit_base_url=_BASE_URL,
            explicit_api_key=_env_value("OPENROUTER_API_KEY") or None,
        ).get("api_key")
        candidates.append(("native", primary))
    except Exception:
        resolution_failed = True
    try:
        from hermes_cli.auth import read_credential_pool

        for entry in read_credential_pool("openrouter"):
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            if isinstance(source, str) and source.startswith("env:"):
                # Env-backed rows can be secret-free on disk; do not resurrect
                # a stale stored token if that environment reference is absent.
                try:
                    secret = _env_value(source.split(":", 1)[1].strip())
                except Exception:
                    resolution_failed = True
                    continue
                candidates.append(("environment", secret))
            else:
                candidates.append(("saved", entry.get("access_token") or entry.get("runtime_api_key")))
    except Exception:
        resolution_failed = True
    result, seen = [], set()
    for source, secret in candidates:
        if not isinstance(secret, str) or not secret.strip():
            continue
        secret = secret.strip()
        if secret in seen:
            continue
        seen.add(secret)  # Equality only, private: no IDs, hashes or key suffixes.
        result.append((f"Key {len(result) + 1} ({source})", secret))
    if not result and resolution_failed:
        raise RuntimeError("credential-resolution-failed") from None
    return result


# Never forward a bearer credential to another endpoint (see base.NoRedirectHandler).
_urlopen = urlopen_no_redirect


def _get_json(url, secret):
    """Return (payload, safe reason), never response bodies or exception text."""
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {secret}", "Accept": "application/json",
        })
        with _urlopen(req, timeout=_HTTP_TIMEOUT_S) as response:
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
        return None, "auth-failed" if code == 401 else f"http-{code}"
    except TimeoutError:
        return None, "timeout"
    except Exception:
        return None, "fetch-error"


def _amount(value):
    """Finite bounded decimal; do not coerce booleans or extreme exponents."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value)
    if len(text) > 64:
        return None
    try:
        number = Decimal(text)
        if not number.is_finite() or number.copy_abs() > Decimal("1e15") or (number and number.adjusted() < -15):
            return None
        return number
    except (InvalidOperation, ValueError):
        return None


def _key_result(label, secret):
    """Return (windows, details, limit_state) for one credential.

    ``limit_state`` is the structured tone evidence: "uncapped" only when the
    API explicitly returned ``limit: null``. An absent, malformed, negative or
    boolean limit stays "unknown" -- showing no cap line is not proof of an
    uncapped key.
    """
    try:
        payload, error = _get_json(f"{_BASE_URL}/key", secret)
        if error:
            return [], [f"{label}: unavailable ({error})"], "unknown"
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return [], [f"{label}: unavailable (parse-pending)"], "unknown"
        details, windows = [], []
        limit, left = _amount(data.get("limit")), _amount(data.get("limit_remaining"))
        limit_state = "unknown"
        if limit is not None and limit >= 0:
            text = f"{label}: key cap USD {limit:.2f}"
            if left is not None:
                text += f"; remaining USD {left:.2f}"
                if left <= 0:
                    text += " (key cap exhausted; account wallet is separate)"
                if limit > 0 and 0 <= left <= limit:
                    windows.append(QuotaWindow(
                        label=f"{label} quota", used_percent=float((limit - left) / limit * 100),
                    ))
            reset = data.get("limit_reset")
            if reset in ("daily", "weekly", "monthly"):
                text += f"; resets {reset}"
            details.append(text)
            limit_state = "capped"
        elif "limit" in data and data["limit"] is None:
            details.append(f"{label}: no key-specific cap (account wallet still applies)")
            limit_state = "uncapped"
        for field, name in (("usage", "total"), ("usage_daily", "today UTC"),
                            ("usage_weekly", "this week UTC"), ("usage_monthly", "this month UTC")):
            value = _amount(data.get(field))
            if value is not None:
                details.append(f"{label}: usage {name} USD {value:.4f}")
        if not details:
            details.append(f"{label}: unavailable (parse-pending)")
        return windows, details, limit_state
    except Exception:
        return [], [f"{label}: unavailable (fetch-error)"], "unknown"


def _balance_text(value):
    """Cents for the headline, but never round a nonzero wallet to USD 0.00."""
    cents = f"{value:.2f}"
    if Decimal(cents) == 0 and value != 0:
        mills = f"{value:.4f}"
        return mills if Decimal(mills) != 0 else f"{value:f}"
    return cents


def _wallet_unavailable(label, reason, scoped=True):
    """A wallet failure that keeps its reason but never leaks the secret.

    ``scoped`` names the credential that failed. A single-key card has no
    other key to scope against, so it omits the generated label there too.
    """
    scope = f" via {label}" if scoped else ""
    return f"Account credits unavailable{scope} ({reason})"


def _wallet_result(label, secret, scoped=True):
    """Return (available, detail, AccountBalance|None) for the scoped wallet.

    The amount is carried separately so a single-key card can show it as the
    large generic Account balance row. With several keys only the detail line
    is used: the wallet belongs to one credential's account, and the other
    keys' membership is unverified.
    """
    try:
        payload, error = _get_json(f"{_BASE_URL}/credits", secret)
        data = payload.get("data") if isinstance(payload, dict) else None
        total = _amount(data.get("total_credits")) if isinstance(data, dict) else None
        used = _amount(data.get("total_usage")) if isinstance(data, dict) else None
        if error or total is None or used is None:
            return False, _wallet_unavailable(label, error or "parse-pending", scoped), None
        remaining = total - used
        scope = f" via {label}" if scoped else ""
        return True, (
            f"Account wallet{scope}: USD {remaining:.2f} remaining "
            "(shared account credits, shown once; not summed across keys)"
        ), AccountBalance(currency="USD", total_balance=_balance_text(remaining))
    except Exception:
        return False, _wallet_unavailable(label, "fetch-error", scoped), None


def _single_key_windows(windows, label):
    """Drop the generated per-key prefix when there is only one key."""
    generated = f"{label} quota"
    return [
        QuotaWindow(
            label="Key quota" if window.label == generated else window.label,
            used_percent=window.used_percent,
            reset_at=window.reset_at,
        )
        for window in windows
    ]


def _single_key_details(details, label):
    """Drop the generated credential prefix, never the facts worth showing.

    The label exists to tell several locally configured keys apart. With one
    key it is noise, but a cap, remaining allowance, reset interval, usage
    total or failure reason must survive verbatim — dropping the line would
    make a capped or failing key look like it reported nothing. The "no cap"
    placeholder is the one exception: with no cap returned there is no cap
    fact, so it is omitted rather than displayed as an empty key cap.
    """
    prefix = f"{label}: "
    uncapped = f"{label}: no key-specific cap (account wallet still applies)"
    lines = []
    for line in details:
        if line == uncapped:
            continue
        if line.startswith(prefix):
            line = line[len(prefix):]
            if line[:1].islower():
                line = line[:1].upper() + line[1:]
        lines.append(line)
    return lines


@register("openrouter")
def fetch_openrouter_quota() -> QuotaResult:
    try:
        credentials = _resolve_credentials()
        if not credentials:
            return build_unavailable("openrouter", "no-credentials")
        single = len(credentials) == 1
        details = [] if single else [
            f"Locally configured credentials: {len(credentials)} (deduplicated); not an inventory of all account keys",
            "Management API key listing not queried; credentials from Hermes native auth and saved pool only",
        ]
        completed = queue.Queue()
        deadline = time.monotonic() + _FETCH_BUDGET_S

        def worker(index, fetcher, *args):
            completed.put((index, fetcher(*args)))

        # Key failures and wallet failures are independent, including a hung
        # wallet. Daemon workers never mutate returned results or the cache.
        for index, (label, secret) in enumerate(credentials):
            threading.Thread(target=worker, args=(index, _key_result, label, secret), daemon=True).start()
        primary_label, primary_secret = credentials[0]
        threading.Thread(
            target=worker,
            args=(-1, _wallet_result, primary_label, primary_secret, not single),
            daemon=True,
        ).start()
        results = {}
        while len(results) < len(credentials) + 1:
            try:
                index, result = completed.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            results[index] = result
        has_data, wallet, balance = results.get(
            -1,
            (False, _wallet_unavailable(primary_label, "timeout", not single), None),
        )
        account_balances = []
        if single and balance is not None:
            # One credential means no second key to scope the wallet against:
            # show the account money as the card's headline instead of a
            # technical "via Key N" detail. With several keys the scoped detail
            # remains the only honest representation.
            account_balances.append(balance)
        else:
            details.append(wallet)
        if not single:
            details.append("Account membership of other keys is unverified; wallet is not a total across accounts")
        windows = []
        limit_states = []
        for index, (label, _) in enumerate(credentials):
            ws, ds, limit_state = results.get(
                index, ([], [f"{label}: unavailable (timeout)"], "unknown"))
            limit_states.append(limit_state)
            if single:
                # A single-key line that is not its own failure marker nor the
                # omitted "no cap" placeholder is real data.
                uncapped = f"{label}: no key-specific cap (account wallet still applies)"
                key_has_data = bool(ws) or any(
                    not d.startswith(f"{label}: unavailable (") and d != uncapped
                    for d in ds
                )
                ws = _single_key_windows(ws, label)
                ds = _single_key_details(ds, label)
            else:
                # Scoped cards keep every key line, so their data test is
                # unchanged: any line but that key's own failure marker.
                key_has_data = bool(ws) or any(
                    not d.startswith(f"{label}: unavailable (") for d in ds
                )
            has_data = has_data or key_has_data
            windows.extend(ws)
            details.extend(ds)
        return QuotaResult(
            label="openrouter", windows=windows, details=details,
            account_balances=account_balances,
            credential_scope="single" if single else "scoped",
            # Only a single-key card can use this: with several keys the wallet
            # is scoped and the key states are not one aggregate health claim.
            key_limit_state=limit_states[0] if single else None,
            unavailable_reason=None if has_data else "no-data",
        )
    except Exception:
        return build_unavailable("openrouter", "fetch-error")
