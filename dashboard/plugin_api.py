"""Quota plugin — desktop widget backend.

Mounted at /api/plugins/quota/ by the dashboard/gateway plugin system.

Thin read-only door over the plugin's precomputed ``quota_cache.json``
(``$HERMES_HOME/quota_cache.json``). The widget in the desktop app calls
``ctx.rest('/quota')`` → ``GET /api/plugins/quota/quota``; this handler
returns the cached providers (with a derived ``remaining_pct`` per window)
plus freshness metadata, so the widget never does network I/O and never
imports the plugin's fetchers.

Security: routes sit behind the dashboard's session-token middleware (same as
every core ``/api/plugins/...`` route), so this is not an unauthenticated
oracle. The ``GET`` handler only ever READS the cache file. The ``POST /refresh``
handler runs the plugin's own ``refresh_quota_cache()`` (as a subprocess in the
plugin directory) — it executes provider code by design, but only the plugin's
own code, and never with request-supplied input.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from hermes_constants import get_default_hermes_root, get_hermes_home

logger = logging.getLogger(__name__)

router = APIRouter()


def _refresh_cache() -> bool:
    """Force a re-fetch of every provider via the plugin's own refresh.

    Prefers running the plugin's own ``quota_cache.py`` as a subprocess from the
    plugin directory — that's the only context where its relative import
    (``from .quota_providers import ...``) and ``hermes_constants`` resolve
    cleanly. Falls back to an in-process import (when the gateway already has
    the plugin on its path). Never raises — returns False so the caller can
    still serve the cached snapshot.
    """
    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_py = os.path.join(plugin_dir, "quota_cache.py")
    if not os.path.exists(cache_py):
        return False

    # 1) subprocess — most faithful to how the plugin refreshes on its own.
    #    Run as a package module (``-m quota.quota_cache``) from the parent of
    #    the plugin dir so its relative import (``from .quota_providers``) and
    #    ``hermes_constants`` resolve cleanly. Running the file directly as a
    #    script would break the relative import.
    try:
        env = dict(os.environ)
        # Quota is account-scoped, not profile-scoped. Keep one canonical
        # snapshot so a stale per-profile file cannot disagree with analytics.
        env["HERMES_HOME"] = _global_hermes_home()
        plugins_parent = os.path.dirname(plugin_dir)
        proc = subprocess.run(
            [sys.executable, "-m", "quota.quota_cache"],
            cwd=plugins_parent,
            env=env,
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0:
            return True
        logger.debug(
            "quota api ▸ subprocess refresh rc=%s stderr=%s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace")[:300],
        )
    except Exception:
        logger.debug("quota api ▸ subprocess refresh failed", exc_info=True)

    # 2) in-process fallback (works only if the plugin package is importable).
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "quota_plugin_cache", cache_py, submodule_search_locations=[plugin_dir]
        )
        if spec is None or spec.loader is None:
            return False
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.refresh_quota_cache()
        return True
    except Exception:
        logger.debug("quota api ▸ in-process refresh fallback failed", exc_info=True)
        return False

_CACHE_FILENAME = "quota_cache.json"
# Footer drops data older than 30 min; surface the same threshold to the widget.
_MAX_AGE_S = 60 * 30
_AUTO_REFRESH_LOCK = threading.Lock()
_AUTO_REFRESH_STARTED = False
_AUTO_REFRESH_LAST_STARTED = 0.0
_AUTO_REFRESH_COOLDOWN_S = 60.0
_AUTO_REFRESH_EVENT: Optional[threading.Event] = None
_AUTO_REFRESH_WAIT_S = 20.0


def _global_hermes_home() -> str:
    # Global Hermes home, independent of any active desktop profile. Used as
    # the fallback source when the active profile has no providers yet.
    return str(get_default_hermes_root())


def _profile_hermes_home() -> str:
    # Active profile home (honors HERMES_HOME / context override). This is
    # where refresh_quota_cache() writes the per-profile cache, and the default
    # read source per SPEC.md decision #11.
    return str(get_hermes_home())


def _cache_path_for(home: str) -> str:
    return os.path.join(home, _CACHE_FILENAME)


def _read_cache_raw(home: str) -> tuple[dict[str, Any], str]:
    """Return (parsed_cache, cache_path) for a specific Hermes home."""
    path = _cache_path_for(home)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data, path
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("quota api ▸ cache read failed (%s)", home, exc_info=True)
    return {"fetched_at": None, "providers": {}}, path


def _read_cache() -> tuple[dict[str, Any], str]:
    """Read the global quota cache, falling back to a legacy profile cache.

    Quota is account-scoped, so the global cache is canonical. A profile cache
    remains a compatibility fallback for older installations only.
    Returns (cache_dict, effective_cache_path).
    """
    global_cache, global_path = _read_cache_raw(_global_hermes_home())
    if global_cache.get("providers") or {}:
        return global_cache, global_path
    return _read_cache_raw(_profile_hermes_home())


def _ensure_cache_initialized(*, wait: bool = False) -> None:
    """Refresh absent/stale data automatically, optionally waiting for it.

    The first widget request waits briefly so it does not render an old cache
    snapshot while the automatic refresh is already in flight. Concurrent
    requests share the same event; no request starts a duplicate refresh.
    """
    global _AUTO_REFRESH_STARTED, _AUTO_REFRESH_LAST_STARTED, _AUTO_REFRESH_EVENT
    cache, _ = _read_cache()
    age = _age_seconds(cache.get("fetched_at"))
    if cache.get("providers") and age is not None and age <= _MAX_AGE_S:
        return
    with _AUTO_REFRESH_LOCK:
        now = datetime.now(timezone.utc).timestamp()
        if _AUTO_REFRESH_STARTED:
            event = _AUTO_REFRESH_EVENT
        elif now - _AUTO_REFRESH_LAST_STARTED < _AUTO_REFRESH_COOLDOWN_S:
            event = _AUTO_REFRESH_EVENT
        else:
            event = threading.Event()
            _AUTO_REFRESH_EVENT = event
            _AUTO_REFRESH_STARTED = True
            _AUTO_REFRESH_LAST_STARTED = now

            def worker() -> None:
                global _AUTO_REFRESH_STARTED
                try:
                    _refresh_cache()
                finally:
                    event.set()
                    with _AUTO_REFRESH_LOCK:
                        _AUTO_REFRESH_STARTED = False

            threading.Thread(target=worker, name="quota-auto-refresh", daemon=True).start()

    if wait and event is not None:
        event.wait(_AUTO_REFRESH_WAIT_S)


def _age_seconds(fetched_at: Optional[str]) -> Optional[float]:
    if not fetched_at:
        return None
    try:
        dt = datetime.fromisoformat(fetched_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


def _remaining_pct(window: dict[str, Any]) -> Optional[int]:
    used = window.get("used_percent")
    if used is None:
        return None
    try:
        rem = 100.0 - float(used)
    except (TypeError, ValueError):
        return None
    return int(round(max(0.0, min(100.0, rem))))


def _render_cache(cache_with_path: tuple[dict[str, Any], str], *, refreshed: bool = False) -> JSONResponse:
    """Build the widget payload from a ``(cache, effective_path)`` tuple."""
    cache, cache_path = cache_with_path
    providers_raw = cache.get("providers") or {}
    age = _age_seconds(cache.get("fetched_at"))

    providers: dict[str, Any] = {}
    for pid, rec in providers_raw.items():
        if not isinstance(rec, dict):
            continue
        windows = []
        for w in rec.get("windows") or []:
            if not isinstance(w, dict):
                continue
            windows.append(
                {
                    "label": w.get("label"),
                    "used_percent": w.get("used_percent"),
                    "remaining_pct": _remaining_pct(w),
                    "reset_at": w.get("reset_at"),
                }
            )
        providers[pid] = {
            "label": rec.get("label") or pid,
            "plan": rec.get("plan"),
            "unavailable_reason": rec.get("unavailable_reason"),
            "windows": windows,
        }

    return JSONResponse(
        {
            "refreshed": refreshed,
            "cache_source": cache_path,
            "fetched_at": cache.get("fetched_at"),
            "age_seconds": None if age is None else round(age, 1),
            "is_fresh": age is not None and age <= _MAX_AGE_S,
            "stale": age is None or age > _MAX_AGE_S,
            "providers": providers,
        }
    )


@router.get("/quota")
def get_quota() -> JSONResponse:
    """Return the cached per-provider quota snapshot for the desktop widget.

    Reads the active profile's cache first; falls back to the global cache when
    the profile has no providers (SPEC.md decision #11). Shape::

        {
          "refreshed": false,
          "cache_source": "/Users/.../.hermes/profiles/<name>/quota_cache.json",
          "fetched_at": "2026-08-05T...Z" | null,
          "age_seconds": 123.4 | null,
          "is_fresh": true | false,
          "stale": false,
          "providers": {
            "<id>": {
              "label": "...", "plan": "...", "unavailable_reason": null | "...",
              "windows": [{"label", "used_percent", "remaining_pct", "reset_at"}]
            }
          }
        }
    """
    _ensure_cache_initialized(wait=True)
    return _render_cache(_read_cache())


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "plugin": "quota"})


@router.post("/refresh")
def post_refresh() -> JSONResponse:
    """Force a re-fetch of every provider, then return the fresh snapshot.

    The widget offers a manual refresh button that hits this endpoint. Because
    the cache file is shared with the footer / ``/quota`` command, a refresh
    here also refreshes those surfaces.
    """
    refreshed = _refresh_cache()
    return _render_cache(_read_cache(), refreshed=refreshed)
