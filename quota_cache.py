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
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_constants import get_hermes_home
from .quota_providers import PROVIDER_FETCHERS, QuotaResult

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "quota_cache.json"
_CACHE_LOCK = threading.Lock()
_MAX_AGE_S = 60 * 30  # 30 minutes — footer drops stale data


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
        "windows": [
            {"label": w.label, "used_percent": w.used_percent, "reset_at": w.reset_at}
            for w in res.windows
        ],
    }


def refresh_quota_cache(*, timeout: float = 12.0) -> dict[str, Any]:
    """Run every registered provider fetcher and write the cache file.

    Fail-open per provider: a fetcher that raises or returns no data leaves an
    ``unavailable_reason`` record rather than aborting the whole refresh.
    Returns the cache dict that was written.
    """
    providers: dict[str, Any] = {}
    for provider_id, fetcher in PROVIDER_FETCHERS.items():
        try:
            res = fetcher()  # type: ignore[operator]
            if res is None:
                providers[provider_id] = {
                    "label": provider_id,
                    "plan": None,
                    "unavailable_reason": "no-data",
                    "windows": [],
                }
            else:
                providers[provider_id] = _result_to_record(res)
        except Exception:
            logger.debug("quota_cache ▸ fetcher %s crashed", provider_id, exc_info=True)
            providers[provider_id] = {
                "label": provider_id,
                "plan": None,
                "unavailable_reason": "fetch-error",
                "windows": [],
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
    return age is not None and age <= _MAX_AGE_S


if __name__ == "__main__":
    result = refresh_quota_cache()
    print(json.dumps(result, indent=2, sort_keys=True))
