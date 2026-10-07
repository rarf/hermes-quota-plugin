"""Ollama Cloud usage — remaining included credits, reset date, and spend.

Two endpoints, both Bearer-key authenticated, both documented in
docs/api/cloud-usage.mdx and docs/api/balance.mdx since ollama/ollama#18829
(server: proxy cloud usage and balance APIs, merged 2026-10-07).

**`GET /api/balance` — the window, the percent and the reset.** Live response
against a real Free-tier account (2026-10-07):

    {
      "included": {
        "balance_usd":  2.43893,
        "allowance_usd": 2.5,
        "period": {"from": "2026-09-22T09:45:23.470675Z",
                   "until": "2026-10-22T09:45:23.470675Z"}
      },
      "purchased": {"balance_usd": 4.99759}
    }

`allowance_usd` is the real denominator the old response never carried, and
`period.until` is the actual subscription reset — Ollama's own docs: "The
included period follows your plan's monthly reset schedule, including for annual
subscriptions." That replaces the previous derivation from /api/me `CreatedAt`,
which was wrong by up to a month for anyone who subscribed after signing up.

The percent is derived, not reported: `(allowance_usd - balance_usd) /
allowance_usd`. Both sides are real dollars from the same response, so the
division is arithmetic, not inference. `balance_usd` above the allowance
(pay-as-you-go overspend) yields >100% used rather than being clamped, matching
the old behaviour for the same situation.

**The two pools are separate money.** ollama.com/settings shows "Free usage
credits" with a percentage against a small monthly allowance, and "Usage
credits / Current balance" for purchased credit below it — on the account this
was captured from, $2.44 of a $2.50 allowance (2.44% used, matching the
dashboard's "2.4% used") alongside a separate $4.99759 of spendable purchased
credit. `included` is the pool that refills at the reset and that the
percentage measures; `purchased` is the wallet the card shows as "Account
balance", because that is the figure the dashboard labels "Current balance".

`/api/usage`'s `totals.usage_usd` spans **both** pools — it is total spend, not
"drawn from the allowance". On the same account it reads $0.06348 against
$0.06107 drawn from the included pool: the $0.00241 difference is the two
deepseek-v4.1-flash requests, which are not free-model requests and so were
paid from purchased credit. The card therefore labels it "Total spend" and says
so, instead of implying it is the same number as the percentage's base.

Legacy pre-credits plans return `included.session` / `included.weekly` with
`remaining_percent` and `resets_at` instead of `balance_usd`/`allowance_usd`.
Those are reported verbatim — the percent is Ollama's own, not derived.

**`GET /api/usage` — spend and request counts over a time range.** The endpoint
was rewritten as a timeseries and no longer returns `activity` / `limits`:

    {
      "range": "7d", "scope": "self", "granularity": "day",
      "from": "2026-09-30T00:00:00Z", "until": "2026-10-07T05:52:38Z",
      "totals": {"request_count": 18, "usage_usd": 0.06348,
                 "input_tokens": 673390, "cached_input_tokens": 392224,
                 "output_tokens": 8111},
      "buckets": [{"from": "...", "until": "...", "partial": true,
                   "request_count": 0, "usage_usd": 0, ...}]
    }

`range` is one of `24h` (hourly buckets), `7d` or `30d` (daily). Anything else
returns 400, as does any unrecognised parameter.

**No plan.** `POST /api/me` returns `"Plan": "free"`, but that is a write-shaped
request to repeat on every refresh for a label, so it is not used. `plan` stays
None.

Both endpoints allow 10 requests per minute per user, shared across API keys and
devices, and answer a breach with 429 + `Retry-After`. Three calls per refresh
(usage, balance, profile) sit well inside that, but the calls are spaced rather
than fired at once.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Optional

from .api_keys import amount
from .base import (
    AccountBalance,
    Deadline,
    QuotaResult,
    QuotaWindow,
    build_unavailable,
    urlopen_no_redirect,
)
from .registry import register

_PROVIDER_ID = "ollama"
_USAGE_URL = "https://ollama.com/api/usage"
_BALANCE_URL = "https://ollama.com/api/balance"
_ME_URL = "https://ollama.com/api/me"
# The documented ranges are 24h / 7d / 30d. 30d is asked for because the
# included-credit window resets monthly, so the read has to cover the period the
# card's percentage describes.
_USAGE_RANGE = "30d"
_TIMEOUT_S = 15.0

# Three calls run in series, so they share one deadline. quota_cache runs every
# provider under a single REFRESH_BUDGET_S (20 s by default) and records an
# overrun as `timeout`, dropping the provider's previous value. Two 7 s reads
# plus an unbounded profile call could reach ~29 s on its own, which would cost
# the whole sweep. Mirrors cursor.py, which bounds its keychain read and two
# RPCs the same way.
_FETCH_BUDGET_S = 18.0
_HTTP_TIMEOUT_S = 7  # matches api_keys.get_json's own default


def _dotenv_path() -> str:
    """Locate Hermes' own .env without importing core (standalone installs)."""
    home = None
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
    except Exception:  # noqa: BLE001 - core absent; fall back to the default path
        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.path.expanduser("~"), ".hermes")
    if not home:  # get_hermes_home() could hand back an empty string
        home = os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, ".env")


def _resolve_key() -> Optional[str]:
    """Resolve the key via Hermes core, then fall back to OLLAMA_API_KEY.

    Core registers the provider as ``ollama-cloud`` with ``auth_type='api_key'``
    and ``api_key_env_vars=('OLLAMA_API_KEY',)``, so its resolver covers both
    the environment and the credential pool -- the path a pooled install uses,
    where the key is not in ``.env`` at all. Mirrors kimi.py and openrouter.py.

    The direct OLLAMA_API_KEY / .env read stays as the standalone fallback for
    installs with no importable core, and never logs the value.
    """
    try:
        from hermes_cli.auth import (
            PROVIDER_REGISTRY,
            _resolve_api_key_provider_secret,
        )

        pconfig = PROVIDER_REGISTRY.get("ollama-cloud")
        if pconfig is not None and getattr(pconfig, "auth_type", "") == "api_key":
            key, _source = _resolve_api_key_provider_secret("ollama-cloud", pconfig)
            if key and str(key).strip():
                return str(key).strip()
    except Exception:  # noqa: BLE001 - standalone install / locked store
        pass

    env_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        with open(_dotenv_path(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() != "OLLAMA_API_KEY":
                    continue
                value = value.strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def _money(value) -> Optional[str]:
    """A dollar figure, normalised to a plain decimal string.

    `amount` rejects bools, non-finite numbers and absurd magnitudes; a bad
    figure must not become "$0.00", which would read as a real balance.
    """
    number = amount(value)
    if number is None:
        return None
    return f"{number:f}"


def _dollars(value) -> Optional[str]:
    """A dollar figure rounded for display — cents, because nobody reads mills.

    The endpoint's own precision is a byproduct of per-token rounding, not
    meaning: a card reading "$2.43893 of $2.5" implies a resolution the number
    does not carry. One exception keeps the widget's existing guard intact: a
    balance that is nonzero but rounds to $0.00 would read as "no money", so
    such a figure keeps enough digits to stay visibly nonzero.
    """
    number = amount(value)
    if number is None:
        return None
    cents = round(float(number), 2)
    if cents == 0.0 and float(number) != 0.0:
        return f"{number:.4f}"
    return f"{cents:.2f}"


def _profile(secret: str, timeout: float = _TIMEOUT_S):
    """(dict, None) from POST /api/me, or (None, reason).

    Ollama exposes the current user only over POST — GET, HEAD, OPTIONS, PUT,
    PATCH and DELETE all return 405, so this is the sole way to learn the plan.
    It is sent with an empty body and returns the stored profile; a failure here
    is never fatal, the card just goes without a label.
    """
    req = urllib.request.Request(
        _ME_URL, method="POST", data=b"",
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"})
    try:
        with urlopen_no_redirect(req, timeout=timeout) as response:
            body = response.read(256 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        if exc.fp is not None:
            exc.close()
        return None, "auth-failed" if code in (401, 403) else f"http-{code}"
    except (TimeoutError,):
        return None, "timeout"
    except Exception:  # noqa: BLE001 - best effort, never fatal
        return None, "fetch-error"
    if len(body) > 256 * 1024:
        return None, "response-too-large"
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return None, "bad-json"
    return (data, None) if isinstance(data, dict) else (None, "parse-pending")


def _get_json(url: str, key: str, timeout: float, max_bytes: int = 1024 * 1024):
    """(dict, None) from a Bearer GET, or (None, reason).

    Shared by /api/usage and /api/balance. `urlopen_no_redirect` keeps the
    credential from being replayed to a redirect target.
    """
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urlopen_no_redirect(req, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        if exc.fp is not None:
            exc.close()
        if code in (401, 403):
            return None, "auth-failed"
        if code == 429:
            return None, "http-429"
        return None, f"http-{code}"
    except (TimeoutError,):
        return None, "timeout"
    except Exception:  # noqa: BLE001 - URLError, timeout, malformed body
        return None, "fetch-error"
    if len(body) > max_bytes:
        return None, "oversized-response"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None, "parse-pending"
    return (data, None) if isinstance(data, dict) else (None, "parse-pending")


def _usage(key: str, timeout: float = _HTTP_TIMEOUT_S):
    """GET /api/usage?range=30d, with the caller's deadline as socket timeout.

    ``range`` is one of 24h / 7d / 30d; anything else is a 400. 30d is asked for
    because the included-credit window is monthly.
    """
    return _get_json(f"{_USAGE_URL}?range={_USAGE_RANGE}", key, timeout)


def _balance(key: str, timeout: float = _HTTP_TIMEOUT_S):
    """GET /api/balance — included allowance, balance, and the reset period."""
    return _get_json(_BALANCE_URL, key, timeout)


def _included_spend_percent(included) -> Optional[float]:
    """(allowance - balance) / allowance as a used percent.

    Both figures are dollar amounts from the same response, so this is
    arithmetic rather than inference. A balance above the allowance (pay-as-you-
    go overspend) yields >100% instead of being clamped; a zero allowance has no
    denominator and yields None rather than a division error.
    """
    allowance = amount(included.get("allowance_usd"))
    balance = amount(included.get("balance_usd"))
    if allowance is None or balance is None or float(allowance) <= 0:
        return None
    used = (float(allowance) - float(balance)) / float(allowance)
    return round(max(0.0, used) * 100.0, 2)


def _windows(included) -> list[QuotaWindow]:
    """Billing windows from /api/balance's `included` block.

    Current plans carry `balance_usd` + `allowance_usd` + `period.until`; legacy
    plans carry `session` / `weekly` with Ollama's own `remaining_percent` and
    `resets_at`. Either way the reset is the timestamp the provider published —
    no derivation, and so nothing to be wrong by a month.
    """
    if not isinstance(included, dict):
        return []
    windows: list[QuotaWindow] = []

    percent = _included_spend_percent(included)
    reset_at = _iso_utc((included.get("period") or {}).get("until")
                        if isinstance(included.get("period"), dict) else None)
    if percent is not None:
        windows.append(QuotaWindow(label="Included credits", used_percent=percent,
                                   reset_at=reset_at))

    for name, label in (("session", "Session"), ("weekly", "Weekly")):
        block = included.get(name)
        if not isinstance(block, dict):
            continue
        remaining = amount(block.get("remaining_percent"))
        used = round(100.0 - float(remaining), 2) if remaining is not None else None
        windows.append(QuotaWindow(
            label=label, used_percent=used,
            reset_at=_iso_utc(block.get("resets_at"))))
    return windows


def _usage_details(usage) -> list[str]:
    """Spend, request and token lines from /api/usage's `totals`."""
    if not isinstance(usage, dict):
        return []
    totals = usage.get("totals")
    if not isinstance(totals, dict):
        return []
    span = usage.get("range")
    span = span if isinstance(span, str) and span.strip() else "period"
    details: list[str] = []
    cost = _dollars(totals.get("usage_usd"))
    if cost is not None:
        # "total", not just "spend": this figure spans every request, including
        # the ones paid from the included allowance, so it is not "drawn from
        # the pool" and calling it plain spend would imply it is. The README
        # spells out how the two differ.
        details.append(f"Total spend ({span}): ${cost}")
    count = totals.get("request_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        details.append(f"Requests ({span}): {count}")
    tokens = _compact_tokens(totals)
    if tokens:
        details.append(f"Tokens ({span}): {tokens}")
    if not details:
        # An empty `totals` block carries nothing; saying so in prose would make
        # a data-less card look populated and defeat has_data().
        return []
    return details


def _compact_tokens(totals: dict) -> Optional[str]:
    """Input/output token counts, input split into cached and uncached."""
    parts: list[str] = []
    for key, label in (("input_tokens", "in"), ("output_tokens", "out")):
        value = totals.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            parts.append(f"{value:,} {label}")
    return " · ".join(parts) if parts else None


def _balance_rows(balance) -> list[AccountBalance]:
    """Purchased credit, as the one account-money row.

    Purchased credit is the money actually in hand — ollama.com/settings labels
    it "Usage credits / Current balance" — so it is the figure that belongs under
    "Account balance". The included allowance is deliberately *not* a second row:
    the widget labels every row "Account balance", so a second one would read as
    another wallet rather than as the plan's monthly pool. It rides in the detail
    lines, next to the percentage it is the denominator of.
    """
    if not isinstance(balance, dict):
        return []
    purchased = balance.get("purchased")
    if not isinstance(purchased, dict):
        return []
    # Display-rounded, not the endpoint's raw digits: this row is the card's
    # headline money figure and "Account balance $4.99759" reads as noise. The
    # widget renders `total_balance` verbatim (balanceFractionDigits honours
    # whatever precision it is given), so the rounding has to happen here.
    value = _dollars(purchased.get("balance_usd"))
    if value is None:
        return []
    return [AccountBalance(currency="USD", total_balance=value)]


def _balance_details(balance) -> list[str]:
    """Name the included allowance, and purchased credit when there is any.

    The included line states the denominator the percentage came from, so the
    percent is auditable against the raw dollars rather than taken on trust.
    Purchased credit is a separate pot that does not refill at the reset, so it
    is labelled as such instead of being added to the included figure.
    """
    if not isinstance(balance, dict):
        return []
    included = balance.get("included")
    lines: list[str] = []
    if isinstance(included, dict):
        remaining = _dollars(included.get("balance_usd"))
        allowance = _dollars(included.get("allowance_usd"))
        if remaining is not None and allowance is not None:
            lines.append(f"Included credits: ${remaining} of ${allowance} remaining")
        elif remaining is not None:
            lines.append(f"Included credits: ${remaining} remaining")
    purchased = balance.get("purchased")
    if isinstance(purchased, dict):
        value = _dollars(purchased.get("balance_usd"))
        if value is not None:
            lines.append(f"Purchased credits: ${value} (does not refill at reset)")
    return lines


def _iso_utc(value) -> Optional[str]:
    """A provider timestamp, normalised to `Z` and rejected if it is not a time.

    `/api/balance` publishes UTC with a trailing Z, but the field is echoed into
    the cache and rendered as a countdown, so anything unparseable is dropped
    rather than shown as a reset that never arrives.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@register(_PROVIDER_ID)
def fetch_ollama_quota() -> QuotaResult:
    # usage, balance and profile run in series under one deadline, sized under
    # the cache's own refresh budget so a slow Ollama cannot eat the sweep.
    deadline = Deadline(_FETCH_BUDGET_S)
    key = _resolve_key()
    if not key:
        return build_unavailable(_PROVIDER_ID, "no-credentials")

    # 30d rather than the 7d default: the included-credit window is monthly, so
    # a 30d read covers the whole billing period the card's percent describes.
    usage, usage_error = _usage(key, deadline.slice(_HTTP_TIMEOUT_S))
    balance, balance_error = None, "timeout"
    if not deadline.expired():
        balance, balance_error = _balance(key, deadline.slice(_HTTP_TIMEOUT_S))
    if deadline.expired():
        profile, profile_error = None, "timeout"
    else:
        profile, profile_error = _profile(key, deadline.slice(_TIMEOUT_S))

    # The balance endpoint is what carries the window; usage carries the spend
    # lines. Either one alone is a card, so a failure on one side must not
    # discard the other's data — only losing both makes the provider dead.
    included = balance.get("included") if isinstance(balance, dict) else None
    windows = _windows(included)
    details = _balance_details(balance) + _usage_details(usage)
    balances = _balance_rows(balance)

    if not windows and not details and not balances:
        # Nothing survived: report the first failure so the reason names the
        # call that actually broke, and fall back to no-data when neither
        # endpoint reported an error (both answered, neither carried a figure).
        return build_unavailable(
            _PROVIDER_ID, usage_error or balance_error or "no-data")

    # /api/me reports the plan verbatim ("free", "pro", ...). Only the name is
    # used -- never the ID, name or email that come back in the same payload.
    plan = None
    if profile and profile_error != "auth-failed":
        raw_plan = profile.get("Plan")
        if isinstance(raw_plan, str) and raw_plan.strip():
            # /api/me reports it lower-case ("free"); the widget shows the plan
            # as a tier name, so capitalise the first letter and leave the rest
            # as sent rather than title-casing an acronym like "max".
            label = raw_plan.strip()[:32]
            plan = label[:1].upper() + label[1:]

    return QuotaResult(
        label=_PROVIDER_ID,
        windows=windows,
        plan=plan,
        unavailable_reason=None,
        details=details,
        account_balances=balances,
    )
