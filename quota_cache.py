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
      },
      "codex_special_reset": {
        "announcement": null,
        "fetched_at": null,
        "stale": true,
        "unavailable_reason": "codex-not-configured"
      }
    }

``codex_special_reset`` is public announcement metadata for Codex. It is not
account quota: it never changes a provider window or ``reset_at``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from .quota_providers import PROVIDER_FETCHERS, QuotaAccount, QuotaResult, GENERATED_ACCOUNT_ID_PREFIX
from .quota_providers.codex import display_label
from . import codex_special_reset

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


def timezone_name() -> str:
    """Hermes' configured IANA timezone; ``''`` means the host system zone.

    The quota surfaces format timestamps with this zone instead of inheriting
    whatever ``TZ`` the gateway process happened to start with: a widget or a
    slash command must show the user's clock, not the server's.
    """
    try:
        path = Path(get_hermes_home()) / "config.yaml"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("timezone:"):
                value = line.split(":", 1)[1].split("#", 1)[0].strip()
                return value.strip("'\"")
    except Exception:
        logger.debug("quota_cache ▸ timezone read failed", exc_info=True)
    return ""


def local_tz():
    """The user's timezone; the system zone when Hermes has no setting."""
    name = timezone_name()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            logger.debug("quota_cache ▸ invalid timezone %r", name, exc_info=True)
    try:
        return datetime.now().astimezone().tzinfo or timezone.utc
    except Exception:
        return timezone.utc


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


def iter_account_records(providers):
    """Expand optional Codex accounts without changing the provider identity."""

    for name, record in providers.items():
        accounts = record.get("accounts") if name == "openai-codex" and isinstance(record, dict) else None
        if isinstance(accounts, list) and accounts:
            for index, account in enumerate(accounts):
                if isinstance(account, dict):
                    label = display_label(account.get("label")) or f"Account {index + 1}"
                    yield name, {**account, "label": f"{name} · {label}"}
        elif name == "openai-codex" and isinstance(record, dict) and display_label(record.get("account_label")):
            yield name, {**record, "label": f"{name} · {display_label(record['account_label'])}"}
        else:
            yield name, record


def _result_to_record(res: QuotaResult) -> dict[str, Any]:
    record: dict[str, Any] = {
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
    if res.account_label:
        record["account_label"] = res.account_label
    if res.accounts:
        record["accounts"] = [
            ({
                "id": account.id,
                "label": account.label,
                "plan": account.plan,
                "unavailable_reason": account.unavailable_reason,
                "details": list(account.details or []),
                "windows": [
                    {"label": w.label, "used_percent": w.used_percent, "reset_at": w.reset_at}
                    for w in account.windows
                ],
            } if isinstance(account, QuotaAccount) else _result_to_record(account))
            for account in res.accounts
        ]
    return record


def _account_row_key(rows: dict[str, Any], provider_id: str,
                     account_id: Optional[str], index: int) -> str:
    """A cache key for one account row that never collides with an existing one.

    A generated id (an account with no usable user id) uses the reserved
    ``GENERATED_ACCOUNT_ID_PREFIX`` namespace; if it — or any configured id —
    still matches an existing row, a deterministic ``#N`` suffix keeps both rows
    visible instead of silently dropping one.
    """
    if isinstance(account_id, str) and account_id.strip():
        base = f"{provider_id}:{account_id.strip()}"
    else:
        base = f"{provider_id}:{GENERATED_ACCOUNT_ID_PREFIX}{index + 1}"
    if base not in rows:
        return base
    suffix = 2
    candidate = f"{base}#{suffix}"
    while candidate in rows:
        suffix += 1
        candidate = f"{base}#{suffix}"
    return candidate


def _expand_accounts(provider_id: str, record: Any) -> dict[str, Any]:
    """Flatten a provider record and its nested accounts into sibling rows.

    The fetcher owns the account/config logic and attaches accounts nested on
    its result; here each one becomes a ``<provider_id>:<account_id>`` record so
    the cache stays a plain ``provider -> record`` map. Every offline consumer
    (footer, /quota, widget) then reads account rows with no account-specific
    code, and an account with no data is a truthful muted row rather than a
    missing one.
    """
    if not isinstance(record, dict):
        return {provider_id: record}
    # Shallow copy: never mutate the fetcher's own record while stripping the
    # nested accounts key that only exists on the wire.
    record = dict(record)
    nested = record.pop("accounts", None)
    # Codex keeps its saved accounts nested: its Desktop selector and tooltip
    # consume that shape. Claude QuotaAccount rows have stable IDs and are
    # expanded below into collision-safe provider siblings.
    if provider_id == "openai-codex" and isinstance(nested, list):
        record["accounts"] = nested
        return {provider_id: record}
    rows: dict[str, Any] = {provider_id: record}
    if not isinstance(nested, list):
        return rows
    base_label = record.get("label") or provider_id
    for index, account in enumerate(nested):
        if not isinstance(account, dict):
            continue
        account_id = account.get("id")
        key = _account_row_key(rows, provider_id, account_id, index)
        label = account.get("label")
        account_label = label.strip() if isinstance(label, str) and label.strip() else None
        reason = account.get("unavailable_reason")
        details = account.get("details")
        windows = account.get("windows")
        rows[key] = {
            "label": f"{base_label} · {account_label}" if account_label else base_label,
            "provider": provider_id,
            "account_id": account_id.strip() if isinstance(account_id, str) and account_id.strip() else None,
            "account_label": account_label,
            "plan": account.get("plan"),
            "unavailable_reason": reason if isinstance(reason, str) and reason else None,
            "details": [str(d) for d in details] if isinstance(details, list) else [],
            "windows": windows if isinstance(windows, list) else [],
            "account_balances": [],
            "api_calls_available": None,
        }
    return rows


def _unavailable_record(provider_id: str, reason: str) -> dict[str, Any]:
    return {
        "label": provider_id,
        "plan": None,
        "unavailable_reason": reason,
        "details": [],
        "windows": [],
    }


def _codex_configured(providers: dict[str, Any]) -> bool:
    """Whether the real Codex result reflects a configured account.

    The announcement source is only consulted when Codex is actually set up,
    so an unrelated quota sweep never makes this third-party request. Any
    response other than the two "nothing configured" reasons means Codex was
    found, including a failed usage fetch for an existing account.
    """
    record = providers.get("openai-codex")
    if not isinstance(record, dict):
        return False
    return record.get("unavailable_reason") not in (
        "no-credentials", "fetcher-unavailable",
    )


def _special_rank(entry: Any) -> tuple[float, int]:
    """Order announcement cache entries by honest success time, then trust."""
    normalized = codex_special_reset.normalize_entry(entry)
    fetched_at = normalized.get("fetched_at")
    stamp = float("-inf")
    if fetched_at:
        try:
            stamp = datetime.fromisoformat(fetched_at).timestamp()
        except (ValueError, TypeError, OverflowError):
            stamp = float("-inf")
    return stamp, 0 if normalized.get("stale") else 1


def _prefer_newer_special(on_disk: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Never let a slow process clobber a newer success written by another."""
    if not isinstance(on_disk, dict):
        return candidate
    if _special_rank(on_disk) > _special_rank(candidate):
        return codex_special_reset.normalize_entry(on_disk)
    return candidate


def _collect_special_reset(previous: Any, configured: bool,
                           deadline: float, state_path: str) -> dict[str, Any]:
    """Fetch the public announcement inside the existing sweep deadline."""
    if not configured:
        return codex_special_reset.empty_entry("codex-not-configured")
    if deadline <= time.monotonic():
        # Never start a request we already have no budget to wait for.
        return codex_special_reset.stale_entry(previous, "timeout")
    try:
        return codex_special_reset.refresh(
            previous, state_path=state_path,
            timeout=max(0.0, deadline - time.monotonic()))
    except Exception:
        logger.debug("quota_cache ▸ special reset failed", exc_info=True)
        return codex_special_reset.stale_entry(previous, "fetch-error")


def _fetch_one(provider_id: str, fetcher: Any) -> dict[str, Any]:
    """Run one fetcher. Fail-open by contract — never raises."""
    try:
        res = fetcher()
        if res is None:
            return _unavailable_record(provider_id, "no-data")
        if not isinstance(res, QuotaResult):
            logger.debug("quota_cache ▸ fetcher %s returned %r, not a QuotaResult",
                         provider_id, type(res).__name__)
            return _unavailable_record(provider_id, "fetch-error")
        # QuotaResult is mutable and a provider or downstream integration can
        # still supply malformed fields (for example windows=None). Keep
        # serialization inside the fail-open boundary just like the fetch.
        return _result_to_record(res)
    except Exception:
        logger.debug("quota_cache ▸ fetcher %s failed while fetching/serializing",
                     provider_id, exc_info=True)
        return _unavailable_record(provider_id, "fetch-error")


def refresh_quota_cache(*, budget: Optional[float] = None) -> dict[str, Any]:
    """Run every registered provider fetcher concurrently and write the cache.

    Bounded by ``budget`` seconds (``REFRESH_BUDGET_S`` default) and run on
    daemon threads, so one hung provider can neither stretch the call nor hold
    the short-lived CLI process open: whatever finished is written, the rest is
    recorded as ``timeout`` and drops the previous value. Fail-open per
    provider: a fetcher that raises, returns nothing, or misses the deadline
    leaves an ``unavailable_reason`` record rather than aborting the sweep.
    Returns the cache dict that was written.
    """
    budget_s = REFRESH_BUDGET_S if budget is None else max(0.0, float(budget))
    previous_special = read_quota_cache().get("codex_special_reset")
    items = list(PROVIDER_FETCHERS.items())
    results: dict[str, Any] = {}
    lock = threading.Lock()
    deadline = time.monotonic() + budget_s
    # Resolve on the profile's caller thread, before spawning any workers.
    state_path = codex_special_reset._state_path()

    def _worker(pid: str, fetcher: Any, event: threading.Event) -> None:
        record = _unavailable_record(pid, "fetch-error")
        try:
            record = _fetch_one(pid, fetcher)
        except Exception:
            # Keep the sweep moving even if the fail-open helper itself ever
            # regresses or is replaced by an integration that raises.
            logger.debug("quota_cache ▸ worker %s failed", pid, exc_info=True)
        finally:
            try:
                with lock:
                    results[pid] = record
            finally:
                # Always release the sweep waiter, including unexpected worker
                # failures; otherwise it waits out the full refresh budget.
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

    special_result: dict[str, Any] = {}
    special_done = threading.Event()

    def _special_worker() -> None:
        try:
            codex_done = events.get("openai-codex")
            if codex_done is None:
                special_result["entry"] = codex_special_reset.empty_entry(
                    "codex-not-configured")
                return
            codex_done.wait(max(0.0, deadline - time.monotonic()))
            with lock:
                codex = results.get("openai-codex") or _unavailable_record(
                    "openai-codex", "timeout")
            special_result["entry"] = _collect_special_reset(
                previous_special, _codex_configured({"openai-codex": codex}),
                deadline, state_path)
        finally:
            special_done.set()

    # Wait only for Codex discovery, not every other provider: a slow unrelated
    # vendor must not consume the announcement collector's entire budget.
    threading.Thread(target=_special_worker, name="quota-special-reset",
                     daemon=True).start()
    for event in events.values():
        event.wait(max(0.0, deadline - time.monotonic()))
    special_done.wait(max(0.0, deadline - time.monotonic()))

    with lock:
        providers: dict[str, Any] = {}
        for provider_id, _fetcher in items:
            record = results.get(provider_id) or _unavailable_record(provider_id, "timeout")
            providers.update(_expand_accounts(provider_id, record))
        # A sweep whose registry is incomplete (a stub registry in a test, an
        # import race) must not delete providers it never tried. Carry the
        # previous record forward, marked so the display never presents it as
        # current.
        swept = {provider_id for provider_id, _fetcher in items}
        previous_providers = read_quota_cache().get("providers") or {}
        orphans = [key for key in previous_providers
                   if str(key).split(":", 1)[0] not in swept]
        for key in orphans:
            record = previous_providers.get(key)
            if not isinstance(record, dict):
                continue
            # Keep the identity (label/plan) and the note, drop the numbers: a
            # sweep that never tried this provider cannot claim its windows are
            # current, and both the CLI and the widget render windows whenever
            # they exist.
            carried = dict(record)
            carried["unavailable_reason"] = "not-refreshed"
            carried["windows"] = []
            carried["account_balances"] = []
            carried["api_calls_available"] = None
            note = "Not refreshed in this sweep: the provider registry was incomplete"
            details = [d for d in (carried.get("details") or []) if d != note]
            details.append(note)
            carried["details"] = details
            providers[key] = carried
    if orphans:
        logger.warning(
            "quota_cache ▸ sweep covered %d provider(s), kept %d from the previous cache",
            len(swept), len(orphans))

    special = special_result.get("entry") if special_done.is_set() else None
    if not isinstance(special, dict):
        special = codex_special_reset.stale_entry(previous_special, "timeout")
    cache = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
        "codex_special_reset": special,
        # Host metadata: the widgets format announcements in the user's zone
        # instead of trusting the process TZ they happen to run under.
        "timezone": timezone_name(),
    }

    try:
        with _CACHE_LOCK:
            # A sibling process may have written a newer announcement success
            # while this sweep ran; its entry is authoritative.
            on_disk = read_quota_cache().get("codex_special_reset")
            cache["codex_special_reset"] = _prefer_newer_special(on_disk, special)
            path = _cache_path()
            fd, tmp = tempfile.mkstemp(
                prefix="." + os.path.basename(path) + ".", suffix=".tmp",
                dir=os.path.dirname(os.path.abspath(path)))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                tmp = None
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    except Exception:
        logger.debug("quota_cache ▸ write failed", exc_info=True)

    _record_history(cache)
    return cache


def _record_history(cache: dict[str, Any]) -> None:
    """Append this sweep's readings to the monitor's quota history (docs/monitor.md). Fail-open: history is a
    by-product of the refresh and must never break or slow it beyond one local sqlite write."""
    try:
        from .quota_monitor import quotas as monitor_quotas
        # next to the cache file this sweep wrote, so the history follows the cache's home
        monitor_quotas.record_from_cache(cache, home=os.path.dirname(_cache_path()))
    except Exception:
        logger.debug("quota_cache ▸ history record failed", exc_info=True)


def is_fresh() -> bool:
    age = quota_cache_age_seconds()
    return age is not None and age <= MAX_AGE_S


if __name__ == "__main__":
    result = refresh_quota_cache()
    print(json.dumps(result, indent=2, sort_keys=True))