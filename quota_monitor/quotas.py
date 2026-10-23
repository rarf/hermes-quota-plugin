"""Quota items, history and burn rate from the plugin's own ``quota_cache.json`` (no network here).

The cache is written by the existing refresh path (``quota_cache.refresh_quota_cache``); every refresh
appends one reading per provider window to ``quota_monitor.db`` (``record_from_cache``), and the snapshot /
history commands catch up from the cache too, so a refresh done by any copy of the plugin is recorded.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import store
from .common import hermes_home, iso_ts, num, read_json

CACHE_NAME = "quota_cache.json"
BURN_SPAN_S = 7200      # slope of used% over the last 2 h
BURN_MIN_SPAN_S = 600   # ... measured over at least 10 min


def read_cache() -> Dict[str, Any]:
    data = read_json(hermes_home() / CACHE_NAME)
    if isinstance(data, dict) and isinstance(data.get("providers"), dict):
        return data
    return {"fetched_at": None, "providers": {}}


def _windows(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for w in rec.get("windows") or []:
        if not isinstance(w, dict):
            continue
        out.append({"label": w.get("label") or "window", "used": num(w.get("used_percent")),
                    "reset": iso_ts(w.get("reset_at"))})
    return out


def items_from_cache(cache: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Agent Monitor-shaped quota items: {provider, base, name, plan, windows: [{label, used, reset}], ...}.

    ``provider`` is the cache key (``anthropic``, ``anthropic:<account id>``, ...). Codex keeps its saved accounts
    nested in one record; each becomes ``openai-codex:<account id>``."""
    cache = read_cache() if cache is None else cache
    fetched = iso_ts(cache.get("fetched_at"))
    items = []
    for key, rec in (cache.get("providers") or {}).items():
        if not isinstance(rec, dict):
            continue
        base = rec.get("provider") or str(key).split(":", 1)[0]
        raw_nested = rec.get("accounts")
        nested: List[Any] = raw_nested if isinstance(raw_nested, list) else []
        rows = [(key, rec)]
        for i, acc in enumerate(nested):
            if isinstance(acc, dict):
                raw_id = acc.get("id")
                aid = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else "account%d" % (i + 1)
                rows.append(("%s:%s" % (key, aid), dict(acc, provider=base)))
        for pkey, r in rows:
            raw_details = r.get("details")
            details: List[Any] = raw_details if isinstance(raw_details, list) else []
            items.append({
                "provider": pkey, "base": base, "name": r.get("label") or pkey,
                "account_label": r.get("account_label") or (r.get("label") if pkey != key else None),
                "plan": r.get("plan"), "windows": _windows(r),
                "details": [str(d) for d in details], "unavailable": r.get("unavailable_reason") or None,
                "stale": any("stale" in str(d).lower() for d in details), "fetched_at": fetched,
            })
    return items


def readings(items: List[Dict[str, Any]]) -> List[Tuple[str, str, float, Optional[float]]]:
    out = []
    for it in items:
        if it.get("unavailable"):
            continue
        seen = set()
        for w in it["windows"]:
            if w["used"] is None or w["label"] in seen:
                continue
            seen.add(w["label"])
            out.append((it["provider"], w["label"], w["used"], w["reset"]))
    return out


def record_from_cache(cache: Optional[Dict[str, Any]] = None, home: Optional[Path] = None) -> int:
    """Append the cache's readings to quota_monitor.db once per cache write (keyed by ``fetched_at``).
    ``home`` is the directory the cache was written to (the history db sits next to it); default: the active
    Hermes home. Returns rows written (0 when this cache was already recorded or holds no readings)."""
    cache = read_cache() if cache is None else cache
    stamp = cache.get("fetched_at")
    ts = iso_ts(stamp)
    if not ts:
        return 0
    db = Path(home) / store.DB_NAME if home is not None else None
    if store.kv_get("last_recorded_fetch", db=db) == stamp:
        return 0
    n = store.append_readings(readings(items_from_cache(cache)), now=min(ts, time.time()), db=db)
    store.kv_set("last_recorded_fetch", stamp, db=db)
    return n


def history(start: float, end: float) -> List[Dict[str, Any]]:
    series: Dict[Tuple[str, str], List[List[float]]] = defaultdict(list)
    for ts, prov, win, used, _reset in store.history_rows(start, end):
        series[(prov, win)].append([ts, used])
    return [{"provider": p, "window": w, "points": sorted(pts)} for (p, w), pts in series.items()]


def burn(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Slope of used% over the last 2 h per window: [{provider, window, used, per_hour, eta_full,
    fills_before_reset, reset}]."""
    now = time.time() if now is None else now
    resets: Dict[Tuple[str, str], Optional[float]] = {}
    series: Dict[Tuple[str, str], List[Tuple[float, float]]] = defaultdict(list)
    for ts, prov, win, used, reset in store.history_rows(now - BURN_SPAN_S, now + 60):
        series[(prov, win)].append((ts, used))
        resets[(prov, win)] = reset
    out = []
    for (prov, win), pts in series.items():
        pts.sort()
        # a reset inside the span starts a new window: only the readings after the last drop count
        start = 0
        for i in range(1, len(pts)):
            if pts[i][1] < pts[i - 1][1] - 0.5:
                start = i
        pts = pts[start:]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < BURN_MIN_SPAN_S:
            continue
        (t0, u0), (t1, u1) = pts[0], pts[-1]
        per_hour = (u1 - u0) / ((t1 - t0) / 3600)
        eta = (100 - u1) / per_hour * 3600 if per_hour > 0.01 else None
        reset = resets.get((prov, win))
        eta_full = now + eta if eta else None
        out.append({"provider": prov, "window": win, "used": u1, "per_hour": per_hour, "eta_full": eta_full,
                    "reset": reset, "fills_before_reset": bool(eta_full and reset and eta_full < reset)})
    out.sort(key=lambda b: (b["provider"], b["window"]))
    return out
