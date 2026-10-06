"""OpenCode Go quota fetcher — plugin standalone copy.

OpenCode Go (https://opencode.ai/docs/go) is a low-cost subscription that
exposes usage limits as three rolling dollar-denominated windows:

  * 5 hour limit  — $12 of usage
  * Weekly limit  — $30 of usage
  * Monthly limit — $60 of usage

The authoritative numbers come from the same endpoint CodexBar uses:

    GET https://opencode.ai/zen/go/v1/usage
    Authorization: Bearer <API key>

The API key is the OpenCode Zen key copied from the console
(https://opencode.ai/auth).  Resolution order:

1. ``OPENCODE_API_KEY`` environment variable (the same var OpenCode itself
   and CodexBar read);
2. ``OPENCODE_GO_API_KEY`` environment variable — Hermes' own ``opencode-go``
   *chat* provider keeps the same Zen key under this name in ``~/.hermes/.env``,
   so a working Go key is honoured instead of being reported as missing;
3. ``opencode`` entry in OpenCode's own auth file,
   ``~/.local/share/opencode/auth.json`` (written by ``opencode auth login``
   / the ``/connect`` TUI command).  The file stores one record per provider;
   we accept either a plain API-key object or an OAuth record whose nested
   payload carries the key.

The usage endpoint is flaky on the vendor's side: it answers
``503 {"message":"Go usage is unavailable"}`` for a large share of calls no
matter which User-Agent or credential is used, so every transient failure is
retried (``_RETRY_ATTEMPTS``) before the provider is reported unavailable.

Note on OAuth: OpenCode's CLI supports an OAuth *device flow* against the
console (``POST {console}/auth/device/code`` → ``/auth/device/token`` with
``client_id: opencode-cli``), but that flow authenticates the console account
and is not required to read usage — the Zen usage endpoint accepts the static
API key directly, so this fetcher never performs network auth.

Parsing mirrors CodexBar's tolerant approach: window dicts are located under
``rollingUsage`` / ``weeklyUsage`` / ``monthlyUsage`` (plus common snake_case
aliases), percentages accept both 0–100 and 0–1 fractions, resets arrive as
either ``resetInSec`` seconds or absolute timestamps, and percent can be
computed from ``used/limit`` pairs when no direct field exists.  Anything the
parser cannot find becomes an honest ``unavailable_reason``, never fake zeros.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Optional

from .base import Deadline, QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect

_PROVIDER_ID = "opencode-go"
_API_URL = "https://opencode.ai/zen/go/v1/usage"

# The usage endpoint flaps: it answers 503 {"message":"Go usage is unavailable"}
# for roughly a fifth to a third of calls, independently of the User-Agent or
# the credential (verified 2026-09-18: same key, same second, mixed 200/503
# across three UAs).  A single attempt therefore loses the provider at random,
# so every transient failure is retried with a short backoff.
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF_SECONDS = (0.25, 0.5, 1.0)
_TRANSIENT_HTTP_STATUSES = (429, 500, 502, 504)
# The retry-loop budget. It caps request socket timeouts and the total
# request/backoff time the fetcher schedules, but urllib's timeout is not a
# hard wall-clock limit: a response that keeps trickling bytes can stay active
# past this budget. The cache sweep records a still-running provider as
# `timeout` after its wait budget, but does not cancel its daemon worker.
_REQUEST_TIMEOUT_S = 15.0
_FETCH_BUDGET_S = 15.0

def _auth_file_candidates() -> tuple[str, ...]:
    """Known locations of OpenCode's local auth file across platforms.

    Linux/macOS use ``~/.local/share/opencode/auth.json``; on Windows the CLI
    keeps state under ``%LOCALAPPDATA%\\opencode\\auth.json`` (with an
    XDG-style override via ``XDG_DATA_HOME``).
    """
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".local", "share", "opencode", "auth.json")]
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        candidates.append(os.path.join(xdg_data, "opencode", "auth.json"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "opencode", "auth.json"))
    return tuple(candidates)


_AUTH_PATHS = _auth_file_candidates()
# ``OPENCODE_API_KEY`` is the name OpenCode's own CLI and CodexBar use for the
# Zen key.  Hermes' *chat* provider ``opencode-go`` stores the same Zen key
# under ``OPENCODE_GO_API_KEY`` in ``~/.hermes/.env``, so a machine can hold a
# perfectly good Go key that this fetcher never looked at.  Accept both, the
# canonical name first.
_ENV_KEYS = ("OPENCODE_API_KEY", "OPENCODE_GO_API_KEY")

_PERCENT_KEYS = (
    "usagePercent",
    "usedPercent",
    "percentUsed",
    "percent",
    "usage_percent",
    "used_percent",
    "utilization",
    "utilizationPercent",
    "utilization_percent",
)
# NOTE: "usage" is deliberately NOT a _PERCENT_KEYS entry. It is also in
# _USED_KEYS, and the percent branch is tried first, so listing it here would
# shadow the used/limit fallback this module documents ("percent can be
# computed from used/limit pairs when no direct field exists") and report a
# dollar amount as a percentage. The live shape nests window dicts under a
# top-level "usage" wrapper, which is handled by the wrapper walk, not here.
_RESET_IN_SEC_KEYS = (
    "resetInSec",
    "resetInSeconds",
    "resetSeconds",
    "reset_sec",
    "reset_in_sec",
    "resetsInSec",
    "resetsInSeconds",
    "resetIn",
    "resetSec",
)
_RESET_AT_KEYS = (
    "resetAt",
    "resetsAt",
    "reset_at",
    "resets_at",
    "nextReset",
    "next_reset",
)
_USED_KEYS = ("used", "usage", "consumed", "count", "usedTokens")
_LIMIT_KEYS = ("limit", "total", "quota", "max", "cap", "tokenLimit")

_ROLLING_KEYS = ("rollingUsage", "rolling", "rolling_usage", "rollingWindow", "rolling_window")
_WEEKLY_KEYS = ("weeklyUsage", "weekly", "weekly_usage", "weeklyWindow", "weekly_window")
_MONTHLY_KEYS = ("monthlyUsage", "monthly", "monthly_usage", "monthlyWindow", "monthly_window")


# -- credential resolution ----------------------------------------------------


def _read_env_api_key() -> Optional[str]:
    for key in _ENV_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        trimmed = value.strip().strip("\"'")
        if trimmed:
            return trimmed
    return None


def _extract_api_key(value: Any) -> Optional[str]:
    """Pull an API key out of one auth.json provider record."""
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    if not isinstance(value, dict):
        return None
    # Plain API-key record: {"type": "api", "key": "..."}
    for field in ("key", "apiKey", "api_key"):
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    # OAuth-style record: {"type": "oauth", "refresh": ..., "access": ...} or
    # nested payloads where the Zen key rides along under extra fields.
    for container in (value, value.get("payload"), value.get("data")):
        if not isinstance(container, dict):
            continue
        for field in ("zenApiKey", "zen_api_key", "goApiKey", "go_api_key"):
            candidate = container.get(field)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _load_auth_file_key() -> Optional[str]:
    for path in _AUTH_PATHS:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # Preferred: the opencode-go provider record; fall back to plain
        # "opencode" / "opencode-zen" entries which share the console key.
        for provider in ("opencode-go", "opencode", "opencode-zen", "zen"):
            if provider in data:
                key = _extract_api_key(data[provider])
                if key:
                    return key
    return None


def resolve_api_key() -> Optional[str]:
    return _read_env_api_key() or _load_auth_file_key()


# -- tolerant response parsing ------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    return int(number) if number is not None else None


def _parse_timestamp(value: Any) -> Optional[str]:
    """Normalize epoch seconds/millis or ISO-8601 text to an ISO-8601 string."""
    number = _as_float(value)
    if number is not None:
        if number > 1_000_000_000_000:
            number /= 1000.0
        if number > 1_000_000_000:
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            return None
    return None


def _window_percent(window: dict) -> Optional[float]:
    percent = None
    direct = False
    for key in _PERCENT_KEYS:
        value = _as_float(window.get(key))
        if value is not None:
            percent = value
            direct = True
            break
    if percent is None:
        used = next((_as_float(window[k]) for k in _USED_KEYS if _as_float(window.get(k)) is not None), None)
        limit = next((_as_float(window[k]) for k in _LIMIT_KEYS if _as_float(window.get(k)) is not None), None)
        if used is not None and limit is not None and limit > 0:
            percent = (used / limit) * 100.0
    if percent is None:
        return None
    # A direct percent may arrive as a fraction (0..1); computed used/limit
    # values are already 0..100 and must not be rescaled. Integral values are
    # face-value percentages (for example OpenCode Go's ``percent: 1``).
    if direct and 0.0 < percent <= 1.0 and percent != int(percent):
        percent *= 100.0
    return max(0.0, min(100.0, percent))


def _window_reset(window: dict, now: float) -> Optional[str]:
    for key in _RESET_IN_SEC_KEYS:
        seconds = _as_int(window.get(key))
        if seconds is not None and seconds >= 0:
            return datetime.fromtimestamp(now + seconds, tz=timezone.utc).isoformat()
    for key in _RESET_AT_KEYS:
        reset_at = _parse_timestamp(window.get(key))
        if reset_at is not None:
            return reset_at
    return None


def _parse_window(window: dict, label: str, now: float) -> Optional[QuotaWindow]:
    if not isinstance(window, dict):
        return None
    used_percent = _window_percent(window)
    if used_percent is None:
        return None
    return QuotaWindow(
        label=label,
        used_percent=round(used_percent, 2),
        reset_at=_window_reset(window, now),
    )


def _first_dict(record: dict, keys: tuple[str, ...]) -> Optional[dict]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return None


def parse_usage_payload(data: Any, now: Optional[float] = None) -> list[QuotaWindow]:
    """Extract Go's rolling/weekly/monthly windows from any reasonable shape."""
    if not isinstance(data, dict):
        return []
    moment = time.time() if now is None else now

    renews_at = _parse_timestamp(data.get("renewsAt") or data.get("renewAt"))

    # Direct shape: {"rollingUsage": {...}, "weeklyUsage": {...}, ...}
    rolling = _first_dict(data, _ROLLING_KEYS)
    weekly = _first_dict(data, _WEEKLY_KEYS)
    monthly = _first_dict(data, _MONTHLY_KEYS)

    # Nested shapes: {"data": {...}} / {"result": {...}} / {"usage": {...}}
    if rolling is None:
        for wrapper in ("data", "result", "usage", "billing", "payload"):
            nested = data.get(wrapper)
            if isinstance(nested, dict):
                found = parse_usage_payload(nested, now=moment)
                if found:
                    return found
        # Last resort: scan one level deep for dicts whose keys mention the
        # window names (CodexBar's "candidates" strategy, simplified).
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            lower = str(key).lower()
            if rolling is None and any(t in lower for t in ("rolling", "hour", "5h")):
                rolling = value
            elif weekly is None and "week" in lower:
                weekly = value
            elif monthly is None and "month" in lower:
                monthly = value

    windows: list[QuotaWindow] = []
    parsed_rolling = _parse_window(rolling, "5-hour", moment) if rolling else None
    if parsed_rolling is None:
        return []
    windows.append(parsed_rolling)
    parsed_weekly = _parse_window(weekly, "Weekly", moment) if weekly else None
    if parsed_weekly:
        windows.append(parsed_weekly)
    parsed_monthly = _parse_window(monthly, "Monthly", moment) if monthly else None
    if parsed_monthly:
        windows.append(parsed_monthly)
    if renews_at and len(windows) < 3:
        windows[-1].reset_at = windows[-1].reset_at or renews_at
    return windows


# -- network ------------------------------------------------------------------


def _attempt_usage(api_key: str, timeout: float = _REQUEST_TIMEOUT_S) -> tuple[Optional[bytes], Optional[str], bool]:
    """One HTTP GET against the usage API.

    Returns ``(body, None, retryable)`` on success and
    ``(None, unavailable_reason, retryable)`` on failure.  ``retryable`` marks
    the failures that are worth another attempt: the endpoint's intermittent
    503 "Go usage is unavailable", other 5xx/429 statuses, and transport
    errors.  A rejected credential is never retried. ``timeout`` is passed to
    ``urllib`` as a socket inactivity timeout, not a hard wall-clock cap for the
    complete response body.
    """
    request = urllib.request.Request(
        _API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "hermes-quota-plugin",
        },
        method="GET",
    )
    try:
        with urlopen_no_redirect(request, timeout=timeout) as resp:
            return resp.read(), None, False
    except urllib.error.HTTPError as exc:
        code = exc.code
        if exc.fp is not None:
            exc.close()
        if code in (401, 403):
            return None, "auth-failed", False
        if code == 503:
            # The vendor's own wording for this flap; keep it recognizable
            # instead of surfacing a bare http-503.
            return None, "usage-unavailable", True
        return None, f"http-{code}", code in _TRANSIENT_HTTP_STATUSES
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return None, f"fetch-error:{type(exc).__name__}", True


def fetch_usage(
    api_key: str,
    *,
    attempts: int = _RETRY_ATTEMPTS,
    _sleep: Any = time.sleep,
    deadline: Optional[Deadline] = None,
) -> QuotaResult:
    total_attempts = max(1, attempts)
    reason: Optional[str] = None
    data: Any = None
    # The retry budget prevents new attempts/backoffs once spent and caps each
    # urllib socket timeout. It is not an absolute response deadline: a server
    # that trickles bytes can keep one response active beyond this budget.
    # Direct callers get a provider-local budget; a multi-key sweep supplies
    # one shared deadline so serial calls cannot reset the budget per key.
    deadline = deadline or Deadline(_FETCH_BUDGET_S)

    for attempt in range(total_attempts):
        if deadline.expired():
            # Out of budget with nothing to show: say so rather than reporting
            # a reason for a request we never made.
            return build_unavailable(_PROVIDER_ID, "timeout")
        body, reason, retryable = _attempt_usage(api_key, deadline.slice(_REQUEST_TIMEOUT_S))
        if body is not None:
            try:
                data = json.loads(body)
            except Exception:
                data = None
                reason = "bad-json"
                retryable = True
            if data is not None:
                break
        if not retryable or attempt == total_attempts - 1:
            break
        # Preserve an HTTP result already observed (notably the vendor's 503)
        # when the response itself or its retry backoff reaches the budget.
        # Reclassifying a received status as `timeout` would discard the most
        # useful fact we have. Transport failures still report `timeout` once
        # there is no budget left for another attempt.
        is_http_failure = reason == "usage-unavailable" or (
            reason is not None and reason.startswith("http-")
        )
        if deadline.expired():
            if is_http_failure:
                break
            return build_unavailable(_PROVIDER_ID, "timeout")
        backoff = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
        _sleep(min(backoff, deadline.remaining()))
        if deadline.expired():
            if is_http_failure:
                break
            return build_unavailable(_PROVIDER_ID, "timeout")

    if data is None:
        return build_unavailable(_PROVIDER_ID, reason or "no-data")

    windows = parse_usage_payload(data)
    plan = None
    if isinstance(data, dict):
        plan_value = data.get("plan") or data.get("planType") or data.get("plan_type")
        if isinstance(plan_value, str) and plan_value.strip():
            plan = plan_value.strip().title()
    if not windows:
        return build_unavailable(_PROVIDER_ID, "no-data")
    return QuotaResult(label=_PROVIDER_ID, windows=windows, plan=plan, unavailable_reason=None)


def _pool_entries() -> list[tuple[str, str]]:
    """Snapshot Hermes' effective pool read-only, with env-seeded keys included.

    A nonempty profile pool wins; otherwise use the global-root pool. We read
    the auth files directly to avoid core readers that can repair/write them.
    Numbered OPENCODE_GO_API_KEY_N variables are then appended if not already
    present, matching the runtime pool's env seeding behavior.
    """
    keys: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        from hermes_constants import get_hermes_home, get_default_hermes_root

        local_home = get_hermes_home()
        global_home = get_default_hermes_root()
        stores = []
        for home in (local_home, global_home):
            if home in [p for p, _ in stores]:
                continue
            try:
                with open(os.path.join(str(home), "auth.json"), "r", encoding="utf-8-sig") as fh:
                    store = json.load(fh)
                stores.append((home, store if isinstance(store, dict) else {}))
            except (OSError, ValueError, UnicodeError):
                stores.append((home, {}))
        local_store = next((data for home, data in stores if home == local_home), {})
        global_store = next((data for home, data in stores if home == global_home), {})
        local_rows = (local_store.get("credential_pool") or {}).get(_PROVIDER_ID) or []
        global_rows = (global_store.get("credential_pool") or {}).get(_PROVIDER_ID) or []
        entries = local_rows if isinstance(local_rows, list) and local_rows else global_rows
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            token = entry.get("access_token")
            if not isinstance(token, str) or not token.strip():
                continue
            token = token.strip()
            if token in seen:
                continue
            seen.add(token)
            label = str(entry.get("label") or entry.get("id") or "")
            label = label.split("@")[0][:14] or f"key{len(keys) + 1}"
            keys.append((token, label))
    except Exception:
        # Do not make a broken profile store fatal; preserve legacy resolution.
        keys = []
        seen.clear()

    # Hermes can seed a pool from numbered environment siblings. The base key
    # remains the fallback resolver's responsibility when no pool was saved.
    env_names = ["OPENCODE_GO_API_KEY"]
    for index in range(2, 101):
        name = f"OPENCODE_GO_API_KEY_{index}"
        if os.environ.get(name):
            env_names.append(name)
    for name in env_names:
        value = os.environ.get(name)
        token = value.strip().strip("\\\"'") if value else ""
        if token and token not in seen:
            seen.add(token)
            keys.append((token, f"env{len(keys) + 1}"))
    if keys:
        return keys
    single = resolve_api_key()
    return [(single, "key1")] if single else []


def fetch_opencode_go_quota() -> QuotaResult:
    entries = _pool_entries()
    if not entries:
        return build_unavailable(_PROVIDER_ID, "no-credentials")

    deadline = Deadline(_FETCH_BUDGET_S)
    windows: list[QuotaWindow] = []
    plan: Optional[str] = None
    failed: list[str] = []
    for index, (token, label) in enumerate(entries):
        if deadline.expired():
            failed.extend(f"{pending_label}: timeout" for _, pending_label in entries[index:])
            break
        res = fetch_usage(token, deadline=deadline)
        if res.unavailable_reason:
            failed.append(f"{label}: {res.unavailable_reason}")
            continue
        plan = plan or res.plan
        for w in res.windows:
            windows.append(
                QuotaWindow(
                    label=f"{w.label} · {label}",
                    used_percent=w.used_percent,
                    reset_at=w.reset_at,
                )
            )
    if not windows:
        return build_unavailable(_PROVIDER_ID, "no-data", details=failed)
    return QuotaResult(
        label=_PROVIDER_ID,
        windows=windows,
        plan=plan,
        unavailable_reason=None,
        details=failed,
    )


from .registry import register as _register  # noqa: E402

_register("opencode-go")(fetch_opencode_go_quota)
