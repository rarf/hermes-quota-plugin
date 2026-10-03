"""Ollama Cloud usage — spend and per-model usage; no credit balance.

GET https://ollama.com/api/usage, Bearer API key. Live response shape, captured
against a real Free-tier account (2026-10-03):

    {
      "activity": {
        "cost": "0.00241",
        "period": {"type": "last_4_weeks",
                   "starting_at": "2026-09-07T00:00:00Z",
                   "ending_at":   "2026-10-03T11:05:03.33902593Z"},
        "models": [{"name": "deepseek-v4.1-flash", "request_count": 2,
                    "cost": "0.00241"}]
      },
      "limits": {
        "monthly": {"usage": 0.004,
                    "models": [{"name": "gpt-oss:120b", "request_count": 8}]}
      }
    }

This is the only usage endpoint: /api/usage. Everything else probed 404s, so the
balance and reset the settings page displays have no API surface.

**`limits.monthly.usage` is a server-reported fraction, not dollars.** The account
behind this capture shows "0.4% used" on ollama.com/settings for a value of
0.004, and nothing in this payload could produce that percentage any other way:
there is no allowance, limit or denominator field anywhere in the response. So
it is used as used/1 — the "server-reported percent" case add-provider.md allows.
A percentage is also what Ollama's own page shows, so any other reading would
show up as the card contradicting the page it came from.

**No reset date.** The settings page shows "Resets in 2 weeks", but no reset
timestamp is present in the response and none of the probed paths expose one,
so the window carries no reset rather than a guessed one.

**No credit balance.** The account behind the capture has a $5 balance that the
settings page displays, but there is no API for it: /api/credits/balance,
/api/credits, /api/balance, /api/billing/balance, /api/account,
/api/subscription, /api/settings, /api/user, /api/me/credits, /api/me/billing
and /api/account/usage all 404, and /api/usage ignores ?include=balance,
?include=credits and ?verbose alike. ollama/ollama#18653 requests the endpoint
and is open. Reporting a balance that cannot be read would be inventing one.

**No plan.** `POST /api/me` does return `"Plan": "free"`, but that is a
write-shaped request to repeat on every refresh for a label, so it is not used.
`plan` stays None.

Legacy pre-credits accounts return `limits.session` / `limits.weekly` instead of
`limits.monthly`, where the values are window counters rather than dollar
spend. Those are not rendered as currency; the card falls back to activity only.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Optional

from .api_keys import amount, get_json
from .base import QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect
from .registry import register

_PROVIDER_ID = "ollama"
_USAGE_URL = "https://ollama.com/api/usage"
_ME_URL = "https://ollama.com/api/me"
_TIMEOUT_S = 15.0


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
    """Read OLLAMA_API_KEY.

    Hermes core's resolve_api_key_provider_credentials() refuses this provider
    ("Provider 'ollama' is not an API-key provider") because Ollama is
    configured as a local/base-URL provider rather than a pooled API-key one, so
    the shared resolve_api_key() helper cannot be used. Read the same .env the
    core would have read, without ever logging the value.
    """
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


def _profile(secret: str):
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
        with urlopen_no_redirect(req, timeout=_TIMEOUT_S) as response:
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


def _next_monthly_reset(created_at) -> Optional[str]:
    """The next monthly reset, derived from the account creation date.

    ollama.com/blog/transparent-pricing: "On Pro, Max, and Team plans, usage
    resets monthly on the same day of the month your plan started ... On the free
    plan, usage resets monthly from the date you signed up." The API publishes
    no reset timestamp, but /api/me reports CreatedAt, so on Free — where signup
    and subscription start are the same day — the reset is the monthly
    anniversary of CreatedAt.

    Caveat, stated rather than hidden: on a paid plan it is the *plan* start
    that governs, and someone who subscribed later than they signed up would get
    the wrong day. There is no field that exposes the plan start, so this cannot
    be narrowed further from the API alone.

    The result is normalised to midnight UTC because Ollama documents the reset
    *day* only -- "resets monthly on the same day of the month your plan
    started" -- and publishes no time. Reusing CreatedAt's clock time would
    render a precise-looking hour that nothing supports, so the day is kept and
    the hour is dropped rather than invented.
    """
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    text = created_at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        started = datetime.datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    day = started.day

    def candidate(year: int, month: int) -> Optional[datetime.datetime]:
        # A 29th/30th/31st start has no counterpart in February; clamp to the
        # last day that exists rather than skipping the month entirely.
        last = [31, 29 if _is_leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        return datetime.datetime(year, month, min(day, last),
                                 started.hour, started.minute, started.second,
                                 tzinfo=datetime.timezone.utc)

    this_month = candidate(now.year, now.month)
    if this_month is not None and this_month > now:
        chosen = this_month
    else:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        chosen = candidate(year, month)
    if chosen is None:
        return None
    # Midnight UTC: the day is documented, the hour is not. See the docstring.
    return chosen.strftime("%Y-%m-%dT00:00:00Z")


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _used_percent(value) -> Optional[float]:
    """`limits.monthly.usage` as a percent, from the server-reported fraction.

    Accepts 0.0 and above: with pay-as-you-go an account can spend past its
    included pool, and >100% used is then the honest reading. `amount` rejects
    bools, non-finite numbers and absurd magnitudes.
    """
    number = amount(value)
    if number is None:
        return None
    # A negative fraction is a schema surprise, not "quota in credit": it would
    # render as a negative percentage and a >100% bar. Drop the window and keep
    # whatever spend detail the response still provides.
    if number < 0:
        return None
    return round(float(number) * 100.0, 2)


def _per_model(entry) -> Optional[str]:
    """One `name: N requests · $X` line, or None if the shape is unusable."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    count = entry.get("request_count")
    parts = []
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        parts.append(f"{count} request{'' if count == 1 else 's'}")
    cost = _money(entry.get("cost"))
    if cost is not None:
        parts.append(f"${cost}")
    elif not parts:
        return None
    return f"{name}: " + " · ".join(parts)


@register(_PROVIDER_ID)
def fetch_ollama_quota() -> QuotaResult:
    key = _resolve_key()
    if not key:
        return build_unavailable(_PROVIDER_ID, "no-credentials")
    payload, error = get_json(_USAGE_URL, key)
    if error:
        return build_unavailable(_PROVIDER_ID, error)
    if not isinstance(payload, dict):
        return build_unavailable(_PROVIDER_ID, "parse-pending")

    # Best effort: the plan label and the derived reset both come from /api/me.
    # A failure here costs the label, never the card.
    profile, profile_error = _profile(key)
    if profile is None and profile_error == "auth-failed":
        # The usage call already succeeded, so this is not a credential
        # problem; keep going rather than downgrading a working card.
        profile = {}

    activity = payload.get("activity")
    limits = payload.get("limits")
    activity = activity if isinstance(activity, dict) else {}
    limits = limits if isinstance(limits, dict) else {}

    details: list[str] = []
    windows: list[QuotaWindow] = []

    # Monthly included usage. `usage` is a server-reported fraction (0.004 ->
    # "0.4% used" on ollama.com/settings), used as used/1. See the docstring.
    monthly = limits.get("monthly")
    monthly = monthly if isinstance(monthly, dict) else {}
    used = _used_percent(monthly.get("usage"))
    reset_at = _next_monthly_reset(profile.get("CreatedAt")) if profile else None
    if used is not None:
        windows.append(QuotaWindow(
            label="Monthly", used_percent=used, reset_at=reset_at))

    # Spend over the rolling window. `cost` is explicitly a dollar string.
    period = activity.get("period")
    window = "last 4 weeks"
    if isinstance(period, dict):
        kind = period.get("type")
        if isinstance(kind, str) and kind.strip():
            window = kind.strip().replace("_", " ")
    cost = _money(activity.get("cost"))
    if cost is not None:
        details.append(f"Spend ({window}): ${cost}")

    # Per-model lines: activity first (it carries cost), then the monthly
    # counters (request counts only).
    seen: set[str] = set()
    for source in (activity.get("models"), monthly.get("models")):
        if not isinstance(source, list):
            continue
        for entry in source:
            line = _per_model(entry)
            if line and line not in seen:
                seen.add(line)
                details.append(line)

    if not windows and not details:
        return build_unavailable(_PROVIDER_ID, "no-data")

    # /api/me reports the plan verbatim ("free", "pro", ...). Only the name is
    # used -- never the ID, name or email that come back in the same payload.
    plan = None
    if profile:
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
    )
