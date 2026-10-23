"""The quota monitor's own state: ``$HERMES_HOME/quota_monitor.db`` and ``quota_monitor.json``.

Agent Monitor kept this state in a long-running server's memory; a CLI call is one-shot, so it lives here:

* ``quota_history`` — one reading per provider window, appended from the plugin's quota cache;
* ``rate_samples`` — cumulative token/cost totals, one sample per snapshot (the token rate is the delta);
* ``kv`` — small cached blobs (model speed, last recorded cache stamp);
* ``account_audit`` — one row per Accounts-tab write (credential order / pool strategy) the plugin delegated to
  the Hermes CLI: what was asked, what changed. Never a token.

Nothing else on disk is ever written by this package.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .common import hermes_home

DB_NAME = "quota_monitor.db"
PREFS_NAME = "quota_monitor.json"
HISTORY_KEEP_S = 35 * 86400
HEARTBEAT_S = 1800  # an unchanged reading is still stored once per this interval (keeps flat lines visible)
RATE_KEEP_S = 3600 + 120

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS quota_history (ts REAL, provider TEXT, window TEXT, used REAL, reset REAL)",
    "CREATE INDEX IF NOT EXISTS quota_history_ts ON quota_history(ts)",
    "CREATE INDEX IF NOT EXISTS quota_history_pw ON quota_history(provider, window, ts)",
    "CREATE TABLE IF NOT EXISTS rate_samples (ts REAL PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, at REAL, value TEXT)",
    "CREATE TABLE IF NOT EXISTS account_audit (ts REAL, action TEXT, profile TEXT, provider TEXT, detail TEXT)",
)
AUDIT_KEEP = 500


def db_path() -> Path:
    return hermes_home() / DB_NAME


def prefs_path() -> Path:
    return hermes_home() / PREFS_NAME


@contextmanager
def rw(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    path = Path(path) if path is not None else db_path()
    conn = sqlite3.connect(str(path), timeout=5)
    try:
        with conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            yield conn
    finally:
        conn.close()


# ---------- kv ----------

def kv_get(key: str, max_age: Optional[float] = None, db: Optional[Path] = None) -> Any:
    try:
        with rw(db) as c:
            row = c.execute("SELECT at, value FROM kv WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    if max_age is not None and time.time() - (row[0] or 0) > max_age:
        return None
    try:
        return json.loads(row[1])
    except (TypeError, ValueError):
        return None


def kv_set(key: str, value: Any, db: Optional[Path] = None) -> None:
    with rw(db) as c:
        c.execute("INSERT OR REPLACE INTO kv (key, at, value) VALUES (?, ?, ?)",
                  (key, time.time(), json.dumps(value, default=str)))


# ---------- account audit ----------

def audit(action: str, profile: str, provider: str, detail: Dict[str, Any], db: Optional[Path] = None) -> None:
    with rw(db) as c:
        c.execute("INSERT INTO account_audit VALUES (?,?,?,?,?)",
                  (time.time(), action, profile, provider, json.dumps(detail, default=str)))
        c.execute("DELETE FROM account_audit WHERE rowid NOT IN "
                  "(SELECT rowid FROM account_audit ORDER BY ts DESC LIMIT ?)", (AUDIT_KEEP,))


def audit_rows(limit: int = 20, db: Optional[Path] = None) -> List[Dict[str, Any]]:
    try:
        with rw(db) as c:
            rows = c.execute("SELECT ts, action, profile, provider, detail FROM account_audit "
                             "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for ts, action, profile, provider, detail in rows:
        try:
            d = json.loads(detail)
        except (TypeError, ValueError):
            d = {}
        out.append({"ts": ts, "action": action, "profile": profile, "provider": provider, "detail": d})
    return out


# ---------- quota history ----------

def append_readings(readings: List[Tuple[str, str, float, Optional[float]]], now: Optional[float] = None,
                    db: Optional[Path] = None) -> int:
    """Append (provider, window, used%, reset ts) readings; skip one identical to the latest stored reading of
    its window unless that one is older than HEARTBEAT_S. Prunes rows older than HISTORY_KEEP_S.
    Returns how many rows were written."""
    now = time.time() if now is None else now
    written = 0
    with rw(db) as c:
        for provider, window, used, reset in readings:
            last = c.execute("""SELECT ts, used, reset FROM quota_history WHERE provider = ? AND window = ?
                                ORDER BY ts DESC LIMIT 1""", (provider, window)).fetchone()
            if last and last[1] == used and (last[2] or None) == (reset or None) and now - last[0] < HEARTBEAT_S:
                continue
            c.execute("INSERT INTO quota_history VALUES (?,?,?,?,?)", (now, provider, window, used, reset))
            written += 1
        c.execute("DELETE FROM quota_history WHERE ts < ?", (now - HISTORY_KEEP_S,))
    return written


def history_rows(start: float, end: float) -> List[Tuple[float, str, str, float, Optional[float]]]:
    try:
        with rw() as c:
            return c.execute("""SELECT ts, provider, window, used, reset FROM quota_history
                                WHERE ts >= ? AND ts < ? ORDER BY ts""", (start, end)).fetchall()
    except sqlite3.Error:
        return []


# ---------- token-rate samples ----------

def add_rate_sample(data: Dict[str, Any], now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    with rw() as c:
        c.execute("INSERT OR REPLACE INTO rate_samples VALUES (?, ?)", (now, json.dumps(data)))
        c.execute("DELETE FROM rate_samples WHERE ts < ?", (now - RATE_KEEP_S,))


def rate_samples() -> List[Tuple[float, Dict[str, Any]]]:
    try:
        with rw() as c:
            rows = c.execute("SELECT ts, data FROM rate_samples ORDER BY ts").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for ts, data in rows:
        try:
            out.append((ts, json.loads(data)))
        except ValueError:
            continue
    return out


# ---------- prefs (budget) ----------

def read_prefs() -> Dict[str, Any]:
    try:
        with open(prefs_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_prefs(data: Dict[str, Any]) -> None:
    path = prefs_path()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    tmp.replace(path)
