"""Quota / rate-limit cache for the runtime footer + /quota command (plugin).

The runtime footer (``footer`` lifecycle hook) and the /quota command can show a
per-provider quota block — one provider per line, each window (session / weekly /
monthly) with its remaining % and reset time.  Showing live quota on every final
message would mean N network calls per reply (one per provider), plus the footer
has no live agent / credentials in scope.  Instead:

  * provider fetchers live in ``.quota_providers`` (a pluggable registry);
  * ``refresh_quota_cache()`` runs them on a schedule (cron) and writes a small
    JSON summary to ``$HERMES_HOME/quota_cache.json``;
  * the footer hook and /quota command read that JSON — pure, offline, fast.

Each fetcher is fail-open: a fetch error yields a ``QuotaResult`` with
``unavailable_reason`` set (no fake zeros), so one broken provider never aborts
the whole refresh.

Cache schema (``quota_cache.json``)::

    {
      "fetched_at": "2026-07-31T12:00:00+00:00",
      "providers": {
        "openai-codex": {
          "label": "openai-codex",
          "plan": "Plus",
          "unavailable_reason": null,
          "windows": [
            {"label": "Session", "used_percent": 100.0,
             "reset_at": "2026-08-05T07:00:41+00:00"}
          ]
        },
        "grok": {"label": "grok", "plan": null,
                 "unavailable_reason": "cloudflare-blocked", "windows": []}
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_constants import get_hermes_home
from .quota_providers import PROVIDER_FETCHERS, QuotaResult

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "quota_cache.json"
_CACHE_LOCK = threading.Lock()
# 30 minutes — footer drops stale data. Also used by CLI/quota command for staleness.
MAX_AGE_S = 60 * 30
# Wall-clock budget for one sweep. The widget polls the cache on a fixed cadence,
# and a sweep that overran that interval is what made a configured 60s refresh
# land 100-200s late. Providers run concurrently and the whole sweep is capped
# here, so the poll path can never be stretched by one slow provider.
REFRESH_BUDGET_S = 20.0


def _cache_path() -> str:
    return os.path.join(str(get_hermes_home()), _CACHE_FILENAME)


def read_quota_cache() -> dict[str, Any]:
    """Return the parsed quota cache, or an empty shell if missing/unreadable."""
    try:
        with open(_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("quota_cache ▸ read failed (degrade to empty)", exc_info=True)
    return {"fetched_at": None, "providers": {}}


def quota_cache_age_seconds() -> Optional[float]:
    """Seconds since the cache was fetched, or None if absent/invalid."""
    data = read_quota_cache()
    ts = data.get("fetched_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


def _result_to_record(res: QuotaResult) -> dict[str, Any]:
    return {
        "label": res.label,
        "plan": res.plan,
        "unavailable_reason": res.unavailable_reason,
        "details": list(res.details or []),
        "account_balances": [
            {"currency": b.currency, "total_balance": b.total_balance,
             "granted_balance": b.granted_balance, "topped_up_balance": b.topped_up_balance}
            for b in res.account_balances
        ],
        "api_calls_available": res.api_calls_available,
        "windows": [
            {"label": w.label, "used_percent": w.used_percent, "reset_at": w.reset_at}
            for w in res.windows
        ],
    }


def _unavailable_record(provider_id: str, reason: str) -> dict[str, Any]:
    return {
        "label": provider_id,
        "plan": None,
        "unavailable_reason": reason,
        "details": [],
        "windows": [],
    }


def _fetch_one(provider_id: str, fetcher: Any) -> dict[str, Any]:
    """Run one fetcher. Fail-open by contract — never raises."""
    try:
        res = fetcher()
    except Exception:
        logger.debug("quota_cache ▸ fetcher %s crashed", provider_id, exc_info=True)
        return _unavailable_record(provider_id, "fetch-error")
    if res is None:
        return _unavailable_record(provider_id, "no-data")
    return _result_to_record(res)


def refresh_quota_cache(*, budget: Optional[float] = None) -> dict[str, Any]:
    """Run every registered provider fetcher concurrently and write the cache.

    Bounded by ``budget`` seconds (``REFRESH_BUDGET_S`` default) and run on
    daemon threads, so one hung provider can neither stretch the call nor hold
    the short-lived CLI process open: whatever finished is written, the rest is
    recorded as ``timeout`` and keeps its previous value. Fail-open per
    provider: a fetcher that raises, returns nothing, or misses the deadline
    leaves an ``unavailable_reason`` record rather than aborting the sweep.
    Returns the cache dict that was written.
    """
    budget_s = REFRESH_BUDGET_S if budget is None else max(0.0, float(budget))
    items = list(PROVIDER_FETCHERS.items())
    results: dict[str, Any] = {}
    lock = threading.Lock()

    def _worker(pid: str, fetcher: Any, event: threading.Event) -> None:
        record = _fetch_one(pid, fetcher)
        with lock:
            results[pid] = record
        event.set()

    events: dict[str, threading.Event] = {}
    for provider_id, fetcher in items:
        event = threading.Event()
        events[provider_id] = event
        threading.Thread(
            target=_worker,
            args=(provider_id, fetcher, event),
            name=f"quota-fetch-{provider_id}",
            daemon=True,
        ).start()

    deadline = time.monotonic() + budget_s
    for event in events.values():
        event.wait(max(0.0, deadline - time.monotonic()))

    with lock:
        providers: dict[str, Any] = {
            provider_id: results.get(provider_id)
            or _unavailable_record(provider_id, "timeout")
            for provider_id, _fetcher in items
        }

    cache = {"fetched_at": datetime.now(timezone.utc).isoformat(), "providers": providers}

    try:
        with _CACHE_LOCK:
            path = _cache_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
    except Exception:
        logger.debug("quota_cache ▸ write failed", exc_info=True)

    return cache


def is_fresh() -> bool:
    age = quota_cache_age_seconds()
    return age is not None and age <= MAX_AGE_S


if __name__ == "__main__":
    result = refresh_quota_cache()
    print(json.dumps(result, indent=2, sort_keys=True))